from fastmcp import FastMCP
import httpx
import os
import re
import math
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

mcp = FastMCP("Printavo Assistant")

EMAIL            = os.environ.get("PRINTAVO_EMAIL", "")
TOKEN            = os.environ.get("PRINTAVO_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
API_URL          = "https://www.printavo.com/api/v2"

# ── CORE HELPER ───────────────────────────────────────────────────────────────
def query_printavo(query: str, variables: dict = None, allow_partial: bool = False):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = httpx.post(
        API_URL,
        json=payload,
        headers={"Content-Type": "application/json", "email": EMAIL, "token": TOKEN},
        timeout=30,
    )
    data = response.json()
    has_errors = "errors" in data
    has_data = bool(data.get("data"))
    if has_errors and (not allow_partial or not has_data):
        return {"error": data["errors"]}
    return data.get("data", {})


# ── DECORATION TYPE HELPERS ───────────────────────────────────────────────────

# Matrix name → decoration type (lowercase key matching)
_MATRIX_TYPE_MAP = {
    "contract sp":       "Screen Print",
    "direct 2024":       "Screen Print",
    "freshprints":       "Screen Print",
    "wholesale direct":  "Screen Print",
    "contract emb":      "Embroidery",
    "embroidery 2025":   "Embroidery",
    "transfers":         "DTF",
}

def _decoration_type(imprint_node: dict) -> str:
    """Determine decoration type from an imprint node."""
    tow = (imprint_node.get("typeOfWork") or {}).get("name", "")
    if tow:
        t = tow.lower()
        if "emb" in t:
            return "Embroidery"
        if "dtf" in t or "transfer" in t:
            return "DTF"
        return "Screen Print"
    matrix_name = (
        (imprint_node.get("pricingMatrixColumn") or {})
        .get("matrix", {})
        .get("name", "")
        .lower()
    )
    for key, deco in _MATRIX_TYPE_MAP.items():
        if key in matrix_name:
            return deco
    return "Screen Print"  # safe default


def _color_count(imprint_node: dict) -> int:
    """Extract screen/color count from pricingMatrixColumn.columnName."""
    col_name = (imprint_node.get("pricingMatrixColumn") or {}).get("columnName", "")
    m = re.search(r'(\d+)\s*[Cc]olor', col_name)
    return int(m.group(1)) if m else 0


def _fetch_imprints_for_order(internal_id: str) -> list:
    """
    Fetch imprint nodes for a single order by internal ID.
    Returns a list of imprint dicts with keys: type, colors, col_name.
    Returns None on API error.

    Uses invoice(id) — a single-object query — which reliably traverses
    lineItemGroups.nodes without hitting complexity limits.
    """
    q = """
    query($id: ID!) {
        invoice(id: $id) {
            totalQuantity
            lineItemGroups {
                nodes {
                    imprints {
                        nodes {
                            typeOfWork { name }
                            pricingMatrixColumn { columnName matrix { name } }
                        }
                    }
                }
            }
        }
    }
    """
    result = query_printavo(q, {"id": internal_id})
    if "error" in result:
        return None
    inv = result.get("invoice") or {}
    groups = inv.get("lineItemGroups", {}).get("nodes", [])
    imprints = []
    for g in groups:
        for imp in g.get("imprints", {}).get("nodes", []):
            imprints.append({
                "type":     _decoration_type(imp),
                "colors":   _color_count(imp),
                "col_name": (imp.get("pricingMatrixColumn") or {}).get("columnName", ""),
            })
    # Also capture totalQuantity from this fetch (more reliable than list query)
    return imprints, int(inv.get("totalQuantity") or 0)


def _format_est_time(total_min: int) -> str:
    if total_min <= 0:
        return "?"
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


# ── EXISTING TOOLS ────────────────────────────────────────────────────────────

@mcp.tool()
def get_recent_orders(limit: int = 10) -> str:
    """Get the most recent Printavo orders."""
    q = """
    query($first: Int) {
        invoices(first: $first) {
            nodes {
                id
                visualId
                nickname
                total
                dueAt
                startAt
                status { name }
                contact { fullName email }
            }
        }
    }
    """
    result = query_printavo(q, {"first": limit})
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("invoices", {}).get("nodes", [])
    if not nodes:
        return "No recent orders found."
    lines = [f"RECENT ORDERS (last {len(nodes)}):"]
    for inv in nodes:
        status = (inv.get("status") or {}).get("name", "?")
        contact = (inv.get("contact") or {}).get("fullName", "?")
        due = (inv.get("dueAt") or "")[:10]
        prod = (inv.get("startAt") or "")[:10]
        lines.append(
            f"  #{inv.get('visualId')} | {inv.get('nickname', '')} | "
            f"${inv.get('total', 0)} | {status} | Customer: {contact} | "
            f"Due: {due} | Prod: {prod}"
        )
    return "\n".join(lines)


@mcp.tool()
def search_orders(query: str, limit: int = 10) -> str:
    """
    Search Printavo orders by keyword, order number, customer name, etc.
    query: search string (e.g. '6817', 'Underground Printing', 'BEBT')
    """
    q = """
    query($q: String, $first: Int) {
        invoices(first: $first, query: $q) {
            nodes {
                id
                visualId
                nickname
                total
                dueAt
                status { name }
                contact { fullName email }
            }
        }
    }
    """
    result = query_printavo(q, {"q": query, "first": limit})
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("invoices", {}).get("nodes", [])
    if not nodes:
        return f"No orders found matching '{query}'."
    lines = [f"SEARCH RESULTS for '{query}' ({len(nodes)} found):"]
    for inv in nodes:
        status = (inv.get("status") or {}).get("name", "?")
        contact = (inv.get("contact") or {}).get("fullName", "?")
        due = (inv.get("dueAt") or "")[:10]
        lines.append(
            f"  #{inv.get('visualId')} | {inv.get('nickname', '')} | "
            f"${inv.get('total', 0)} | {status} | {contact} | Due: {due} | "
            f"Internal ID: {inv.get('id')}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_order_details(visual_id: str) -> str:
    """
    Get detailed information about a specific Printavo order.
    visual_id: the order number shown in the Printavo UI (e.g. '6817')
    """
    q = """
    query($q: String) {
        invoices(first: 5, query: $q) {
            nodes {
                id
                visualId
                nickname
                total
                customerDueAt
                dueAt
                startAt
                invoiceAt
                visualPoNumber
                status { name }
                contact { fullName email }
                productionFiles { nodes { id name } }
            }
        }
        quotes(first: 5, query: $q) {
            nodes {
                id
                visualId
                nickname
                total
                customerDueAt
                dueAt
                startAt
                invoiceAt
                visualPoNumber
                status { name }
                contact { fullName email }
                productionFiles { nodes { id name } }
            }
        }
    }
    """
    result = query_printavo(q, {"q": str(visual_id)}, allow_partial=True)
    if "error" in result:
        return f"Error: {result['error']}"
    invoice_nodes = result.get("invoices", {}).get("nodes", [])
    quote_nodes   = result.get("quotes",   {}).get("nodes", [])
    all_nodes = invoice_nodes + quote_nodes
    matching = [n for n in all_nodes if str(n.get("visualId")) == str(visual_id)]
    if not matching:
        return f"Order #{visual_id} not found."
    inv = matching[0]
    contact = inv.get("contact") or {}
    status  = (inv.get("status") or {}).get("name", "?")
    lines = [
        f"ORDER #{inv.get('visualId')} | {inv.get('nickname', '')}",
        f"  Customer:       {contact.get('fullName', '?')}",
        f"  Status:         {status}",
        f"  Total:          ${inv.get('total', 0)}",
        f"  PO #:           {inv.get('visualPoNumber', '')}",
        f"  Customer Due:   {inv.get('customerDueAt', '')}",
        f"  Production Date:{(inv.get('dueAt') or inv.get('startAt') or '')[:10]}",
        f"  Invoice Date:   {inv.get('invoiceAt', '')}",
        f"  Internal ID:    {inv.get('id')}",
    ]
    prod_files = (inv.get("productionFiles") or {}).get("nodes", [])
    if prod_files:
        lines.append(f"\n  Production Files ({len(prod_files)}):")
        for pf in prod_files:
            lines.append(f"    - {pf.get('name', 'unnamed')} (ID: {pf.get('id')})")
    else:
        lines.append("\n  Production Files: none")
    lines.append("\n  (Use get_invoice_structure for line items and imprints)")
    return "\n".join(lines)


@mcp.tool()
def get_statuses() -> str:
    """List all available order statuses in Printavo."""
    q = """
    query {
        statuses(first: 50) {
            nodes { id name color }
        }
    }
    """
    result = query_printavo(q)
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("statuses", {}).get("nodes", [])
    if not nodes:
        return "No statuses found."
    lines = ["AVAILABLE STATUSES:"]
    for s in nodes:
        lines.append(f"  ID: {s.get('id')} | Name: {s.get('name')} | Color: {s.get('color','')}")
    return "\n".join(lines)


@mcp.tool()
def get_outstanding_balances(limit: int = 20) -> str:
    """Get orders with outstanding balances (unpaid invoices)."""
    q = """
    query($first: Int) {
        invoices(first: $first, query: "balance_due > 0") {
            nodes {
                visualId
                nickname
                total
                amountPaid
                dueAt
                contact { fullName }
                status { name }
            }
        }
    }
    """
    result = query_printavo(q, {"first": limit})
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("invoices", {}).get("nodes", [])
    if not nodes:
        return "No outstanding balances found."
    lines = [f"OUTSTANDING BALANCES ({len(nodes)} orders):"]
    total_outstanding = 0.0
    for inv in nodes:
        total_val = float(inv.get("total") or 0)
        paid_val  = float(inv.get("amountPaid") or 0)
        balance   = total_val - paid_val
        total_outstanding += balance
        contact   = inv.get("contact") or {}
        lines.append(
            f"  #{inv.get('visualId')} | {inv.get('nickname','')} | "
            f"Balance: ${balance:.2f} | Total: ${total_val:.2f} | "
            f"Customer: {contact.get('fullName','?')} | Due: {(inv.get('dueAt') or '')[:10]}"
        )
    lines.append(f"\nTotal Outstanding: ${total_outstanding:.2f}")
    return "\n".join(lines)


@mcp.tool()
def create_quote(customer_email: str, order_name: str, due_date: str) -> str:
    """
    Create a new Printavo quote for an existing customer.
    customer_email: customer's email address in Printavo
    order_name: nickname/label for the order
    due_date: ISO date string e.g. '2026-06-19'
    """
    cq = """
    query($email: String) {
        contacts(first: 5, query: $email) {
            nodes { id fullName email }
        }
    }
    """
    cresult = query_printavo(cq, {"email": customer_email})
    if "error" in cresult:
        return f"Error finding customer: {cresult['error']}"
    contacts = cresult.get("contacts", {}).get("nodes", [])
    if not contacts:
        return f"No customer found with email '{customer_email}'."
    contact_id = contacts[0]["id"]
    mutation = """
    mutation($contactId: ID!, $nickname: String, $dueAt: ISO8601DateTime) {
        quoteCreate(input: { contactId: $contactId, nickname: $nickname, dueAt: $dueAt }) {
            quote { id visualId nickname dueAt }
            errors { message }
        }
    }
    """
    result = query_printavo(mutation, {
        "contactId": contact_id,
        "nickname":  order_name,
        "dueAt":     f"{due_date}T12:00:00Z",
    })
    if "error" in result:
        return f"Error: {result['error']}"
    qc = result.get("quoteCreate", {})
    errors = qc.get("errors", [])
    if errors:
        return f"Printavo error: {[e.get('message') for e in errors]}"
    quote = qc.get("quote", {})
    return (
        f"Quote created!\n"
        f"  Order #: {quote.get('visualId')}\n"
        f"  Nickname: {quote.get('nickname')}\n"
        f"  Due: {(quote.get('dueAt') or '')[:10]}\n"
        f"  Internal ID: {quote.get('id')}"
    )


@mcp.tool()
def inspect_fields(type_name: str) -> str:
    """
    Inspect the GraphQL fields available on a Printavo type.
    type_name: GraphQL type name (e.g. 'Invoice', 'LineItem', 'Imprint', 'Contact')
    """
    q = """
    query($name: String!) {
        __type(name: $name) {
            name kind
            fields {
                name
                type { name kind ofType { name kind } }
                args { name type { name kind ofType { name kind } } }
            }
            inputFields {
                name
                type { name kind ofType { name kind } }
            }
        }
    }
    """
    result = query_printavo(q, {"name": type_name})
    if "error" in result:
        return f"Error: {result['error']}"
    t = result.get("__type")
    if not t:
        return f"Type '{type_name}' not found. Try: Invoice, LineItem, LineItemGroup, Imprint, Contact, Status, PricingMatrix, PricingMatrixColumn"
    lines = [f"TYPE: {t.get('name')} ({t.get('kind')})"]
    fields = t.get("fields") or []
    if fields:
        lines.append("\nFIELDS:")
        for f in fields:
            ftype = f.get("type") or {}
            tname = ftype.get("name") or ((ftype.get("ofType") or {}).get("name"))
            lines.append(f"  {f.get('name')}: {tname or ftype.get('kind','?')}")
    input_fields = t.get("inputFields") or []
    if input_fields:
        lines.append("\nINPUT FIELDS:")
        for f in input_fields:
            ftype = f.get("type") or {}
            tname = ftype.get("name") or ((ftype.get("ofType") or {}).get("name"))
            lines.append(f"  {f.get('name')}: {tname or ftype.get('kind','?')}")
    return "\n".join(lines)


@mcp.tool()
def diagnose_order(visual_id: str) -> str:
    """
    Run a full diagnostic on a Printavo order — checks all key fields,
    imprint pricing, line items, and flags any issues.
    visual_id: order number shown in Printavo UI
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return f"Error: {err}"
    frag = """
                id visualId nickname total customerDueAt startAt invoiceAt visualPoNumber
                status { name }
                contact { fullName email }
                lineItemGroups {
                    nodes {
                        id
                        lineItems {
                            nodes {
                                id itemNumber description color price
                                sizes { size count }
                            }
                        }
                        imprints {
                            nodes {
                                id
                                pricingMatrixColumn { id columnName }
                            }
                        }
                    }
                }
                productionFiles { nodes { id name } }
    """
    q = f"""
    query($id: ID!) {{
        {order_type}(id: $id) {{ {frag} }}
    }}
    """
    result = query_printavo(q, {"id": internal_id})
    if "error" in result:
        return f"Error: {result['error']}"
    inv = result.get(order_type)
    if not inv:
        return f"Order #{visual_id} not found."
    issues = []
    if not inv.get("nickname"):      issues.append("⚠ MISSING: nickname")
    if not inv.get("visualPoNumber"): issues.append("⚠ MISSING: PO number")
    if not inv.get("customerDueAt"): issues.append("⚠ MISSING: customer due date")
    if not inv.get("startAt"):       issues.append("⚠ MISSING: production date")
    if not inv.get("invoiceAt"):     issues.append("⚠ MISSING: invoice date")
    groups = (inv.get("lineItemGroups") or {}).get("nodes", [])
    if not groups:
        issues.append("⚠ MISSING: no line item groups")
    else:
        for gi, g in enumerate(groups, 1):
            items = (g.get("lineItems") or {}).get("nodes", [])
            if not items:
                issues.append(f"⚠ Group {gi}: no line items")
            for item in items:
                total_qty = sum((s.get("count") or 0) for s in (item.get("sizes") or []))
                if total_qty == 0:
                    issues.append(f"⚠ Line item {item.get('id')}: no sizes/qty")
                if not item.get("price") or float(item.get("price") or 0) == 0:
                    issues.append(f"⚠ Line item {item.get('id')}: $0 price (pricing not set?)")
            imprints = (g.get("imprints") or {}).get("nodes", [])
            if not imprints:
                issues.append(f"⚠ Group {gi}: no imprints")
            for imp in imprints:
                col = (imp.get("pricingMatrixColumn") or {})
                if not col.get("id"):
                    issues.append(f"⚠ Imprint {imp.get('id')}: no pricing matrix column set")
    contact = inv.get("contact") or {}
    status  = (inv.get("status") or {}).get("name", "?")
    prod_files = (inv.get("productionFiles") or {}).get("nodes", [])
    lines = [
        f"DIAGNOSTIC — Order #{inv.get('visualId')} | {inv.get('nickname','')}",
        f"  Status: {status} | Total: ${inv.get('total',0)}",
        f"  Customer: {contact.get('fullName','?')}",
        f"  Due: {inv.get('customerDueAt', '')} | "
        f"Prod: {(inv.get('startAt') or '')[:10]} | "
        f"Invoice: {inv.get('invoiceAt', '')}",
        f"  PO #: {inv.get('visualPoNumber','')}",
        f"  Production Files: {len(prod_files)}",
        f"  Line Item Groups: {len(groups)}",
    ]
    if issues:
        lines.append(f"\nISSUES FOUND ({len(issues)}):")
        for issue in issues:
            lines.append(f"  {issue}")
    else:
        lines.append("\n✓ No issues found — order looks complete.")
    return "\n".join(lines)


# ── FIXED: get_production_schedule ────────────────────────────────────────────
# BUG (original): the query never fetched lineItemGroups/imprints at all,
# so Items and Imprints were always 0 for every order.
#
# FIX: two-step approach
#   Step 1 — get order list with minimal fields (avoids complexity limit)
#   Step 2 — per order, call invoice(id) → lineItemGroups → imprints
#             invoice(id) is a single-object query and reliably traverses
#             nested .nodes without hitting the 25k complexity ceiling.

@mcp.tool()
def get_production_schedule(days_ahead: int = 7) -> str:
    """
    Get the production schedule — orders due in the next N days, with full
    imprint, screen count, and estimated production time per order.
    days_ahead: how many days ahead to look (default 7)
    """
    now      = datetime.now(timezone.utc)
    end_date = now + timedelta(days=days_ahead)
    start_str = now.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")

    # ── Step 1: lightweight list query (no nested collections) ──────────────
    # Uses inProductionAfter/inProductionBefore named args — confirmed working
    # in direct GraphQL testing. The query-string `production_at >=` syntax
    # is unreliable; named params are the correct approach.
    q_list = """
    query($after: ISO8601Date, $before: ISO8601Date, $first: Int) {
        invoices(inProductionAfter: $after, inProductionBefore: $before, first: $first) {
            nodes {
                id visualId nickname total
                dueAt startAt
                status { name }
                contact { fullName }
            }
        }
    }
    """
    result = query_printavo(q_list, {"after": start_str, "before": end_str, "first": 25})
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("invoices", {}).get("nodes", [])
    if not nodes:
        return f"No orders scheduled for production in the next {days_ahead} days."

    # ── Step 2: per-order imprint fetch via invoice(id) ─────────────────────
    # Uses _fetch_imprints_for_order() which queries invoice(id) — a single-
    # object query — so it reliably gets lineItemGroups.nodes even for orders
    # in ART APPROVAL SENT, PRINT READY, and other non-contract statuses.

    today_str = now.strftime("%Y-%m-%d")
    lines = [
        f"PRODUCTION SCHEDULE — {start_str} to {end_str}",
        f"  {len(nodes)} orders | Generated {today_str}\n",
    ]

    for inv in nodes:
        internal_id = inv.get("id")
        status  = (inv.get("status") or {}).get("name", "?")
        contact = (inv.get("contact") or {}).get("fullName", "Unknown")
        prod_dt = (inv.get("startAt") or "")[:10]
        due_dt  = (inv.get("dueAt") or "")[:10]

        # Header line
        lines.append(f"#{inv.get('visualId')} | {contact} | {inv.get('nickname', '')}")

        # Fetch imprint data
        fetch_result = _fetch_imprints_for_order(internal_id) if internal_id else None
        if fetch_result is None:
            lines.append(
                f"  Status: {status} | Prod: {prod_dt} | Due: {due_dt} | "
                f"⚠️ could not fetch imprint data"
            )
            lines.append("")
            continue

        imprint_nodes, total_qty = fetch_result

        if not imprint_nodes:
            # Order is in schedule but has no imprints set in Printavo
            lines.append(
                f"  Status: {status} | Items: {total_qty} | "
                f"Total Imprints: 0 | Screens: ⚠️ not entered | "
                f"Est. Time: ⚠️ unknown"
            )
            if total_qty > 0:
                lines.append(f"  ⚠️ {total_qty} pcs — no imprints entered")
            lines.append("")
            continue

        # ── Calculate totals ─────────────────────────────────────────────────
        num_locations  = len(imprint_nodes)
        total_imprints = total_qty * num_locations
        total_screens  = sum(i["colors"] for i in imprint_nodes)

        # Group by decoration type for per-type breakdown
        type_groups: dict = defaultdict(lambda: {"locations": 0, "screens": 0})
        for imp in imprint_nodes:
            tg = type_groups[imp["type"]]
            tg["locations"] += 1
            tg["screens"]   += imp["colors"]

        # Estimate time (SP only; EMB/DTF get flat 15 min/location rough est.)
        sp_qty    = total_qty * type_groups.get("Screen Print", {}).get("locations", 0)
        sp_colors = type_groups.get("Screen Print", {}).get("screens", 0)
        sp_run_min = int((sp_qty * (sp_colors or 1)) / 350 * 60) if sp_qty else 0
        emb_dtf_min = (
            type_groups.get("Embroidery", {}).get("locations", 0) +
            type_groups.get("DTF", {}).get("locations", 0)
        ) * 15
        setup_min  = num_locations * 30
        total_min  = setup_min + sp_run_min + emb_dtf_min
        est_time   = _format_est_time(total_min)

        screens_str = str(total_screens) if total_screens > 0 else "N/A"
        lines.append(
            f"  Status: {status} | Items: {total_qty} | "
            f"Total Imprints: {total_imprints} | Screens: {screens_str} | "
            f"Est. Time: {est_time}"
        )

        # Per-decoration-type breakdown
        for deco_type, tg in type_groups.items():
            locs        = tg["locations"]
            type_total  = total_qty * locs
            scr         = tg["screens"]
            detail = f"  {deco_type} | {total_qty} pcs × {locs} imprint(s) = {type_total} imprints"
            if scr > 0:
                detail += f" | {scr} screens"
            detail += f" | {est_time}"
            lines.append(detail)

        lines.append("")  # blank line between orders

    return "\n".join(lines)


@mcp.tool()
def send_schedule_to_slack(days_ahead: int = 7) -> str:
    """Post the production schedule to the configured Slack channel."""
    if not SLACK_WEBHOOK_URL:
        return "Error: SLACK_WEBHOOK_URL environment variable not set."
    schedule_text = get_production_schedule(days_ahead)
    payload = {
        "text": f"*Printavo Production Schedule*\n```{schedule_text}```"
    }
    response = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
    if response.status_code == 200:
        return "Schedule posted to Slack successfully."
    return f"Slack error: HTTP {response.status_code} — {response.text}"


# ── FIXED: get_production_time_estimate ───────────────────────────────────────
# BUG (original): used invoices(query) list query with nested lineItemGroups.
# List queries with deeply nested nodes hit the 25k complexity ceiling for
# some orders (especially those not yet in CONTRACT status), returning
# empty lineItemGroups and therefore 0 imprints.
#
# FIX: use _find_order() to get the internal ID, then invoice(id) for
# the nested data — same reliable single-object pattern used everywhere else.

@mcp.tool()
def get_production_time_estimate(visual_id: str) -> str:
    """
    Estimate production time for an order based on quantity, imprint count, and decoration type.
    visual_id: order number shown in Printavo UI
    """
    internal_id, _order_type, err = _find_order(visual_id)
    if err:
        return f"Error finding order: {err}"

    # Fetch basic info
    q_basic = """
    query($id: ID!) {
        invoice(id: $id) {
            visualId nickname totalQuantity
            lineItemGroups {
                nodes {
                    imprints {
                        nodes {
                            typeOfWork { name }
                            pricingMatrixColumn { columnName matrix { name } }
                        }
                    }
                }
            }
        }
    }
    """
    result = query_printavo(q_basic, {"id": internal_id})
    if "error" in result:
        return f"Error: {result['error']}"

    inv = result.get("invoice")
    if not inv:
        return f"Order #{visual_id} not found."

    total_qty = int(inv.get("totalQuantity") or 0)
    groups = inv.get("lineItemGroups", {}).get("nodes", [])
    imprint_info = []
    for g in groups:
        for imp in g.get("imprints", {}).get("nodes", []):
            imprint_info.append({
                "type":   _decoration_type(imp),
                "colors": _color_count(imp),
            })

    total_prints = len(imprint_info)

    if total_qty > 0 and imprint_info:
        sp_items = [(i["colors"] or 1) for i in imprint_info if i["type"] == "Screen Print"]
        sp_run_min  = int(sum(total_qty * c for c in sp_items) / 350 * 60) if sp_items else 0
        emb_dtf_min = sum(15 for i in imprint_info if i["type"] in ("Embroidery", "DTF"))
        setup_min   = total_prints * 30
        total_min   = setup_min + sp_run_min + emb_dtf_min
    else:
        total_min = 0

    lines = [
        f"PRODUCTION TIME ESTIMATE — Order #{inv.get('visualId')} | {inv.get('nickname','')}",
        f"  Total Quantity:  {total_qty} pcs",
        f"  Print Locations: {total_prints}",
        f"  Imprint Details: {imprint_info}",
        f"  TOTAL ESTIMATE:  {_format_est_time(total_min)} ({total_min} min)",
        f"  Note: Estimates based on ~350 SP impressions/hr. Actual times vary.",
    ]
    return "\n".join(lines)


# ── PRIVATE HELPERS (not tools) ───────────────────────────────────────────────

def _find_invoice_internal_id(visual_id: str):
    internal_id, _order_type, err = _find_order(visual_id)
    return internal_id, err


def _find_order(visual_id: str):
    """Returns (internal_id, order_type, error_string). Searches invoices and quotes."""
    q = """
    query($q: String) {
        invoices(first: 5, query: $q) {
            nodes { id visualId }
        }
        quotes(first: 5, query: $q) {
            nodes { id visualId }
        }
    }
    """
    result = query_printavo(q, {"q": str(visual_id)}, allow_partial=True)
    if "error" in result:
        return None, None, f"API Error: {result['error']}"
    invoice_nodes = result.get("invoices", {}).get("nodes", [])
    quote_nodes   = result.get("quotes",   {}).get("nodes", [])
    for n in invoice_nodes:
        if str(n.get("visualId")) == str(visual_id):
            return n["id"], "invoice", None
    for n in quote_nodes:
        if str(n.get("visualId")) == str(visual_id):
            return n["id"], "quote", None
    return None, None, f"Order #{visual_id} not found in invoices or quotes."


def _get_status_id_by_name(status_name: str):
    q = """query { statuses(first: 50) { nodes { id name } } }"""
    result = query_printavo(q)
    if "error" in result:
        return None, f"API Error: {result['error']}"
    statuses = result.get("statuses", {}).get("nodes", [])
    for s in statuses:
        if s.get("name", "").lower().strip() == status_name.lower().strip():
            return s["id"], None
    names = [s.get("name") for s in statuses]
    return None, f"Status '{status_name}' not found. Available: {names}"


def _find_pricing_matrix_column_id(color_count: int):
    q = """
    query {
        account {
            pricingMatrices(first: 50) {
                nodes {
                    id name
                    columns { id columnName }
                }
            }
        }
    }
    """
    result = query_printavo(q)
    if "error" in result:
        return None, f"API Error: {result['error']}"
    matrices = result.get("account", {}).get("pricingMatrices", {}).get("nodes", [])
    target = None
    for m in matrices:
        if "contract sp 2025" in m.get("name", "").lower():
            target = m
            break
    if not target:
        names = [m.get("name") for m in matrices]
        return None, f"'Contract SP 2025 (NEW)' matrix not found. Available: {names}"
    columns = target.get("columns", [])
    for col in columns:
        if re.search(rf'\b{color_count}\s+[Cc]olor', col.get("columnName", "")):
            return col["id"], None
    col_names = [c.get("columnName") for c in columns]
    return None, f"No {color_count}-color column found in matrix '{target.get('name')}'. Columns: {col_names}"


def _upload_file_rest(endpoint: str, file_path: str, extra_data: dict) -> dict:
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}
    with open(file_path, "rb") as f:
        filename = os.path.basename(file_path)
        response = httpx.post(
            f"https://www.printavo.com/api/v2/{endpoint}",
            data=extra_data,
            files={"file": (filename, f, "application/octet-stream")},
            headers={"email": EMAIL, "token": TOKEN},
            timeout=60,
        )
    if response.status_code in (200, 201):
        try:
            return response.json()
        except Exception:
            return {"success": True, "status": response.status_code}
    return {"error": f"HTTP {response.status_code}: {response.text[:400]}"}


def _normalize_size_key(size_str: str) -> str:
    size_map = {
        "XS": "size_xs", "S": "size_s", "M": "size_m", "L": "size_l",
        "XL": "size_xl", "2XL": "size_2xl", "XXL": "size_2xl",
        "3XL": "size_3xl", "XXXL": "size_3xl", "4XL": "size_4xl",
        "5XL": "size_5xl", "6XL": "size_6xl",
        "WXS": "size_wxs", "WS": "size_ws", "WM": "size_wm",
        "WL": "size_wl", "WXL": "size_wxl", "W2XL": "size_w2xl",
    }
    return size_map.get(size_str.strip().upper(), f"size_{size_str.strip().lower()}")


# ── NEW TOOLS: UGP QUOTE ENTRY WORKFLOW ───────────────────────────────────────

@mcp.tool()
def duplicate_invoice(source_visual_id: str) -> str:
    """
    Duplicate a Printavo invoice (used to copy template order 6817 for each UGP order).
    source_visual_id: the order number to duplicate (e.g. '6817')
    """
    internal_id, err = _find_invoice_internal_id(source_visual_id)
    if err:
        return err
    mutation = """
    mutation($id: ID!) {
        invoiceDuplicate(id: $id) {
            id visualId nickname
        }
    }
    """
    result = query_printavo(mutation, {"id": internal_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    invoice = result.get("invoiceDuplicate", {})
    if not invoice:
        return f"Unexpected response — 'invoiceDuplicate' key missing. Raw: {result}"
    return (
        f"Invoice duplicated!\n"
        f"New Order # (visual): {invoice.get('visualId')}\n"
        f"Internal ID: {invoice.get('id')}\n"
        f"Nickname: {invoice.get('nickname', '')}\n"
        f"NEXT: Run delete_production_files('{invoice.get('visualId')}') "
        f"to remove inherited artwork from the template."
    )


@mcp.tool()
def update_invoice_fields(
    visual_id: str,
    nickname: str,
    po_number: str,
    production_date: str,
    customer_due_date: str,
    invoice_date: str,
) -> str:
    """
    Update the header fields on a Printavo invoice.
    visual_id: order number shown in Printavo UI (e.g. '6999')
    nickname: e.g. 'Minot AF Ball Tees - 1151454'
    po_number: UGP order number as PO (e.g. '1151454')
    production_date: YYYY-MM-DD
    customer_due_date: YYYY-MM-DD
    invoice_date: YYYY-MM-DD
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    update_field = "quoteUpdate" if order_type == "quote" else "invoiceUpdate"
    if order_type == "quote":
        mutation = f"""
        mutation($id: ID!, $nickname: String, $visualPoNumber: String, $dueAt: ISO8601DateTime, $customerDueAt: ISO8601Date) {{
            {update_field}(id: $id, input: {{
                nickname: $nickname, visualPoNumber: $visualPoNumber,
                dueAt: $dueAt, customerDueAt: $customerDueAt
            }}) {{
                id visualId nickname visualPoNumber customerDueAt dueAt startAt invoiceAt
            }}
        }}
        """
        variables = {
            "id": internal_id, "nickname": nickname,
            "visualPoNumber": po_number, "dueAt": f"{production_date}T12:00:00Z",
            "customerDueAt": customer_due_date,
        }
    else:
        mutation = f"""
        mutation($id: ID!, $nickname: String, $visualPoNumber: String, $startAt: ISO8601DateTime, $customerDueAt: ISO8601Date, $invoiceAt: ISO8601Date) {{
            {update_field}(id: $id, input: {{
                nickname: $nickname, visualPoNumber: $visualPoNumber,
                startAt: $startAt, customerDueAt: $customerDueAt, invoiceAt: $invoiceAt
            }}) {{
                id visualId nickname visualPoNumber customerDueAt startAt invoiceAt
            }}
        }}
        """
        variables = {
            "id": internal_id, "nickname": nickname, "visualPoNumber": po_number,
            "startAt": f"{production_date}T12:00:00Z", "customerDueAt": customer_due_date,
            "invoiceAt": invoice_date,
        }
    result = query_printavo(mutation, variables)
    if "error" in result:
        return f"API Error: {result['error']}"
    inv = result.get(update_field, {})
    prod_date = (inv.get('dueAt') or inv.get('startAt') or '')[:10]
    return (
        f"Order #{inv.get('visualId')} header updated!\n"
        f"  Nickname:          {inv.get('nickname')}\n"
        f"  PO #:              {inv.get('visualPoNumber')}\n"
        f"  Production Date:   {prod_date}\n"
        f"  Customer Due Date: {inv.get('customerDueAt', '')}\n"
        f"  Invoice Date:      {inv.get('invoiceAt', '')}"
    )


@mcp.tool()
def get_invoice_structure(visual_id: str) -> str:
    """
    Get all line item group IDs, line item IDs, and imprint IDs for an invoice.
    visual_id: order number shown in Printavo UI
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err
    fragment = """
        visualId nickname
        productionFiles { nodes { id name } }
        lineItemGroups {
            nodes {
                id
                lineItems {
                    nodes {
                        id itemNumber description color
                        sizes { size count }
                    }
                }
                imprints {
                    nodes {
                        id
                        pricingMatrixColumn { id columnName }
                    }
                }
            }
        }
    """
    q = f"""
    query($id: ID!) {{
        invoice(id: $id) {{ {fragment} }}
        quote(id: $id) {{ {fragment} }}
    }}
    """
    result = query_printavo(q, {"id": internal_id}, allow_partial=True)
    if "error" in result:
        return f"API Error: {result['error']}"
    inv = result.get("invoice") or result.get("quote") or {}
    if not inv:
        return f"Order #{visual_id} not found via direct ID lookup."
    lines = [f"STRUCTURE — Order #{inv.get('visualId')} | {inv.get('nickname', '')}"]
    prod_files = (inv.get("productionFiles") or {}).get("nodes", [])
    lines.append(f"\nProduction Files ({len(prod_files)}):")
    for pf in prod_files:
        lines.append(f"  ID: {pf.get('id')} | {pf.get('name', 'unnamed')}")
    groups = (inv.get("lineItemGroups") or {}).get("nodes", [])
    lines.append(f"\nLine Item Groups ({len(groups)}):")
    for gi, g in enumerate(groups, 1):
        lines.append(f"\n  Group {gi} | ID: {g.get('id')}")
        items = (g.get("lineItems") or {}).get("nodes", [])
        for ii, item in enumerate(items, 1):
            sizes_str = ", ".join(
                f"{s.get('size','?').replace('size_','').upper()}:{s.get('count',0)}"
                for s in (item.get("sizes") or [])
                if (s.get("count") or 0) > 0
            )
            lines.append(
                f"    Line Item {ii} | ID: {item.get('id')} | "
                f"#{item.get('itemNumber','?')} | {item.get('color','?')} | "
                f"{(item.get('description') or '')[:60]}"
            )
            if sizes_str:
                lines.append(f"      Sizes: {sizes_str}")
        imprints = (g.get("imprints") or {}).get("nodes", [])
        for ii, imp in enumerate(imprints, 1):
            col = imp.get("pricingMatrixColumn") or {}
            lines.append(
                f"    Imprint {ii} | ID: {imp.get('id')} | "
                f"Column: {col.get('columnName', 'none')} (Col ID: {col.get('id', 'none')})"
            )
    return "\n".join(lines)


@mcp.tool()
def delete_production_files(visual_id: str) -> str:
    """
    Delete ALL inherited files from a Printavo order after duplicating template 6817.
    visual_id: order number shown in Printavo UI
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    type_field = order_type
    q = f"""
    query($id: ID!) {{
        {type_field}(id: $id) {{
            productionFiles {{ nodes {{ id name }} }}
            lineItemGroups {{ nodes {{ lineItems {{ nodes {{ id mockups {{ nodes {{ id }} }} }} }} }} }}
        }}
    }}
    """
    result = query_printavo(q, {"id": internal_id}, allow_partial=True)
    if "error" in result:
        return f"API Error: {result['error']}"
    obj = result.get(type_field) or {}
    files = obj.get("productionFiles", {}).get("nodes", [])
    del_pf_mutation = """mutation($id: ID!) { productionFileDelete(id: $id) { id } }"""
    deleted_files, failed_files = [], []
    for pf in files:
        dr = query_printavo(del_pf_mutation, {"id": pf["id"]})
        (failed_files if "error" in dr else deleted_files).append(pf.get("name", pf["id"]))
    mockups_to_delete = []
    for g in (obj.get("lineItemGroups") or {}).get("nodes", []):
        for item in (g.get("lineItems") or {}).get("nodes", []):
            for m in (item.get("mockups") or {}).get("nodes", []):
                mockups_to_delete.append(m["id"])
    del_mockup_mutation = """mutation($id: ID!) { mockupDelete(id: $id) { id } }"""
    deleted_mockups, failed_mockups = [], []
    for mid in mockups_to_delete:
        dr = query_printavo(del_mockup_mutation, {"id": mid})
        (failed_mockups if "error" in dr else deleted_mockups).append(mid)
    lines = [
        f"Order #{visual_id} — inherited files cleared:",
        f"  Production files: {len(deleted_files)}/{len(files)} deleted",
    ]
    for name in deleted_files:
        lines.append(f"    ✓ {name}")
    lines.append(f"  Line item mockups: {len(deleted_mockups)}/{len(mockups_to_delete)} deleted")
    if failed_files:   lines.append(f"  FAILED files: {failed_files}")
    if failed_mockups: lines.append(f"  FAILED mockups: {failed_mockups}")
    return "\n".join(lines)


@mcp.tool()
def update_line_item(
    line_item_id: str,
    color: str,
    description: str,
    sizes_json: str,
    item_number: str = "SCRN",
) -> str:
    """
    Update a line item's item number, color, description, and size quantities.
    line_item_id: internal GraphQL ID of the line item
    color: garment color
    description: multi-line description
    sizes_json: JSON object e.g. '{"S": 12, "M": 12}'
    item_number: always 'SCRN' for screen print orders (default)
    """
    try:
        sizes_dict = json.loads(sizes_json)
    except Exception as e:
        return f"Invalid sizes_json: {e}"
    STANDARD_SIZES = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL"]
    normalized_input = {k.strip().upper(): int(v) for k, v in sizes_dict.items()}
    full_sizes = {s: normalized_input.get(s, 0) for s in STANDARD_SIZES}
    for k, v in normalized_input.items():
        if k not in STANDARD_SIZES:
            full_sizes[k] = v
    sizes_gql = ", ".join(
        f'{{size: {_normalize_size_key(k)}, count: {v}}}'
        for k, v in full_sizes.items()
    )
    mutation = f"""
    mutation($id: ID!, $itemNumber: String, $color: String, $description: String) {{
        lineItemUpdate(id: $id, input: {{
            itemNumber: $itemNumber, color: $color, description: $description,
            position: 1, sizes: [{sizes_gql}]
        }}) {{
            id itemNumber description color
            sizes {{ size count }}
        }}
    }}
    """
    result = query_printavo(mutation, {
        "id": line_item_id, "itemNumber": item_number,
        "color": color, "description": description,
    })
    if "error" in result:
        return f"API Error: {result['error']}"
    item = result.get("lineItemUpdate", {})
    sizes_summary = ", ".join(
        f"{s.get('size')}:{s.get('count')}"
        for s in (item.get("sizes") or [])
        if (s.get("count") or 0) > 0
    ) or ", ".join(f"{k}:{v}" for k, v in sizes_dict.items() if int(v) > 0)
    return (
        f"Line item updated!\n"
        f"  ID: {item.get('id')}\n"
        f"  Item #: {item.get('itemNumber')}\n"
        f"  Color: {item.get('color')}\n"
        f"  Description: {(item.get('description') or '')[:100]}\n"
        f"  Sizes: {sizes_summary}"
    )


@mcp.tool()
def duplicate_line_item(line_item_id: str) -> str:
    """
    Duplicate a line item within its line item group.
    line_item_id: internal GraphQL ID of the line item to duplicate
    """
    fetch_q = """
    query($id: ID!) {
        lineItem(id: $id) {
            id itemNumber color description position
            lineItemGroup { id }
            sizes { size count }
        }
    }
    """
    src = query_printavo(fetch_q, {"id": line_item_id})
    if "error" in src:
        return f"API Error fetching source line item: {src['error']}"
    src_item = src.get("lineItem")
    if not src_item:
        return f"Line item {line_item_id} not found."
    group_id = (src_item.get("lineItemGroup") or {}).get("id")
    if not group_id:
        return "Could not determine line item group ID."
    sizes_gql = ", ".join(
        f'{{size: {s["size"]}, count: {s["count"] or 0}}}'
        for s in (src_item.get("sizes") or [])
    )
    next_pos = (src_item.get("position") or 1) + 1
    create_mutation = f"""
    mutation($groupId: ID!, $itemNumber: String, $color: String, $description: String) {{
        lineItemCreate(lineItemGroupId: $groupId, input: {{
            itemNumber: $itemNumber, color: $color, description: $description,
            position: {next_pos}, sizes: [{sizes_gql}]
        }}) {{
            id itemNumber description color
            sizes {{ size count }}
        }}
    }}
    """
    result = query_printavo(create_mutation, {
        "groupId": group_id, "itemNumber": src_item.get("itemNumber", "SCRN"),
        "color": src_item.get("color", ""), "description": src_item.get("description", ""),
    })
    if "error" in result:
        return f"API Error: {result['error']}"
    item = result.get("lineItemCreate")
    if not item:
        return f"Unexpected response — 'lineItemCreate' key missing. Raw: {result}"
    return (
        f"Line item duplicated!\n"
        f"New Line Item ID: {item.get('id')}\n"
        f"  Color: {item.get('color')}\n"
        f"  Description: {(item.get('description') or '')[:80]}\n"
        f"Now call update_line_item(line_item_id='{item.get('id')}', ...) to set new product details."
    )


@mcp.tool()
def set_imprint_pricing(imprint_id: str, color_count: int) -> str:
    """
    Set an imprint's pricing to 'Contract SP 2025 (NEW)' matrix with the given color count.
    imprint_id: internal GraphQL ID of the imprint
    color_count: number of ink colors (1–6)
    """
    col_id, err = _find_pricing_matrix_column_id(color_count)
    if err:
        return f"Could not find pricing column: {err}"
    mutation = """
    mutation($id: ID!, $colId: ID!) {
        imprintUpdate(id: $id, input: { pricingMatrixColumn: { id: $colId } }) {
            id pricingMatrixColumn { id columnName }
        }
    }
    """
    result = query_printavo(mutation, {"id": imprint_id, "colId": col_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    imp = result.get("imprintUpdate", {})
    col_name = (imp.get("pricingMatrixColumn") or {}).get("columnName", "?")
    return f"Imprint {imprint_id} pricing set to: {col_name}"


@mcp.tool()
def add_imprint(line_item_group_id: str, color_count: int) -> str:
    """
    Add a new imprint row to a line item group and set its pricing matrix.
    line_item_group_id: internal GraphQL ID of the line item group
    color_count: number of ink colors (1–6)
    """
    col_id, err = _find_pricing_matrix_column_id(color_count)
    if err:
        return f"Could not find pricing column: {err}"
    mutation = """
    mutation($groupId: ID!, $colId: ID!) {
        imprintCreate(lineItemGroupId: $groupId, input: { pricingMatrixColumn: { id: $colId } }) {
            id pricingMatrixColumn { id columnName }
        }
    }
    """
    result = query_printavo(mutation, {"groupId": line_item_group_id, "colId": col_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    cr = result.get("imprintCreate", {})
    if not cr:
        return f"Unexpected response — 'imprintCreate' key missing.\nRaw: {result}"
    col_name = (cr.get("pricingMatrixColumn") or {}).get("columnName", "?")
    return (
        f"Imprint added!\n"
        f"New Imprint ID: {cr.get('id')}\n"
        f"Matrix column: {col_name}"
    )


@mcp.tool()
def refresh_invoice_pricing(visual_id: str) -> str:
    """
    Verify current pricing on an order.
    visual_id: order number shown in Printavo UI
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    q = f"""
    query($id: ID!) {{
        {order_type}(id: $id) {{ id visualId total }}
    }}
    """
    result = query_printavo(q, {"id": internal_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    inv = result.get(order_type, {})
    total = inv.get("total", "?")
    if not total or float(total or 0) == 0:
        return (
            f"Order #{inv.get('visualId')} total is $0 — pricing may not be set.\n"
            f"Verify imprint matrix columns are assigned via set_imprint_pricing."
        )
    return f"Order #{inv.get('visualId')} current total: ${total} ✓"


@mcp.tool()
def set_order_status(visual_id: str, status_name: str) -> str:
    """
    Set a Printavo order's status by name.
    visual_id: order number shown in Printavo UI
    status_name: exact status name — use get_statuses() to see all available names.
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    status_id, err = _get_status_id_by_name(status_name)
    if err:
        return err
    mutation = """
    mutation($parentId: ID!, $statusId: ID!) {
        statusUpdate(parentId: $parentId, statusId: $statusId) {
            ... on Quote   { id visualId status { name } }
            ... on Invoice { id visualId status { name } }
        }
    }
    """
    result = query_printavo(mutation, {"parentId": internal_id, "statusId": status_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    inv = result.get("statusUpdate", {})
    new_status = (inv.get("status") or {}).get("name", "?")
    return f"Order #{inv.get('visualId')} status → {new_status}"


@mcp.tool()
def upload_production_file(visual_id: str, file_path: str) -> str:
    """
    Upload a file to a Printavo order as a production file.
    visual_id: order number shown in Printavo UI
    file_path: https:// URL to the file
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err
    if not file_path.startswith("http"):
        return "file_path must be an https:// URL."
    mutation = """
    mutation($parentId: ID!, $url: String!) {
        productionFileCreate(parentId: $parentId, publicFileUrl: $url) {
            id name
        }
    }
    """
    result = query_printavo(mutation, {"parentId": internal_id, "url": file_path})
    if "error" in result:
        return f"API Error: {result['error']}"
    pf = result.get("productionFileCreate", {})
    filename = file_path.split("/")[-1].split("?")[0]
    return f"Production file uploaded: {pf.get('name', filename)} (ID: {pf.get('id')}) → Order #{visual_id}"


@mcp.tool()
def attach_mockup_to_order(visual_id: str, file_path: str) -> str:
    """
    Attach a PDF spec sheet as a mockup to the first line item of an order.
    visual_id: order number shown in Printavo UI
    file_path: https:// URL to the PDF
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    if not file_path.startswith("http"):
        return "file_path must be an https:// URL."
    q = f"""
    query($id: ID!) {{
        {order_type}(id: $id) {{
            lineItemGroups {{ nodes {{ lineItems {{ nodes {{ id }} }} }} }}
        }}
    }}
    """
    result = query_printavo(q, {"id": internal_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    groups = (result.get(order_type) or {}).get("lineItemGroups", {}).get("nodes", [])
    if not groups:
        return f"No line item groups found on order #{visual_id}."
    items = (groups[0].get("lineItems") or {}).get("nodes", [])
    if not items:
        return f"No line items found in first group of order #{visual_id}."
    line_item_id = items[0]["id"]
    mutation = """
    mutation($lineItemId: ID!, $url: String!) {
        lineItemMockupCreate(lineItemId: $lineItemId, publicImageUrl: $url) {
            id fullImageUrl
        }
    }
    """
    upload_result = query_printavo(mutation, {"lineItemId": line_item_id, "url": file_path})
    if "error" in upload_result:
        return f"Mockup upload failed: {upload_result['error']}"
    mockup = upload_result.get("lineItemMockupCreate", {})
    filename = file_path.split("/")[-1].split("?")[0]
    return f"Mockup attached: {filename} → Order #{visual_id} (Mockup ID: {mockup.get('id')})"


@mcp.tool()
def list_available_mutations() -> str:
    """List all GraphQL mutations available in the Printavo API."""
    q = """
    query {
        __schema {
            mutationType { fields { name } }
        }
    }
    """
    result = query_printavo(q)
    if "error" in result:
        return f"API Error: {result['error']}"
    mt = result.get("__schema", {}).get("mutationType", {})
    if not mt:
        return "Could not retrieve mutation list."
    fields = sorted(f.get("name") for f in mt.get("fields", []))
    return "AVAILABLE MUTATIONS:\n" + "\n".join(f"  {n}" for n in fields)


# ── DAILY SLACK SCHEDULER ─────────────────────────────────────────────────────

def _is_us_federal_holiday(dt: datetime) -> bool:
    month, day, weekday = dt.month, dt.day, dt.weekday()
    if month == 1  and day == 1:                           return True
    if month == 1  and weekday == 0 and 15 <= day <= 21:  return True
    if month == 2  and weekday == 0 and 15 <= day <= 21:  return True
    if month == 5  and weekday == 0 and day >= 25:        return True
    if month == 6  and day == 19:                          return True
    if month == 7  and day == 4:                           return True
    if month == 9  and weekday == 0 and day <= 7:         return True
    if month == 11 and weekday == 3 and 22 <= day <= 28:  return True
    if month == 12 and day == 25:                          return True
    return False


def run_daily_scheduler():
    """Background thread: post production schedule to Slack at 6am CST weekdays."""
    CST_OFFSET = timedelta(hours=-6)
    while True:
        now_cst = datetime.now(timezone.utc) + CST_OFFSET
        if now_cst.weekday() < 5 and not _is_us_federal_holiday(now_cst):
            target = now_cst.replace(hour=6, minute=0, second=0, microsecond=0)
            if now_cst >= target and now_cst < target + timedelta(minutes=5):
                try:
                    send_schedule_to_slack(days_ahead=7)
                except Exception:
                    pass
        time.sleep(300)


scheduler_thread = threading.Thread(target=run_daily_scheduler, daemon=True)
scheduler_thread.start()

if __name__ == "__main__":
    _port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", host="0.0.0.0", port=_port)
