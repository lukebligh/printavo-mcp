from fastmcp import FastMCP
import httpx
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta

mcp = FastMCP("Printavo Assistant")

EMAIL = os.environ.get("PRINTAVO_EMAIL", "")
TOKEN = os.environ.get("PRINTAVO_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
API_URL = "https://www.printavo.com/api/v2"


def query_printavo(query: str, variables: dict = None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = httpx.post(
        API_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "email": EMAIL,
            "token": TOKEN
        },
        timeout=30
    )
    data = response.json()
    if "errors" in data:
        return {"error": data["errors"]}
    return data.get("data", {})


def post_to_slack(message: str):
    """Post a message to Slack via webhook"""
    if not SLACK_WEBHOOK_URL:
        return False
    try:
        response = httpx.post(
            SLACK_WEBHOOK_URL,
            json={"text": message},
            timeout=10
        )
        return response.status_code == 200
    except Exception:
        return False


def normalize_location(text):
    t = text.lower().strip()
    if any(x in t for x in ["full front", "ff", "front - full", "front full"]):
        return "FF"
    elif any(x in t for x in ["left chest", "lc", "left-chest"]):
        return "LC"
    elif any(x in t for x in ["full back", "fb", "back - full", "back full"]):
        return "FB"
    elif any(x in t for x in ["right chest", "rc", "right-chest"]):
        return "RC"
    elif any(x in t for x in ["back top", "top back", "upper back"]):
        return "Back Top"
    elif any(x in t for x in ["sleeve", "slv"]):
        return "SLV"
    elif "hood" in t:
        return "HD"
    elif "front" in t:
        return "Front"
    elif "back" in t:
        return "Back"
    elif "chest" in t:
        return "Chest"
    return text.strip()[:15] if text.strip() else "?"


def extract_matrix_colors(matrix_col):
    if not matrix_col:
        return None
    mc = re.search(r'(\d+)\s*[Cc]olor', matrix_col)
    if mc:
        return int(mc.group(1))
    mc = re.search(r'(\d+)', matrix_col)
    if mc:
        return int(mc.group(1))
    return None


def parse_imprint_details(details):
    results = []
    if not details:
        return results
    loc_match = re.search(r'Location:\s*(.+?)(?:\n|Colors:|$)', details, re.IGNORECASE)
    if not loc_match:
        return results
    location = normalize_location(loc_match.group(1).strip())
    color_match = re.search(r'Colors:\s*(\d+)\s*Colors?', details, re.IGNORECASE)
    if not color_match:
        color_match = re.search(r'(\d+)\s*[-]?\s*color', details, re.IGNORECASE)
    colors = int(color_match.group(1)) if color_match else None
    results.append({"location": location, "colors": colors})
    return results


def parse_description_imprints(description):
    results = []
    if not description:
        return results
    matches = re.findall(r'(\d+)C\s+([A-Za-z][A-Za-z ]{1,15}?)(?:\n|,|$)', description)
    for colors, location in matches:
        loc = normalize_location(location.strip())
        results.append({"location": loc, "colors": int(colors)})
    return results


def imprint_type_sort_key(primary_type: str) -> int:
    """Sort key: Screen Print=1, Embroidery=2, DTF/Store/other=3"""
    t = (primary_type or "").lower()
    if "screen print" in t:
        return 1
    elif "embroid" in t:
        return 2
    else:
        return 3


def resolve_primary_type(current: str, new_type: str) -> str:
    """Keep whichever type has the highest sort priority (lowest key)."""
    if imprint_type_sort_key(new_type) < imprint_type_sort_key(current):
        return new_type
    return current


# Statuses to exclude from the production schedule
EXCLUDED_STATUS_PREFIXES = ["promo items"]
EXCLUDED_STATUS_EXACT    = ["quote", "quote sent"]


def should_exclude_status(status_name: str) -> bool:
    s = status_name.lower().strip()
    if any(s.startswith(p) for p in EXCLUDED_STATUS_PREFIXES):
        return True
    if s in EXCLUDED_STATUS_EXACT:
        return True
    return False


def build_production_schedule(date: str) -> str:
    """Core logic for production schedule — used by both the tool and the Slack scheduler."""

    # ── Phase 1: Collect invoices for this date ─────────────────────────────
    all_invoices = []
    page_query = """
    query($cursor: String) {
        invoices(first: 25, after: $cursor, sortOn: VISUAL_ID, sortDescending: true) {
            nodes {
                id
                visualId
                nickname
                totalQuantity
                startAt
                status { name }
                contact { fullName }
            }
            pageInfo { hasNextPage endCursor }
        }
    }
    """
    cursor = None
    pages_searched = 0
    found_any = False
    consecutive_misses = 0

    while pages_searched < 20:
        variables = {"cursor": cursor} if cursor else {}
        result = query_printavo(page_query, variables)
        if "error" in result:
            return f"API Error: {result['error']}"
        nodes     = (result.get("invoices") or {}).get("nodes", [])
        page_info = (result.get("invoices") or {}).get("pageInfo", {})

        for o in nodes:
            start = (o.get("startAt") or "")[:10]
            if start == date:
                status_name = (o.get("status") or {}).get("name") or ""
                found_any = True
                consecutive_misses = 0
                if should_exclude_status(status_name):
                    continue
                all_invoices.append(o)
            elif found_any and start < date:
                consecutive_misses += 1

        if consecutive_misses >= 25:
            break
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        pages_searched += 1

    if not all_invoices:
        return f"No in-house orders scheduled for production on {date}."

    # ── FIX: sizes added so we can calculate per-group quantity ─────────────
    detail_query = """
    query($id: ID!) {
        invoice(id: $id) {
            lineItemGroups {
                nodes {
                    lineItems {
    nodes {
        description
        items
    }
}
                    imprints {
                        nodes {
                            details
                            typeOfWork { name }
                            pricingMatrixColumn { columnName }
                        }
                    }
                }
            }
        }
    }
    """

    # ── Phase 2: Fetch detail + parse each order ─────────────────────────────
    order_data_list = []

    for o in all_invoices:
        try:
            qty         = int(o.get("totalQuantity") or 0)
            customer    = (o.get("contact") or {}).get("fullName") or "Unknown"
            nickname    = o.get("nickname") or ""
            status_name = (o.get("status") or {}).get("name") or "?"
            invoice_id  = o.get("id")
            is_store    = "store" in status_name.lower()

            if is_store:
                order_data_list.append({
                    "visual_id":     o.get("visualId"),
                    "customer":      customer,
                    "nickname":      nickname,
                    "status_name":   status_name,
                    "qty":           qty,
                    "is_store":      True,
                    "imprint_lines": ["    → Store Order (InkSoft) — pieces only"],
                    "order_screens": 0,
                    "total_imprints": 0,
                    "primary_type":  "Store Order",
                    "breakdown":     {"Store Order (pieces only)": qty},
                    "error":         None,
                })
                continue

            groups           = []
            all_descriptions = []  # order-level, for order-level fallback only

            if invoice_id:
                detail_result = query_printavo(detail_query, {"id": invoice_id})
                if "error" not in detail_result:
                    invoice_data = detail_result.get("invoice") or {}
                    groups = (invoice_data.get("lineItemGroups") or {}).get("nodes") or []

            order_screens    = 0
            imprint_lines    = []
            breakdown        = {}
            primary_type     = "Screen Print"
            total_imprints   = 0   # ── FIX: accumulated at group level, not qty × locations
            any_group_parsed = False

            for group in groups:
                # ── FIX: calculate this group's quantity from its own line item sizes ──
                group_qty = 0
                group_descriptions = []
                items = (group.get("lineItems") or {}).get("nodes") or []
                for item in items:
                    desc = item.get("description") or ""
                    if desc:
                        group_descriptions.append(desc)
                        all_descriptions.append(desc)
                    group_qty += int(item.get("items") or 0)

                # Safety net: if sizes aren't entered, fall back to order total for this group
                if group_qty == 0:
                    group_qty = qty

                imprints      = (group.get("imprints") or {}).get("nodes") or []
                group_locs    = 0
                group_parsed  = False

                for imp in imprints:
                    type_of_work  = (imp.get("typeOfWork") or {}).get("name") or ""
                    details       = imp.get("details") or ""
                    matrix_col    = (imp.get("pricingMatrixColumn") or {}).get("columnName") or ""
                    details_lower = details.lower()

                    # Layer 1 — TypeOfWork (most reliable)
                    if type_of_work and type_of_work.lower() == "embroidery":
                        imprint_lines.append("    → Embroidery")
                        breakdown["Embroidery"] = breakdown.get("Embroidery", 0) + group_qty
                        primary_type = resolve_primary_type(primary_type, "Embroidery")
                        group_locs  += 1
                        group_parsed = True
                        any_group_parsed = True
                        continue

                    # Determine decoration type
                    if "dtf" in details_lower:
                        dec_type = "DTF"
                    elif "embroid" in details_lower:
                        dec_type = "Embroidery"
                    elif "screenprint" in details_lower or "screen print" in details_lower:
                        dec_type = "Screen Print"
                    else:
                        dec_type = "Screen Print"

                    matrix_colors = extract_matrix_colors(matrix_col)

                    # Layer 2 — Structured "Location: / Colors:" text
                    parsed = parse_imprint_details(details)
                    if parsed:
                        for p in parsed:
                            loc       = p.get("location", "?")
                            colors    = p.get("colors") or matrix_colors
                            color_str = f"{colors}C" if colors else "?C"
                            screens   = colors if colors else 0
                            order_screens += screens
                            imprint_lines.append(f"    → {dec_type} | {loc}: {color_str}")
                            breakdown[dec_type] = breakdown.get(dec_type, 0) + group_qty
                            primary_type = resolve_primary_type(primary_type, dec_type)
                            group_locs  += 1
                            group_parsed = True
                            any_group_parsed = True

                    # Layer 3 — Short plain text (contract imprint cards)
                    elif details and len(details.strip()) < 60 and "name:" not in details_lower and "color:" not in details_lower:
                        loc       = normalize_location(details.strip())
                        colors    = matrix_colors
                        color_str = f"{colors}C" if colors else "?C"
                        screens   = colors if colors else 0
                        order_screens += screens
                        imprint_lines.append(f"    → {dec_type} | {loc}: {color_str}")
                        breakdown[dec_type] = breakdown.get(dec_type, 0) + group_qty
                        primary_type = resolve_primary_type(primary_type, dec_type)
                        group_locs  += 1
                        group_parsed = True
                        any_group_parsed = True

                    # Layer 4 — PricingMatrixColumn color count only
                    elif matrix_colors:
                        color_str = f"{matrix_colors}C"
                        order_screens += matrix_colors
                        imprint_lines.append(f"    → {dec_type} | {color_str} (location not entered)")
                        breakdown[dec_type] = breakdown.get(dec_type, 0) + group_qty
                        primary_type = resolve_primary_type(primary_type, dec_type)
                        group_locs  += 1
                        group_parsed = True
                        any_group_parsed = True

                    else:
                        imprint_lines.append(f"    → {dec_type} | (no color/location entered)")
                        breakdown[dec_type] = breakdown.get(dec_type, 0) + group_qty
                        primary_type = resolve_primary_type(primary_type, dec_type)
                        group_locs  += 1
                        any_group_parsed = True

                # ── FIX: accumulate this group's imprint contribution ────────
                total_imprints += group_qty * group_locs

                # Per-group description fallback — runs if this group had no imprint nodes
                if not group_parsed and group_descriptions:
                    seen_combos = set()
                    for desc in group_descriptions:
                        parsed_desc = parse_description_imprints(desc)
                        for p in parsed_desc:
                            key = (p["location"], p["colors"])
                            if key not in seen_combos:
                                seen_combos.add(key)
                                loc       = p["location"]
                                colors    = p["colors"]
                                color_str = f"{colors}C"
                                order_screens  += colors
                                total_imprints += group_qty
                                imprint_lines.append(f"    → Screen Print | {loc}: {color_str}")
                                breakdown["Screen Print"] = breakdown.get("Screen Print", 0) + group_qty
                                primary_type = resolve_primary_type(primary_type, "Screen Print")
                                any_group_parsed = True
                    if not seen_combos:
                        imprint_lines.append("    → Screen Print | (no color/location entered)")
                        breakdown["Screen Print"] = breakdown.get("Screen Print", 0) + group_qty
                        primary_type = resolve_primary_type(primary_type, "Screen Print")
                        total_imprints += group_qty
                        any_group_parsed = True

            # Order-level fallback — no groups at all or all groups were empty
            if not any_group_parsed and all_descriptions:
                seen_combos = set()
                for desc in all_descriptions:
                    parsed_desc = parse_description_imprints(desc)
                    for p in parsed_desc:
                        key = (p["location"], p["colors"])
                        if key not in seen_combos:
                            seen_combos.add(key)
                            loc       = p["location"]
                            colors    = p["colors"]
                            color_str = f"{colors}C"
                            order_screens  += colors
                            total_imprints += qty
                            imprint_lines.append(f"    → Screen Print | {loc}: {color_str}")
                            breakdown["Screen Print"] = breakdown.get("Screen Print", 0) + qty
                            primary_type = resolve_primary_type(primary_type, "Screen Print")
                if not seen_combos:
                    imprint_lines.append("    → Screen Print | (no color/location entered)")
                    breakdown["Screen Print"] = breakdown.get("Screen Print", 0) + qty
                    primary_type = resolve_primary_type(primary_type, "Screen Print")
                    total_imprints += qty

            order_data_list.append({
                "visual_id":     o.get("visualId"),
                "customer":      customer,
                "nickname":      nickname,
                "status_name":   status_name,
                "qty":           qty,
                "is_store":      False,
                "imprint_lines": imprint_lines,
                "order_screens": order_screens,
                "total_imprints": total_imprints,  # ── FIX: pre-calculated, not qty × locations
                "primary_type":  primary_type,
                "breakdown":     breakdown,
                "error":         None,
            })

        except Exception as e:
            order_data_list.append({
                "visual_id":    o.get("visualId"),
                "error":        str(e),
                "primary_type": "Screen Print",
                "qty":          0,
                "breakdown":    {},
            })

    # ── Phase 3: Sort by imprint type, then render ───────────────────────────
    order_data_list.sort(key=lambda x: imprint_type_sort_key(x.get("primary_type", "")))

    lines                = [f"*PRODUCTION SCHEDULE — {date}*", ""]
    grand_total_qty      = 0
    grand_total_imprints = 0
    grand_total_screens  = 0
    breakdown_by_type    = {}

    for od in order_data_list:
        if od.get("error"):
            lines.append(f"  [Skipped #{od.get('visual_id')} due to error: {od['error']}]")
            lines.append("")
            continue

        qty = od["qty"]
        grand_total_qty += qty
        for t, v in od.get("breakdown", {}).items():
            breakdown_by_type[t] = breakdown_by_type.get(t, 0) + v

        if od["is_store"]:
            lines.append(f"  #{od['visual_id']} | {od['customer']} | {od['nickname']}")
            lines.append(f"    Status: {od['status_name']} | Items: {qty}")
            lines.extend(od["imprint_lines"])
            lines.append("")
            continue

        order_screens   = od["order_screens"]
        order_imprints  = od["total_imprints"]  # ── FIX: use pre-calculated value directly
        grand_total_imprints += order_imprints
        grand_total_screens  += order_screens

        lines.append(f"  #{od['visual_id']} | {od['customer']} | {od['nickname']}")
        lines.append(
            f"    Status: {od['status_name']} | Items: {qty} | "
            f"Imprints: {order_imprints} | "
            f"Est. Screens: {order_screens if order_screens else '(not entered)'}"
        )
        lines.extend(od["imprint_lines"])
        lines.append("")

    lines.append("─" * 50)
    lines.append(f"TOTALS FOR {date}:")
    lines.append(f"  Orders on schedule: {len([o for o in order_data_list if not o.get('error')])}")
    lines.append(f"  Total Items: {grand_total_qty}")
    lines.append(f"  Total Imprints (excl. store orders): {grand_total_imprints}")
    lines.append(
        f"  Est. Total Screens Needed: "
        f"{grand_total_screens if grand_total_screens else '(color data missing on some orders)'}"
    )
    if breakdown_by_type:
        lines.append("")
        lines.append("  BY TYPE:")
        for type_name in sorted(breakdown_by_type.keys(), key=imprint_type_sort_key):
            count = breakdown_by_type[type_name]
            label = "pieces" if "store" in type_name.lower() else "imprints"
            lines.append(f"    {type_name}: {count} {label}")

    return "\n".join(lines)


def run_daily_scheduler():
    """Background thread — posts production schedule to Slack at 6am CST on weekdays only."""
    CST = timezone(timedelta(hours=-6))
    while True:
        now = datetime.now(CST)
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        time.sleep(wait_seconds)

        fire_time = datetime.now(CST)
        if fire_time.weekday() >= 5:
            continue

        today    = fire_time.strftime("%Y-%m-%d")
        day_name = fire_time.strftime("%A, %B %d")
        schedule = build_production_schedule(today)
        message  = (
            f":printer: *Good morning, Est. Merch team!* "
            f"Here's your production schedule for *{day_name}*:\n\n{schedule}"
        )
        post_to_slack(message)


# ── TOOL 1: Get Recent Orders ────────────────────────────────────────────────
@mcp.tool()
def get_recent_orders(limit: int = 10) -> str:
    """Get the most recent orders/invoices from Printavo, newest first"""
    query = """
    query {
        invoices(first: %d, sortOn: VISUAL_ID, sortDescending: true) {
            nodes {
                id
                visualId
                nickname
                total
                dueAt
                status { name }
                contact { fullName email phone }
            }
        }
    }
    """ % limit
    result = query_printavo(query)
    if "error" in result:
        return f"API Error: {result['error']}"
    invoices = result.get("invoices", {}).get("nodes", [])
    if not invoices:
        return "No invoices found."
    lines = [f"RECENT {len(invoices)} ORDERS:"]
    for o in invoices:
        due = o.get('dueAt', 'N/A')
        due_clean = due[:10] if due else 'N/A'
        lines.append(
            f"  #{o.get('visualId')} | {o.get('contact', {}).get('fullName', 'Unknown')} | "
            f"Total: ${o.get('total', 0)} | "
            f"Status: {o.get('status', {}).get('name', '?')} | Due: {due_clean}"
        )
    return "\n".join(lines)


# ── TOOL 2: Search by Customer Name ─────────────────────────────────────────
@mcp.tool()
def search_orders(customer_name: str) -> str:
    """Search for orders by customer name"""
    query = """
    query($q: String) {
        invoices(first: 25, query: $q, sortOn: VISUAL_ID, sortDescending: true) {
            nodes {
                id
                visualId
                nickname
                total
                dueAt
                status { name }
                contact { fullName email phone }
            }
        }
    }
    """
    result = query_printavo(query, {"q": customer_name})
    if "error" in result:
        return f"API Error: {result['error']}"
    invoices = result.get("invoices", {}).get("nodes", [])
    if not invoices:
        return f"No orders found for '{customer_name}'."
    lines = [f"Found {len(invoices)} order(s) for '{customer_name}':"]
    for o in invoices:
        due = o.get('dueAt', 'N/A')
        due_clean = due[:10] if due else 'N/A'
        lines.append(
            f"  #{o.get('visualId')} | ${o.get('total', 0)} | "
            f"Status: {o.get('status', {}).get('name', '?')} | Due: {due_clean}"
        )
    return "\n".join(lines)


# ── TOOL 3: Get Order Details ────────────────────────────────────────────────
@mcp.tool()
def get_order_details(order_number: str) -> str:
    """Get full details on a specific order including style, color, and size quantities."""
    query = """
    query($q: String) {
        invoices(first: 5, query: $q) {
            nodes {
                id
                visualId
                nickname
                total
                dueAt
                status { name }
                contact { fullName email phone }
                lineItemGroups {
                    nodes {
                        id
                        lineItems {
                            nodes {
                                id
                                description
                                itemNumber
                                color
                                price
                                sizes { count size }
                            }
                        }
                    }
                }
            }
        }
    }
    """
    result = query_printavo(query, {"q": order_number})
    if "error" in result:
        return f"API Error: {result['error']}"
    invoices = result.get("invoices", {}).get("nodes", [])
    matching = [i for i in invoices if str(i.get("visualId", "")) == str(order_number)]
    if not matching:
        return f"Order #{order_number} not found."
    o = matching[0]
    due = o.get('dueAt', 'N/A')
    due_clean = due[:10] if due else 'N/A'
    lines = [
        f"ORDER #{o.get('visualId')} — {o.get('nickname', '')}",
        f"Customer: {o.get('contact', {}).get('fullName')} | {o.get('contact', {}).get('email')} | {o.get('contact', {}).get('phone')}",
        f"Status: {o.get('status', {}).get('name')} | Due: {due_clean}",
        f"Total: ${o.get('total')}",
        "",
        "LINE ITEMS:"
    ]
    for group in o.get("lineItemGroups", {}).get("nodes", []):
        for item in group.get("lineItems", {}).get("nodes", []):
            lines.append(
                f"  Item: {item.get('itemNumber', 'N/A')} | {item.get('description', '')} | "
                f"Color: {item.get('color', 'N/A')} | ${item.get('price', '?')} ea"
            )
            size_parts = []
            for size in item.get("sizes", []):
                count = size.get("count")
                raw_size = size.get("size", "")
                if count:
                    clean_size = raw_size.replace("size_", "").upper()
                    size_parts.append(f"{clean_size}: {count}")
            if size_parts:
                lines.append(f"    Sizes: {' | '.join(size_parts)}")
            else:
                lines.append("    Sizes: None entered")
    return "\n".join(lines)


# ── TOOL 4: Get All Statuses ─────────────────────────────────────────────────
@mcp.tool()
def get_statuses() -> str:
    """Get all order statuses configured in your Printavo account"""
    query = """
    query {
        statuses(first: 25) {
            nodes { id name color }
        }
    }
    """
    result = query_printavo(query)
    if "error" in result:
        return f"API Error: {result['error']}"
    statuses = result.get("statuses", {}).get("nodes", [])
    if not statuses:
        return "No statuses found."
    lines = ["YOUR PRINTAVO STATUSES:"]
    for s in statuses:
        lines.append(f"  ID: {s.get('id')} | Name: {s.get('name')} | Color: {s.get('color')}")
    return "\n".join(lines)


# ── TOOL 5: Outstanding Balances ─────────────────────────────────────────────
@mcp.tool()
def get_outstanding_balances() -> str:
    """Get open orders that are not yet marked paid or done"""
    query = """
    query {
        invoices(first: 25, sortOn: VISUAL_ID, sortDescending: true) {
            nodes {
                visualId
                total
                dueAt
                contact { fullName email phone }
                status { name id }
            }
        }
    }
    """
    result = query_printavo(query)
    if "error" in result:
        return f"API Error: {result['error']}"
    invoices = result.get("invoices", {}).get("nodes", [])
    paid_keywords = ["paid", "done", "complete", "cancelled", "canceled", "void"]
    unpaid = [
        o for o in invoices
        if not any(kw in (o.get("status", {}).get("name", "").lower()) for kw in paid_keywords)
    ]
    if not unpaid:
        lines = ["No open orders found in most recent 25. All statuses returned:"]
        seen = set()
        for o in invoices:
            name = o.get("status", {}).get("name", "Unknown")
            if name not in seen:
                seen.add(name)
                lines.append(f"  - {name}")
        return "\n".join(lines)
    total_outstanding = sum(float(o.get("total") or 0) for o in unpaid)
    lines = [f"OPEN ORDERS — {len(unpaid)} orders | Gross value: ${total_outstanding:.2f}", ""]
    for o in unpaid:
        due = o.get('dueAt', 'N/A')
        due_clean = due[:10] if due else 'N/A'
        lines.append(
            f"  #{o.get('visualId')} | {o.get('contact', {}).get('fullName')} | "
            f"Total: ${float(o.get('total') or 0):.2f} | Due: {due_clean} | "
            f"Status: {o.get('status', {}).get('name')} | "
            f"Phone: {o.get('contact', {}).get('phone', 'N/A')}"
        )
    return "\n".join(lines)


# ── TOOL 6: Create a Quote ───────────────────────────────────────────────────
@mcp.tool()
def create_quote(customer_email: str, order_name: str, due_date: str) -> str:
    """
    Create a new quote in Printavo.
    customer_email: must already exist as a contact in Printavo
    order_name: nickname for this job
    due_date: format YYYY-MM-DD
    """
    contact_query = """
    query($q: String) {
        contacts(first: 1, query: $q) {
            nodes { id fullName email }
        }
    }
    """
    contact_result = query_printavo(contact_query, {"q": customer_email})
    contacts = contact_result.get("contacts", {}).get("nodes", [])
    if not contacts:
        return f"No customer found with email '{customer_email}'. Add them to Printavo first."
    contact = contacts[0]
    mutation = """
    mutation($contactId: ID!, $nickname: String, $dueAt: ISO8601DateTime) {
        quoteCreate(input: { contactId: $contactId, nickname: $nickname, dueAt: $dueAt }) {
            quote { id visualId nickname dueAt }
            errors { message }
        }
    }
    """
    result = query_printavo(mutation, {
        "contactId": contact["id"],
        "nickname": order_name,
        "dueAt": f"{due_date}T00:00:00Z"
    })
    quote_data = result.get("quoteCreate", {})
    errors = quote_data.get("errors", [])
    if errors:
        return f"Printavo error: {errors}"
    quote = quote_data.get("quote", {})
    return (
        f"Quote created!\n"
        f"Order #{quote.get('visualId')} | {quote.get('nickname')} | "
        f"For: {contact['fullName']} | Due: {quote.get('dueAt', '')[:10]}"
    )


# ── TOOL 7: Inspect API Field Names ─────────────────────────────────────────
@mcp.tool()
def inspect_fields(type_name: str) -> str:
    """Look up the exact field names on any Printavo API type."""
    query = """
    query($typeName: String!) {
        __type(name: $typeName) {
            name
            fields {
                name
                type { name kind }
            }
        }
    }
    """
    result = query_printavo(query, {"typeName": type_name})
    if "error" in result:
        return f"API Error: {result['error']}"
    type_data = result.get("__type")
    if not type_data:
        return f"Type '{type_name}' not found."
    fields = type_data.get("fields", [])
    lines = [f"FIELDS ON {type_name}:"]
    for f in fields:
        type_info = f.get("type", {})
        lines.append(f"  {f.get('name')} ({type_info.get('name', type_info.get('kind', '?'))})")
    return "\n".join(lines)


# ── TOOL 8: Production Schedule ──────────────────────────────────────────────
@mcp.tool()
def get_production_schedule(date: str) -> str:
    """
    Get all in-house orders scheduled for production on a given date.
    Excludes PROMO ITEMS and quote/quote-sent statuses.
    Sorted by imprint type: Screen Print → Embroidery → DTF/Store.
    date: format YYYY-MM-DD (e.g. 2026-05-01)
    """
    return build_production_schedule(date)


# ── TOOL 9: Send Production Schedule to Slack Now ────────────────────────────
@mcp.tool()
def send_schedule_to_slack(date: str) -> str:
    """
    Manually send the production schedule for any date to Slack right now.
    date: format YYYY-MM-DD (e.g. 2026-05-01)
    """
    schedule = build_production_schedule(date)
    day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%A, %B %d")
    message  = (
        f":printer: *Good morning, Est. Merch team!* "
        f"Here's your production schedule for *{day_name}*:\n\n{schedule}"
    )
    success = post_to_slack(message)
    if success:
        return f"Production schedule for {date} posted to Slack successfully!"
    else:
        return "Failed to post to Slack. Check that SLACK_WEBHOOK_URL is set in Railway variables."


if __name__ == "__main__":
    # Start the daily 6am Slack scheduler in a background thread
    scheduler_thread = threading.Thread(target=run_daily_scheduler, daemon=True)
    scheduler_thread.start()

    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
