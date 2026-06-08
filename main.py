from fastmcp import FastMCP
import httpx
import os
import re
import math
import threading
import time
from datetime import datetime, timezone, timedelta

mcp = FastMCP("Printavo Assistant")

EMAIL = os.environ.get("PRINTAVO_EMAIL", "")
TOKEN = os.environ.get("PRINTAVO_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
API_URL = "https://www.printavo.com/api/v2"


# ── API ───────────────────────────────────────────────────────────────────────

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


# ── DECORATION TYPE ───────────────────────────────────────────────────────────

def resolve_decoration_type(matrix_name: str) -> str:
    """
    Derive decoration type from pricing matrix name.
    CONTRACT EMB 2024, Embroidery 2025 → Embroidery
    TRANSFERS (CONTRACT), TRANSFERS (DIRECT) → DTF
    Everything else (including DO NOT USE matrices) → Screen Print
    """
    if not matrix_name:
        return "Screen Print"
    m = matrix_name.lower().strip()
    if "contract emb" in m or "embroidery 2025" in m:
        return "Embroidery"
    if "transfers" in m:
        return "DTF"
    return "Screen Print"


# ── COLOR COUNT ───────────────────────────────────────────────────────────────

def extract_matrix_colors(matrix_col: str):
    """
    Extract color count from pricing matrix column name.
    e.g. 'Contract SP 2025 (NEW) • 1 Color' → 1
         '2 Color' → 2
    Returns int or None if not found.
    """
    if not matrix_col:
        return None
    mc = re.search(r'(\d+)\s*[Cc]olor', matrix_col)
    if mc:
        return int(mc.group(1))
    mc = re.search(r'(\d+)', matrix_col)
    if mc:
        return int(mc.group(1))
    return None


# ── PRODUCTION TIME ───────────────────────────────────────────────────────────

# Screen Print constants
SP_SETUP_MINUTES = {1: 5, 2: 9, 3: 13, 4: 17, 5: 21, 6: 25}
SP_BASE_RATE     = 375   # pieces/hour at 1-2 colors
SP_COLOR_PENALTY = 0.10  # 10% run rate reduction per color above 2C
SP_MAX_COLORS    = 6

# Embroidery constants
EMB_MINUTES_PER_RUN = 16.5
EMB_2HEAD_HEADS     = 2
EMB_6HEAD_HEADS     = 6
EMB_6HEAD_THRESHOLD = 24
EMB_DUAL_THRESHOLD  = 48

# Capacity thresholds (hours)
CAPACITY_CURRENT_HOURS = 6.25
CAPACITY_TARGET_HOURS  = 8.0


def sp_run_rate(colors: int) -> float:
    c = min(max(colors, 1), SP_MAX_COLORS)
    if c <= 2:
        return SP_BASE_RATE
    penalty = SP_COLOR_PENALTY * (c - 2)
    return SP_BASE_RATE * (1 - penalty)


def sp_setup_minutes(colors: int) -> float:
    c = min(max(colors, 1), SP_MAX_COLORS)
    return SP_SETUP_MINUTES.get(c, SP_SETUP_MINUTES[SP_MAX_COLORS])


def estimate_sp_minutes(qty: int, colors: int) -> float:
    """
    Screen print time for one imprint block:
      setup time (by color count) + run time (qty / rate * 60)
    Called once per imprint block, then summed across the group.
    """
    if qty <= 0 or colors <= 0:
        return 0.0
    setup = sp_setup_minutes(colors)
    run   = (qty / sp_run_rate(colors)) * 60
    return setup + run


def estimate_emb_minutes(qty: int) -> float:
    """
    Embroidery time based on piece count and machine configuration.
    <24 pcs → 2-head only
    24-47   → 6-head only
    48+     → both machines simultaneously (time = max of each machine's portion)
    """
    if qty <= 0:
        return 0.0
    if qty >= EMB_DUAL_THRESHOLD:
        six_qty  = math.ceil(qty * (EMB_6HEAD_HEADS / (EMB_6HEAD_HEADS + EMB_2HEAD_HEADS)))
        two_qty  = qty - six_qty
        six_time = math.ceil(six_qty / EMB_6HEAD_HEADS) * EMB_MINUTES_PER_RUN
        two_time = math.ceil(two_qty / EMB_2HEAD_HEADS) * EMB_MINUTES_PER_RUN
        return max(six_time, two_time)
    elif qty >= EMB_6HEAD_THRESHOLD:
        return math.ceil(qty / EMB_6HEAD_HEADS) * EMB_MINUTES_PER_RUN
    else:
        return math.ceil(qty / EMB_2HEAD_HEADS) * EMB_MINUTES_PER_RUN


def format_duration(minutes: float) -> str:
    h = int(minutes // 60)
    m = int(minutes % 60)
    if h > 0 and m > 0:
        return f"{h}h {m}m"
    elif h > 0:
        return f"{h}h"
    else:
        return f"{m}m"


def capacity_flag(total_minutes: float) -> str:
    hours = total_minutes / 60
    if hours <= CAPACITY_CURRENT_HOURS * 0.8:
        return "🟢 On Track"
    elif hours <= CAPACITY_CURRENT_HOURS:
        return "🟡 Full Day"
    elif hours <= CAPACITY_TARGET_HOURS:
        return "🟠 Needs Extended Hours (to 4:30)"
    else:
        return "🔴 OVERLOADED — Reschedule Required"


# ── STATUS FILTERING ──────────────────────────────────────────────────────────

EXCLUDED_STATUS_PREFIXES = ["promo items"]
EXCLUDED_STATUS_EXACT    = ["quote", "quote sent"]


def should_exclude_status(status_name: str) -> bool:
    s = status_name.lower().strip()
    if any(s.startswith(p) for p in EXCLUDED_STATUS_PREFIXES):
        return True
    if s in EXCLUDED_STATUS_EXACT:
        return True
    return False


# ── DECORATION TYPE SORT ──────────────────────────────────────────────────────

def imprint_type_sort_key(primary_type: str) -> int:
    t = (primary_type or "").lower()
    if "screen print" in t:
        return 1
    elif "embroid" in t:
        return 2
    else:
        return 3


# ── CORE SCHEDULE BUILDER ─────────────────────────────────────────────────────

def build_production_schedule(date: str) -> str:
    """
    Builds the production schedule for a given date.

    Data model (per line item group):
      - total_pieces  = sum of sizes.count across all line items in the group
      - decoration    = resolved from pricingMatrixColumn.columnName on imprint blocks
      - per imprint block:
          colors      = extract_matrix_colors(columnName)
          contributes: colors to screen count, estimate_sp_minutes(total_pieces, colors) to time
      - total_imprints = total_pieces × number of imprint blocks in group
      - total_screens  = sum of colors across all imprint blocks in group
      - group_time     = sum of estimate_sp_minutes(total_pieces, colors) per imprint block (SP)
                       = estimate_emb_minutes(total_pieces) (EMB, calculated once per group)
                       = 0 (DTF — not calculated)

    All group values are summed to invoice totals.
    """

    # ── Phase 1: Fetch invoices scheduled for this date ──────────────────────
    all_invoices = []
    page_query = """
    query($cursor: String) {
        invoices(first: 25, after: $cursor, sortOn: VISUAL_ID, sortDescending: true) {
            nodes {
                id
                visualId
                nickname
                totalQuantity
                dueAt
                status { name }
                contact { fullName }
            }
            pageInfo { hasNextPage endCursor }
        }
    }
    """
    cursor         = None
    pages_searched = 0

    while pages_searched < 20:
        variables = {"cursor": cursor} if cursor else {}
        result    = query_printavo(page_query, variables)
        if "error" in result:
            return f"API Error: {result['error']}"

        nodes     = (result.get("invoices") or {}).get("nodes", [])
        page_info = (result.get("invoices") or {}).get("pageInfo", {})

        for o in nodes:
            # Use dueAt (Production Due Date) — what Printavo calendar plots against.
            # startAt is the Power Scheduler time slot and does not match calendar dates.
            due_date_str = (o.get("dueAt") or "")[:10]
            status_name  = (o.get("status") or {}).get("name") or ""

            if due_date_str == date:
                if not should_exclude_status(status_name):
                    all_invoices.append(o)

        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        pages_searched += 1

    if not all_invoices:
        return f"No in-house orders scheduled for production on {date}."

    # ── Phase 2: Fetch group/imprint detail for each invoice ─────────────────
    #
    # Lean query — only what the new data model requires:
    #   lineItems → sizes.count (to sum total_pieces per group)
    #   imprints  → pricingMatrixColumn.columnName (color count + decoration type)
    #
    # Deliberately excludes: description, itemNumber, color, price, details,
    # typeOfWork — none of these are needed for the calculation and their
    # inclusion on large orders (e.g. 800+ pcs with many size variants) was
    # responsible for exceeding Printavo's GraphQL complexity limit of 25,000.

    detail_query = """
    query($id: ID!) {
        invoice(id: $id) {
            lineItemGroups {
                nodes {
                    lineItems {
                        nodes {
                            sizes { count }
                        }
                    }
                    imprints {
                        nodes {
                            pricingMatrixColumn { columnName }
                        }
                    }
                }
            }
        }
    }
    """

    # ── Phase 3: Parse each invoice ──────────────────────────────────────────
    order_data_list = []

    for o in all_invoices:
        try:
            visual_id   = o.get("visualId")
            customer    = (o.get("contact") or {}).get("fullName") or "Unknown"
            nickname    = o.get("nickname") or ""
            status_name = (o.get("status") or {}).get("name") or "?"
            invoice_id  = o.get("id")
            is_store    = "store" in status_name.lower()

            # Store orders — pieces only, no imprint calculation
            if is_store:
                qty = int(o.get("totalQuantity") or 0)
                order_data_list.append({
                    "visual_id":      visual_id,
                    "customer":       customer,
                    "nickname":       nickname,
                    "status_name":    status_name,
                    "qty":            qty,
                    "is_store":       True,
                    "primary_type":   "Store Order",
                    "total_imprints": 0,
                    "total_screens":  0,
                    "est_minutes":    0.0,
                    "group_summaries": [],
                    "error":          None,
                })
                continue

            # Fetch group detail
            groups = []
            if invoice_id:
                detail_result = query_printavo(detail_query, {"id": invoice_id})
                if "error" not in detail_result:
                    invoice_data = detail_result.get("invoice") or {}
                    groups = (invoice_data.get("lineItemGroups") or {}).get("nodes") or []

            # ── Per-group calculation ─────────────────────────────────────────
            invoice_total_pieces  = 0
            invoice_total_imprints = 0
            invoice_total_screens  = 0
            invoice_est_minutes    = 0.0
            invoice_primary_type   = "Screen Print"
            group_summaries        = []
            data_missing           = False

            # Pre-check: does ANY group in this invoice have sizes populated?
            # If none do, Printavo has no size matrix data — quantity was entered
            # as a flat number. In that case we use invoice totalQuantity and
            # distribute it across groups proportionally (or just use it directly
            # for single-group invoices).
            any_sizes = False
            for g in groups:
                for item in (g.get("lineItems") or {}).get("nodes") or []:
                    if item.get("sizes"):
                        any_sizes = True
                        break
                if any_sizes:
                    break

            invoice_qty_fallback = int(o.get("totalQuantity") or 0)

            for group in groups:

                # 1. Total pieces for this group
                group_pieces = 0
                line_items   = (group.get("lineItems") or {}).get("nodes") or []

                if any_sizes:
                    # Normal path: sum sizes.count across line items
                    for item in line_items:
                        for size in (item.get("sizes") or []):
                            group_pieces += int(size.get("count") or 0)
                else:
                    # Flat-quantity path: no sizes entered anywhere on this invoice.
                    # Use totalQuantity for single-group invoices.
                    # For multi-group: distribute evenly (best available approximation).
                    if len(groups) == 1:
                        group_pieces = invoice_qty_fallback
                    else:
                        group_pieces = invoice_qty_fallback // len(groups)

                # 2. Imprint blocks
                imprint_nodes = (group.get("imprints") or {}).get("nodes") or []

                if not imprint_nodes:
                    # Group has no imprints entered — flag it but don't skip
                    group_summaries.append({
                        "pieces":       group_pieces,
                        "decoration":   "Screen Print",
                        "num_imprints": 0,
                        "total_imprints": 0,
                        "screens":      0,
                        "minutes":      0.0,
                        "missing_data": True,
                    })
                    data_missing = True
                    invoice_total_pieces += group_pieces
                    continue

                # 3. Per imprint block: decoration type, color count
                group_decoration = "Screen Print"  # will be set from first imprint
                group_screens    = 0
                group_minutes    = 0.0
                emb_counted      = False

                for imp in imprint_nodes:
                    col_name = (imp.get("pricingMatrixColumn") or {}).get("columnName") or ""
                    dec_type = resolve_decoration_type(col_name)

                    # Decoration type: first imprint block wins
                    # (groups should never be mixed type)
                    if group_decoration == "Screen Print" and dec_type != "Screen Print":
                        group_decoration = dec_type

                    if dec_type == "Screen Print":
                        colors = extract_matrix_colors(col_name)
                        if colors:
                            group_screens += colors
                            group_minutes += estimate_sp_minutes(group_pieces, colors)
                        else:
                            data_missing = True

                    elif dec_type == "Embroidery":
                        # EMB time calculated once per group regardless of imprint count
                        if not emb_counted:
                            group_minutes += estimate_emb_minutes(group_pieces)
                            emb_counted    = True
                        # EMB doesn't add screens

                    elif dec_type == "DTF":
                        # DTF time not calculated
                        pass

                # 4. Group totals
                num_imprints        = len(imprint_nodes)
                group_total_imprints = group_pieces * num_imprints

                # Track primary decoration type for sort (SP > EMB > DTF)
                if imprint_type_sort_key(group_decoration) < imprint_type_sort_key(invoice_primary_type):
                    invoice_primary_type = group_decoration

                group_summaries.append({
                    "pieces":         group_pieces,
                    "decoration":     group_decoration,
                    "num_imprints":   num_imprints,
                    "total_imprints": group_total_imprints,
                    "screens":        group_screens,
                    "minutes":        group_minutes,
                    "missing_data":   data_missing,
                })

                invoice_total_pieces   += group_pieces
                invoice_total_imprints += group_total_imprints
                invoice_total_screens  += group_screens
                invoice_est_minutes    += group_minutes

            order_data_list.append({
                "visual_id":      visual_id,
                "customer":       customer,
                "nickname":       nickname,
                "status_name":    status_name,
                "qty":            invoice_total_pieces,
                "is_store":       False,
                "primary_type":   invoice_primary_type,
                "total_imprints": invoice_total_imprints,
                "total_screens":  invoice_total_screens,
                "est_minutes":    invoice_est_minutes,
                "group_summaries": group_summaries,
                "data_missing":   data_missing,
                "error":          None,
            })

        except Exception as e:
            order_data_list.append({
                "visual_id":  o.get("visualId"),
                "customer":   (o.get("contact") or {}).get("fullName") or "Unknown",
                "nickname":   o.get("nickname") or "",
                "status_name": (o.get("status") or {}).get("name") or "?",
                "error":      str(e),
                "primary_type": "Screen Print",
                "qty":        0,
                "total_imprints": 0,
                "total_screens":  0,
                "est_minutes":    0.0,
                "group_summaries": [],
                "is_store":   False,
            })

    # ── Phase 4: Sort — SP first, then EMB, then DTF/Store ───────────────────
    order_data_list.sort(key=lambda x: imprint_type_sort_key(x.get("primary_type", "")))

    # ── Phase 5: Render output ────────────────────────────────────────────────
    lines = [f"*PRODUCTION SCHEDULE — {date}*", ""]

    grand_total_qty      = 0
    grand_total_imprints = 0
    grand_total_screens  = 0
    grand_total_minutes  = 0.0
    breakdown_by_type    = {}

    for od in order_data_list:
        if od.get("error"):
            lines.append(f"  ⚠️ #{od.get('visual_id')} | {od.get('customer')} — skipped due to error: {od['error']}")
            lines.append("")
            continue

        grand_total_qty     += od["qty"]
        grand_total_minutes += od["est_minutes"]
        grand_total_imprints += od["total_imprints"]
        grand_total_screens  += od["total_screens"]

        dec = od["primary_type"]
        breakdown_by_type[dec] = breakdown_by_type.get(dec, {"pieces": 0, "imprints": 0})
        breakdown_by_type[dec]["pieces"]   += od["qty"]
        breakdown_by_type[dec]["imprints"] += od["total_imprints"]

        if od["is_store"]:
            lines.append(f"  #{od['visual_id']} | {od['customer']} | {od['nickname']}")
            lines.append(f"    Status: {od['status_name']} | Items: {od['qty']} | Store Order")
            lines.append("")
            continue

        # Order header
        screens_str  = str(od["total_screens"]) if od["total_screens"] else "⚠️ not entered"
        time_str     = format_duration(od["est_minutes"]) if od["est_minutes"] else "⚠️ unknown"
        missing_flag = " | ⚠️ some imprint data missing" if od.get("data_missing") else ""

        lines.append(f"  #{od['visual_id']} | {od['customer']} | {od['nickname']}")
        lines.append(
            f"    Status: {od['status_name']} | "
            f"Items: {od['qty']} | "
            f"Total Imprints: {od['total_imprints']} | "
            f"Screens: {screens_str} | "
            f"Est. Time: {time_str}"
            f"{missing_flag}"
        )

        # Group detail lines
        for i, g in enumerate(od["group_summaries"], 1):
            if len(od["group_summaries"]) > 1:
                lines.append(f"    Group {i}:")
            if g.get("missing_data") and g["num_imprints"] == 0:
                lines.append(f"      ⚠️ {g['pieces']} pcs — no imprints entered")
            else:
                lines.append(
                    f"      {g['decoration']} | "
                    f"{g['pieces']} pcs × {g['num_imprints']} imprint(s) = "
                    f"{g['total_imprints']} imprints | "
                    f"{g['screens']} screens | "
                    f"{format_duration(g['minutes']) if g['minutes'] else '⚠️ unknown'}"
                )

        lines.append("")

    # ── Daily totals ──────────────────────────────────────────────────────────
    lines.append("─" * 50)
    lines.append(f"TOTALS FOR {date}:")
    lines.append(f"  Orders: {len([o for o in order_data_list if not o.get('error')])}")
    lines.append(f"  Total Pieces: {grand_total_qty}")
    lines.append(f"  Total Imprints: {grand_total_imprints}")
    lines.append(
        f"  Total Screens: "
        f"{grand_total_screens if grand_total_screens else '⚠️ color data missing on some orders'}"
    )
    lines.append(f"  Est. Production Time: {format_duration(grand_total_minutes)}")
    lines.append(f"  Capacity Status: {capacity_flag(grand_total_minutes)}")

    if breakdown_by_type:
        lines.append("")
        lines.append("  BY TYPE:")
        for type_name in sorted(breakdown_by_type.keys(), key=imprint_type_sort_key):
            b = breakdown_by_type[type_name]
            lines.append(f"    {type_name}: {b['pieces']} pcs | {b['imprints']} imprints")

    return "\n".join(lines)


# ── BACKGROUND SLACK SCHEDULER ────────────────────────────────────────────────

def run_daily_scheduler():
    """Posts production schedule to Slack at 6am CST on weekdays."""
    CST = timezone(timedelta(hours=-6))
    while True:
        now    = datetime.now(CST)
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())

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


# ── TOOL 1: Get Recent Orders ─────────────────────────────────────────────────
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
        due       = o.get("dueAt", "N/A")
        due_clean = due[:10] if due else "N/A"
        lines.append(
            f"  #{o.get('visualId')} | {o.get('contact', {}).get('fullName', 'Unknown')} | "
            f"Total: ${o.get('total', 0)} | "
            f"Status: {o.get('status', {}).get('name', '?')} | Due: {due_clean}"
        )
    return "\n".join(lines)


# ── TOOL 2: Search Orders by Customer Name ────────────────────────────────────
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
        due       = o.get("dueAt", "N/A")
        due_clean = due[:10] if due else "N/A"
        lines.append(
            f"  #{o.get('visualId')} | ${o.get('total', 0)} | "
            f"Status: {o.get('status', {}).get('name', '?')} | Due: {due_clean}"
        )
    return "\n".join(lines)


# ── TOOL 3: Get Order Details ─────────────────────────────────────────────────
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
    result   = query_printavo(query, {"q": order_number})
    if "error" in result:
        return f"API Error: {result['error']}"
    invoices = result.get("invoices", {}).get("nodes", [])
    matching = [i for i in invoices if str(i.get("visualId", "")) == str(order_number)]
    if not matching:
        return f"Order #{order_number} not found."
    o         = matching[0]
    due       = o.get("dueAt", "N/A")
    due_clean = due[:10] if due else "N/A"
    lines = [
        f"ORDER #{o.get('visualId')} — {o.get('nickname', '')}",
        f"Customer: {o.get('contact', {}).get('fullName')} | "
        f"{o.get('contact', {}).get('email')} | "
        f"{o.get('contact', {}).get('phone')}",
        f"Status: {o.get('status', {}).get('name')} | Due: {due_clean}",
        f"Total: ${o.get('total')}",
        "",
        "LINE ITEMS:"
    ]
    for group in o.get("lineItemGroups", {}).get("nodes", []):
        for item in group.get("lineItems", {}).get("nodes", []):
            lines.append(
                f"  Item: {item.get('itemNumber', 'N/A')} | "
                f"{item.get('description', '')} | "
                f"Color: {item.get('color', 'N/A')} | "
                f"${item.get('price', '?')} ea"
            )
            size_parts = []
            for size in item.get("sizes", []):
                count    = size.get("count")
                raw_size = size.get("size", "")
                if count:
                    clean_size = raw_size.replace("size_", "").upper()
                    size_parts.append(f"{clean_size}: {count}")
            lines.append(
                f"    Sizes: {' | '.join(size_parts)}" if size_parts else "    Sizes: None entered"
            )
    return "\n".join(lines)


# ── TOOL 4: Get All Statuses ──────────────────────────────────────────────────
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
        lines.append(
            f"  ID: {s.get('id')} | Name: {s.get('name')} | Color: {s.get('color')}"
        )
    return "\n".join(lines)


# ── TOOL 5: Outstanding Balances ──────────────────────────────────────────────
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
    invoices     = result.get("invoices", {}).get("nodes", [])
    paid_keywords = ["paid", "done", "complete", "cancelled", "canceled", "void"]
    unpaid       = [
        o for o in invoices
        if not any(kw in (o.get("status", {}).get("name", "").lower()) for kw in paid_keywords)
    ]
    if not unpaid:
        lines = ["No open orders found in most recent 25. Statuses returned:"]
        seen  = set()
        for o in invoices:
            name = o.get("status", {}).get("name", "Unknown")
            if name not in seen:
                seen.add(name)
                lines.append(f"  - {name}")
        return "\n".join(lines)
    total_outstanding = sum(float(o.get("total") or 0) for o in unpaid)
    lines = [f"OPEN ORDERS — {len(unpaid)} orders | Gross value: ${total_outstanding:.2f}", ""]
    for o in unpaid:
        due       = o.get("dueAt", "N/A")
        due_clean = due[:10] if due else "N/A"
        lines.append(
            f"  #{o.get('visualId')} | {o.get('contact', {}).get('fullName')} | "
            f"Total: ${float(o.get('total') or 0):.2f} | Due: {due_clean} | "
            f"Status: {o.get('status', {}).get('name')} | "
            f"Phone: {o.get('contact', {}).get('phone', 'N/A')}"
        )
    return "\n".join(lines)


# ── TOOL 6: Create a Quote ────────────────────────────────────────────────────
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
    contacts       = contact_result.get("contacts", {}).get("nodes", [])
    if not contacts:
        return f"No customer found with email '{customer_email}'. Add them to Printavo first."
    contact  = contacts[0]
    mutation = """
    mutation($contactId: ID!, $nickname: String, $dueAt: ISO8601DateTime) {
        quoteCreate(input: { contactId: $contactId, nickname: $nickname, dueAt: $dueAt }) {
            quote { id visualId nickname dueAt }
            errors { message }
        }
    }
    """
    result     = query_printavo(mutation, {
        "contactId": contact["id"],
        "nickname":  order_name,
        "dueAt":     f"{due_date}T00:00:00Z"
    })
    quote_data = result.get("quoteCreate", {})
    errors     = quote_data.get("errors", [])
    if errors:
        return f"Printavo error: {errors}"
    quote = quote_data.get("quote", {})
    return (
        f"Quote created!\n"
        f"Order #{quote.get('visualId')} | {quote.get('nickname')} | "
        f"For: {contact['fullName']} | Due: {quote.get('dueAt', '')[:10]}"
    )


# ── TOOL 7: Inspect API Field Names ──────────────────────────────────────────
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
    result    = query_printavo(query, {"typeName": type_name})
    if "error" in result:
        return f"API Error: {result['error']}"
    type_data = result.get("__type")
    if not type_data:
        return f"Type '{type_name}' not found."
    fields = type_data.get("fields", [])
    lines  = [f"FIELDS ON {type_name}:"]
    for f in fields:
        type_info = f.get("type", {})
        lines.append(f"  {f.get('name')} ({type_info.get('name', type_info.get('kind', '?'))})")
    return "\n".join(lines)


# ── TOOL 8: Production Schedule ───────────────────────────────────────────────
@mcp.tool()
def get_production_schedule(date: str) -> str:
    """
    Get all in-house orders scheduled for production on a given date.
    Excludes PROMO ITEMS and quote/quote-sent statuses.
    Sorted: Screen Print → Embroidery → DTF → Store Orders.

    Per order shows:
      - Total pieces, total imprints, total screens, estimated production time
      - Per line item group breakdown

    Decoration type resolved from pricing matrix name — no manual text parsing.
    date: format YYYY-MM-DD (e.g. 2026-06-09)
    """
    return build_production_schedule(date)


# ── TOOL 9: Send Production Schedule to Slack ─────────────────────────────────
@mcp.tool()
def send_schedule_to_slack(date: str) -> str:
    """
    Manually send the production schedule for any date to Slack right now.
    date: format YYYY-MM-DD (e.g. 2026-06-09)
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
        return "Failed to post to Slack. Check SLACK_WEBHOOK_URL in Railway variables."


# ── TOOL 10: Production Time Estimate ────────────────────────────────────────
@mcp.tool()
def get_production_time_estimate(date: str) -> str:
    """
    Returns a focused production time estimate for a given date.
    Shows per-order time, daily total, and capacity status.

    Capacity thresholds:
      🟢 On Track       = under 80% of current capacity (~5h)
      🟡 Full Day       = 80–100% of current capacity (5–6h 15m)
      🟠 Extended Hours = requires staying to 4:30pm (6h 15m–8h)
      🔴 Overloaded     = exceeds even extended hours (8h+)

    date: format YYYY-MM-DD (e.g. 2026-06-09)
    """
    raw = build_production_schedule(date)
    if "No in-house orders" in raw or "API Error" in raw:
        return raw

    # Extract per-order time lines and daily totals from rendered schedule
    order_times  = []
    total_line   = ""
    status_line  = ""
    current_vid  = None

    for line in raw.split("\n"):
        stripped = line.strip()

        # Capture order visual ID
        if stripped.startswith("#") and "|" in stripped:
            current_vid = stripped.split("|")[0].strip()

        # Capture Est. Time from the order detail line
        if "Est. Time:" in stripped and current_vid:
            time_part = stripped.split("Est. Time:")[-1].strip()
            # Strip any trailing warning flags
            time_part = time_part.split("|")[0].strip()
            order_times.append(f"  {current_vid} — {time_part}")
            current_vid = None

        if "Est. Production Time:" in stripped:
            total_line = stripped.split("Est. Production Time:")[-1].strip()

        if "Capacity Status:" in stripped:
            status_line = stripped.split("Capacity Status:")[-1].strip()

    lines = [f"*PRODUCTION TIME ESTIMATE — {date}*", ""]
    if order_times:
        lines.append("  PER ORDER:")
        lines.extend(order_times)
        lines.append("")
    lines.append(f"  Total: {total_line}")
    lines.append(f"  Status: {status_line}")
    lines.append("")
    lines.append("  Thresholds:")
    lines.append("    🟢 On Track       = under 5h production")
    lines.append("    🟡 Full Day        = 5h – 6h 15m (current standard)")
    lines.append("    🟠 Extended Hours  = 6h 15m – 8h (requires 8am–4:30pm)")
    lines.append("    🔴 Overloaded      = over 8h — reschedule required")

    return "\n".join(lines)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    scheduler_thread = threading.Thread(target=run_daily_scheduler, daemon=True)
    scheduler_thread.start()

    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
