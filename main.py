from fastmcp import FastMCP
import httpx
import os
import re
import math
import json
import threading
import time
from datetime import datetime, timezone, timedelta

mcp = FastMCP("Printavo Assistant")

EMAIL            = os.environ.get("PRINTAVO_EMAIL", "")
TOKEN            = os.environ.get("PRINTAVO_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
API_URL          = "https://www.printavo.com/api/v2"


# ── CORE HELPER ───────────────────────────────────────────────────────────────

def query_printavo(query: str, variables: dict = None):
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
    if "errors" in data:
        return {"error": data["errors"]}
    return data.get("data", {})


# ── EXISTING TOOLS ────────────────────────────────────────────────────────────

@mcp.tool()
def get_recent_orders(limit: int = 10) -> str:
    """Get the most recent Printavo orders."""
    q = """
    query($first: Int) {
        invoices(first: $first, sortOn: UPDATED_AT, sortDirection: DESC) {
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
    result = query_printavo(q, {"q": str(visual_id)})
    if "error" in result:
        return f"Error: {result['error']}"

    nodes = result.get("invoices", {}).get("nodes", [])
    matching = [n for n in nodes if str(n.get("visualId")) == str(visual_id)]
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
        f"  Due Date:       {(inv.get('dueAt') or '')[:10]}",
        f"  Production Date:{(inv.get('startAt') or '')[:10]}",
        f"  Invoice Date:   {(inv.get('invoiceAt') or '')[:10]}",
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
    # Find customer's contact ID
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
    Useful for discovering what fields can be queried or mutated.
    type_name: GraphQL type name (e.g. 'Invoice', 'LineItem', 'Imprint', 'Contact')
    """
    q = """
    query($name: String!) {
        __type(name: $name) {
            name
            kind
            fields {
                name
                type {
                    name
                    kind
                    ofType { name kind }
                }
                args {
                    name
                    type { name kind ofType { name kind } }
                }
            }
            inputFields {
                name
                type {
                    name
                    kind
                    ofType { name kind }
                }
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
    q = """
    query($q: String) {
        invoices(first: 5, query: $q) {
            nodes {
                id visualId nickname total dueAt startAt invoiceAt visualPoNumber
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
                                typeOfWork { name }
                                pricingMatrixColumn { id columnName }
                            }
                        }
                    }
                }
                productionFiles { nodes { id name } }
                mockups { nodes { id fileName } }
            }
        }
    }
    """
    result = query_printavo(q, {"q": str(visual_id)})
    if "error" in result:
        return f"Error: {result['error']}"

    nodes = result.get("invoices", {}).get("nodes", [])
    matching = [n for n in nodes if str(n.get("visualId")) == str(visual_id)]
    if not matching:
        return f"Order #{visual_id} not found."

    inv    = matching[0]
    issues = []

    # Check header fields
    if not inv.get("nickname"):
        issues.append("⚠ MISSING: nickname")
    if not inv.get("visualPoNumber"):
        issues.append("⚠ MISSING: PO number")
    if not inv.get("dueAt"):
        issues.append("⚠ MISSING: due date")
    if not inv.get("startAt"):
        issues.append("⚠ MISSING: production date")
    if not inv.get("invoiceAt"):
        issues.append("⚠ MISSING: invoice date")

    groups = (inv.get("lineItemGroups") or {}).get("nodes", [])
    if not groups:
        issues.append("⚠ MISSING: no line item groups")
    else:
        for gi, g in enumerate(groups, 1):
            items = (g.get("lineItems") or {}).get("nodes", [])
            if not items:
                issues.append(f"⚠ Group {gi}: no line items")
            for item in items:
                total_qty = sum(s.get("count", 0) for s in (item.get("sizes") or []))
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

    # Summarize
    contact = inv.get("contact") or {}
    status  = (inv.get("status") or {}).get("name", "?")
    prod_files = (inv.get("productionFiles") or {}).get("nodes", [])
    mockups    = (inv.get("mockups") or {}).get("nodes", [])

    lines = [
        f"DIAGNOSTIC — Order #{inv.get('visualId')} | {inv.get('nickname','')}",
        f"  Status: {status} | Total: ${inv.get('total',0)}",
        f"  Customer: {contact.get('fullName','?')}",
        f"  Due: {(inv.get('dueAt') or '')[:10]} | "
        f"Prod: {(inv.get('startAt') or '')[:10]} | "
        f"Invoice: {(inv.get('invoiceAt') or '')[:10]}",
        f"  PO #: {inv.get('visualPoNumber','')}",
        f"  Production Files: {len(prod_files)} | Mockups: {len(mockups)}",
        f"  Line Item Groups: {len(groups)}",
    ]
    if issues:
        lines.append(f"\nISSUES FOUND ({len(issues)}):")
        for issue in issues:
            lines.append(f"  {issue}")
    else:
        lines.append("\n✓ No issues found — order looks complete.")

    return "\n".join(lines)


@mcp.tool()
def get_production_schedule(days_ahead: int = 7) -> str:
    """
    Get the production schedule — orders due in the next N days.
    days_ahead: how many days ahead to look (default 7)
    """
    now       = datetime.now(timezone.utc)
    end_date  = now + timedelta(days=days_ahead)
    start_str = now.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")

    q = """
    query($q: String, $first: Int) {
        invoices(first: $first, query: $q, sortOn: PRODUCTION_AT, sortDirection: ASC) {
            nodes {
                visualId nickname total
                dueAt startAt
                status { name }
                contact { fullName }
                lineItemGroups {
                    nodes {
                        lineItems {
                            nodes {
                                description color
                                sizes { size count }
                            }
                        }
                        imprints {
                            nodes {
                                typeOfWork { name }
                                pricingMatrixColumn { columnName }
                            }
                        }
                    }
                }
            }
        }
    }
    """
    search_q = f"production_at >= {start_str} production_at <= {end_str}"
    result   = query_printavo(q, {"q": search_q, "first": 50})
    if "error" in result:
        return f"Error: {result['error']}"

    nodes = result.get("invoices", {}).get("nodes", [])
    if not nodes:
        return f"No orders scheduled for production in the next {days_ahead} days."

    lines = [f"PRODUCTION SCHEDULE — Next {days_ahead} days ({start_str} to {end_str}):"]
    lines.append(f"Total orders: {len(nodes)}\n")

    for inv in nodes:
        contact  = inv.get("contact") or {}
        status   = (inv.get("status") or {}).get("name", "?")
        prod_dt  = (inv.get("startAt") or "")[:10]
        due_dt   = (inv.get("dueAt") or "")[:10]

        lines.append(
            f"#{inv.get('visualId')} | {inv.get('nickname','')} | "
            f"{contact.get('fullName','?')} | Status: {status} | "
            f"Prod: {prod_dt} | Due: {due_dt}"
        )

        groups = (inv.get("lineItemGroups") or {}).get("nodes", [])
        for g in groups:
            items = (g.get("lineItems") or {}).get("nodes", [])
            for item in items:
                qty = sum(s.get("count", 0) for s in (item.get("sizes") or []))
                lines.append(f"  {item.get('color','?')} | {item.get('description','')[:60]} | Qty: {qty}")

            imprints = (g.get("imprints") or {}).get("nodes", [])
            for imp in imprints:
                tow = (imp.get("typeOfWork") or {}).get("name", "?")
                col = (imp.get("pricingMatrixColumn") or {}).get("columnName", "?")
                lines.append(f"  Imprint: {tow} | {col}")
        lines.append("")

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


@mcp.tool()
def get_production_time_estimate(visual_id: str) -> str:
    """
    Estimate production time for an order based on quantity, imprint count, and decoration type.
    visual_id: order number shown in Printavo UI
    """
    q = """
    query($q: String) {
        invoices(first: 5, query: $q) {
            nodes {
                visualId nickname
                lineItemGroups {
                    nodes {
                        lineItems {
                            nodes { sizes { size count } }
                        }
                        imprints {
                            nodes {
                                typeOfWork { name }
                                pricingMatrixColumn { columnName }
                            }
                        }
                    }
                }
            }
        }
    }
    """
    result = query_printavo(q, {"q": str(visual_id)})
    if "error" in result:
        return f"Error: {result['error']}"

    nodes = result.get("invoices", {}).get("nodes", [])
    matching = [n for n in nodes if str(n.get("visualId")) == str(visual_id)]
    if not matching:
        return f"Order #{visual_id} not found."

    inv = matching[0]
    groups = (inv.get("lineItemGroups") or {}).get("nodes", [])

    total_qty    = 0
    total_prints = 0
    imprint_info = []

    for g in groups:
        items = (g.get("lineItems") or {}).get("nodes", [])
        for item in items:
            total_qty += sum(s.get("count", 0) for s in (item.get("sizes") or []))

        imprints = (g.get("imprints") or {}).get("nodes", [])
        total_prints += len(imprints)
        for imp in imprints:
            tow = (imp.get("typeOfWork") or {}).get("name", "?")
            col = (imp.get("pricingMatrixColumn") or {}).get("columnName", "?")
            # Extract color count from column name like "Contract SP 2025 (NEW) • 3 Color"
            color_match = re.search(r'(\d+)\s+[Cc]olor', col)
            num_colors = int(color_match.group(1)) if color_match else 1
            imprint_info.append({"type": tow, "colors": num_colors})

    # Rough estimates: ~350 pcs/hour per color per screen print location
    setup_time_hrs = total_prints * 0.5  # 30 min setup per screen
    if total_qty > 0 and imprint_info:
        avg_colors = sum(i["colors"] for i in imprint_info) / len(imprint_info)
        run_time_hrs = (total_qty * avg_colors) / 350
    else:
        run_time_hrs = 0

    total_hrs = setup_time_hrs + run_time_hrs

    lines = [
        f"PRODUCTION TIME ESTIMATE — Order #{inv.get('visualId')} | {inv.get('nickname','')}",
        f"  Total Quantity:  {total_qty} pcs",
        f"  Print Locations: {total_prints}",
        f"  Imprint Details: {imprint_info}",
        f"  Setup Time:      {setup_time_hrs:.1f} hrs",
        f"  Run Time:        {run_time_hrs:.1f} hrs",
        f"  TOTAL ESTIMATE:  {total_hrs:.1f} hrs ({math.ceil(total_hrs * 60)} min)",
        f"  Note: Estimates based on ~350 impressions/hr. Actual times vary.",
    ]
    return "\n".join(lines)


# ── PRIVATE HELPERS (not tools) ───────────────────────────────────────────────

def _find_invoice_internal_id(visual_id: str):
    """Returns (internal_id, error_string). One will be None.
    Searches both invoices and quotes (duplicates create quotes)."""
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
    result = query_printavo(q, {"q": str(visual_id)})
    if "error" in result:
        return None, f"API Error: {result['error']}"
    nodes = (
        result.get("invoices", {}).get("nodes", []) +
        result.get("quotes", {}).get("nodes", [])
    )
    matching = [n for n in nodes if str(n.get("visualId")) == str(visual_id)]
    if not matching:
        return None, f"Order #{visual_id} not found in invoices or quotes."
    return matching[0]["id"], None


def _get_status_id_by_name(status_name: str):
    """Returns (status_id, error_string). One will be None."""
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
    """
    Find the pricing matrix column ID for 'Contract SP 2025 (NEW)' + N colors.
    Returns (column_id, error_string). One will be None.
    """
    q = """
    query {
        pricingMatrices(first: 50) {
            nodes {
                id
                name
                pricingMatrixColumns(first: 20) {
                    nodes { id columnName }
                }
            }
        }
    }
    """
    result = query_printavo(q)
    if "error" in result:
        return None, f"API Error: {result['error']}"

    matrices = result.get("pricingMatrices", {}).get("nodes", [])
    target = None
    for m in matrices:
        if "contract sp 2025" in m.get("name", "").lower():
            target = m
            break
    if not target:
        names = [m.get("name") for m in matrices]
        return None, f"'Contract SP 2025 (NEW)' matrix not found. Available: {names}"

    columns = target.get("pricingMatrixColumns", {}).get("nodes", [])
    for col in columns:
        col_name = col.get("columnName", "")
        if re.search(rf'\b{color_count}\s+[Cc]olor', col_name):
            return col["id"], None

    col_names = [c.get("columnName") for c in columns]
    return None, f"No {color_count}-color column found in matrix '{target.get('name')}'. Columns: {col_names}"


def _upload_file_rest(endpoint: str, file_path: str, extra_data: dict) -> dict:
    """Upload a file via REST multipart POST to Printavo API."""
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
    """Convert common size abbreviations to Printavo's internal format."""
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
    Returns the new invoice's visual ID and internal ID.
    source_visual_id: the order number to duplicate (e.g. '6817')
    """
    internal_id, err = _find_invoice_internal_id(source_visual_id)
    if err:
        return err

    mutation = """
    mutation($id: ID!) {
        invoiceDuplicate(id: $id) {
            id
            visualId
            nickname
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
    production_date: YYYY-MM-DD  (2 business days before due date)
    customer_due_date: YYYY-MM-DD  (carrier date from UGP)
    invoice_date: YYYY-MM-DD  (same as production_date)
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err

    mutation = """
    mutation(
        $id: ID!,
        $nickname: String,
        $visualPoNumber: String,
        $startAt: ISO8601DateTime,
        $dueAt: ISO8601DateTime,
        $invoiceAt: ISO8601DateTime
    ) {
        invoiceUpdate(input: {
            id: $id,
            nickname: $nickname,
            visualPoNumber: $visualPoNumber,
            startAt: $startAt,
            dueAt: $dueAt,
            invoiceAt: $invoiceAt
        }) {
            invoice { id visualId nickname visualPoNumber dueAt startAt invoiceAt }
            errors { message }
        }
    }
    """
    variables = {
        "id":             internal_id,
        "nickname":       nickname,
        "visualPoNumber": po_number,
        "startAt":        f"{production_date}T12:00:00Z",
        "dueAt":          f"{customer_due_date}T12:00:00Z",
        "invoiceAt":      f"{invoice_date}T12:00:00Z",
    }
    result = query_printavo(mutation, variables)
    if "error" in result:
        return f"API Error: {result['error']}"

    upd = result.get("invoiceUpdate", {})
    errors = upd.get("errors", [])
    if errors:
        return f"Printavo error: {[e.get('message') for e in errors]}"

    inv = upd.get("invoice", {})
    return (
        f"Invoice #{inv.get('visualId')} header updated!\n"
        f"  Nickname:          {inv.get('nickname')}\n"
        f"  PO #:              {inv.get('visualPoNumber')}\n"
        f"  Production Date:   {(inv.get('startAt') or '')[:10]}\n"
        f"  Customer Due Date: {(inv.get('dueAt') or '')[:10]}\n"
        f"  Invoice Date:      {(inv.get('invoiceAt') or '')[:10]}"
    )


@mcp.tool()
def get_invoice_structure(visual_id: str) -> str:
    """
    Get all line item group IDs, line item IDs, and imprint IDs for an invoice.
    Call this after duplicating to get the IDs needed for update_line_item,
    set_imprint_pricing, attach_mockup_to_order, etc.
    visual_id: order number shown in Printavo UI
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err

    fragment = """
        visualId
        nickname
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
    result = query_printavo(q, {"id": internal_id})
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
    Delete all production files from a Printavo invoice.
    Call after duplicating template 6817 to remove inherited artwork.
    visual_id: order number shown in Printavo UI
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err

    q = """
    query($id: ID!) {
        invoice(id: $id) { productionFiles { nodes { id name } } }
        quote(id: $id) { productionFiles { nodes { id name } } }
    }
    """
    result = query_printavo(q, {"id": internal_id})
    if "error" in result:
        return f"API Error: {result['error']}"

    obj = result.get("invoice") or result.get("quote") or {}
    files = obj.get("productionFiles", {}).get("nodes", [])
    if not files:
        return f"No production files found on order #{visual_id}. Nothing to delete."

    delete_mutation = """
    mutation($id: ID!) {
        productionFileDelete(input: { id: $id }) {
            deletedId
            errors { message }
        }
    }
    """
    deleted = []
    failed  = []
    for pf in files:
        dr = query_printavo(delete_mutation, {"id": pf["id"]})
        if "error" in dr:
            failed.append(f"{pf.get('name', pf['id'])}: {dr['error']}")
        else:
            dd = dr.get("productionFileDelete", {})
            errs = dd.get("errors", [])
            if errs:
                failed.append(f"{pf.get('name', pf['id'])}: {[e.get('message') for e in errs]}")
            else:
                deleted.append(pf.get("name", pf["id"]))

    lines = [f"Deleted {len(deleted)}/{len(files)} production file(s) from order #{visual_id}:"]
    for name in deleted:
        lines.append(f"  ✓ {name}")
    if failed:
        lines.append(f"\nFailed ({len(failed)}):")
        for f in failed:
            lines.append(f"  ✗ {f}")
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
    Use get_invoice_structure to get the line_item_id first.

    line_item_id: internal GraphQL ID of the line item
    color: garment color (e.g. 'Royal', 'Black', 'White')
    description: multi-line. Line 1: garment description (e.g. '5000G - Gildan Heavy Cotton Basic T-Shirt')
                 Line 2+: imprint abbreviations, one per line (e.g. '1C FF\\n1C FB')
    sizes_json: JSON object e.g. '{"S": 12, "M": 12, "L": 12, "XL": 12, "2XL": 12}'
    item_number: always 'SCRN' for screen print orders (default)
    """
    try:
        sizes_dict = json.loads(sizes_json)
    except Exception as e:
        return f"Invalid sizes_json — could not parse JSON: {e}\nExpected: '{{\"S\": 12, \"M\": 10}}'"

    sizes_input = [
        {"size": _normalize_size_key(k), "count": int(v)}
        for k, v in sizes_dict.items()
        if int(v) > 0
    ]

    mutation = """
    mutation($id: ID!, $itemNumber: String, $color: String, $description: String, $sizes: [SizeInput!]) {
        lineItemUpdate(input: {
            id: $id,
            itemNumber: $itemNumber,
            color: $color,
            description: $description,
            sizes: $sizes
        }) {
            lineItem { id itemNumber description color }
            errors { message }
        }
    }
    """
    variables = {
        "id":          line_item_id,
        "itemNumber":  item_number,
        "color":       color,
        "description": description,
        "sizes":       sizes_input,
    }
    result = query_printavo(mutation, variables)
    if "error" in result:
        return f"API Error: {result['error']}"

    upd = result.get("lineItemUpdate", {})
    errors = upd.get("errors", [])
    if errors:
        return f"Printavo error: {[e.get('message') for e in errors]}"

    item = upd.get("lineItem", {})
    sizes_summary = ", ".join(
        f"{k.upper()}:{v}" for k, v in sizes_dict.items() if int(v) > 0
    )
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
    Use for multi-product orders: duplicate the first item, then update the copy.
    Returns the new line item ID to use with update_line_item.
    line_item_id: internal GraphQL ID of the line item to duplicate
    """
    mutation = """
    mutation($id: ID!) {
        lineItemDuplicate(input: { lineItemId: $id }) {
            lineItem { id description color itemNumber }
            errors { message }
        }
    }
    """
    result = query_printavo(mutation, {"id": line_item_id})
    if "error" in result:
        return f"API Error: {result['error']}"

    dup = result.get("lineItemDuplicate", {})
    if not dup:
        return (
            f"Unexpected response — 'lineItemDuplicate' key missing.\n"
            f"Raw: {result}\n"
            f"Run list_available_mutations() to verify the mutation name."
        )

    errors = dup.get("errors", [])
    if errors:
        return f"Printavo error: {[e.get('message') for e in errors]}"

    item = dup.get("lineItem", {})
    if not item:
        return f"No lineItem returned. Full result: {result}"

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
    Use get_invoice_structure to get the imprint_id first.
    imprint_id: internal GraphQL ID of the imprint
    color_count: number of ink colors for this imprint (1, 2, 3, 4, 5, or 6)
    """
    col_id, err = _find_pricing_matrix_column_id(color_count)
    if err:
        return f"Could not find pricing column: {err}"

    mutation = """
    mutation($id: ID!, $pricingMatrixColumnId: ID!) {
        imprintUpdate(input: {
            id: $id,
            pricingMatrixColumnId: $pricingMatrixColumnId
        }) {
            imprint { id pricingMatrixColumn { id columnName } }
            errors { message }
        }
    }
    """
    result = query_printavo(mutation, {"id": imprint_id, "pricingMatrixColumnId": col_id})
    if "error" in result:
        return f"API Error: {result['error']}"

    upd = result.get("imprintUpdate", {})
    if not upd:
        return (
            f"Unexpected response — 'imprintUpdate' key missing.\n"
            f"Raw: {result}\n"
            f"Run list_available_mutations() to verify the mutation name."
        )

    errors = upd.get("errors", [])
    if errors:
        return f"Printavo error: {[e.get('message') for e in errors]}"

    imp = upd.get("imprint", {})
    col_name = (imp.get("pricingMatrixColumn") or {}).get("columnName", "?")
    return f"Imprint {imprint_id} pricing set to: {col_name}"


@mcp.tool()
def add_imprint(line_item_group_id: str, color_count: int) -> str:
    """
    Add a new imprint row to a line item group and set its pricing matrix.
    Use when an order has 2+ print locations (e.g. Full Front + Full Back).
    Use get_invoice_structure to get the line_item_group_id.
    line_item_group_id: internal GraphQL ID of the line item group
    color_count: number of ink colors for this imprint (1-6)
    """
    col_id, err = _find_pricing_matrix_column_id(color_count)
    if err:
        return f"Could not find pricing column: {err}"

    mutation = """
    mutation($groupId: ID!, $pricingMatrixColumnId: ID!) {
        imprintCreate(input: {
            lineItemGroupId: $groupId,
            pricingMatrixColumnId: $pricingMatrixColumnId
        }) {
            imprint { id pricingMatrixColumn { id columnName } }
            errors { message }
        }
    }
    """
    result = query_printavo(mutation, {
        "groupId":               line_item_group_id,
        "pricingMatrixColumnId": col_id,
    })
    if "error" in result:
        return f"API Error: {result['error']}"

    cr = result.get("imprintCreate", {})
    if not cr:
        return (
            f"Unexpected response — 'imprintCreate' key missing.\n"
            f"Raw: {result}\n"
            f"Run list_available_mutations() to verify the mutation name."
        )

    errors = cr.get("errors", [])
    if errors:
        return f"Printavo error: {[e.get('message') for e in errors]}"

    imp = cr.get("imprint", {})
    col_name = (imp.get("pricingMatrixColumn") or {}).get("columnName", "?")
    return (
        f"Imprint added!\n"
        f"New Imprint ID: {imp.get('id')}\n"
        f"Matrix column: {col_name}"
    )


@mcp.tool()
def refresh_invoice_pricing(visual_id: str) -> str:
    """
    Trigger pricing recalculation on an invoice.
    Call after setting all imprint pricing to update line item prices.
    visual_id: order number shown in Printavo UI
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err

    mutation = """
    mutation($id: ID!) {
        invoiceCalculate(input: { invoiceId: $id }) {
            invoice { id visualId total }
            errors { message }
        }
    }
    """
    result = query_printavo(mutation, {"id": internal_id})
    if "error" in result:
        return f"API Error: {result['error']}"

    calc = result.get("invoiceCalculate", {})
    if not calc:
        return (
            f"'invoiceCalculate' mutation not found or returned unexpected response.\n"
            f"Raw: {result}\n"
            f"Run list_available_mutations() to find the correct pricing refresh mutation."
        )

    errors = calc.get("errors", [])
    if errors:
        return f"Printavo error: {[e.get('message') for e in errors]}"

    inv = calc.get("invoice", {})
    return f"Pricing refreshed for order #{inv.get('visualId')} | New total: ${inv.get('total', '?')}"


@mcp.tool()
def set_order_status(visual_id: str, status_name: str) -> str:
    """
    Set a Printavo order's status by name.
    visual_id: order number shown in Printavo UI
    status_name: exact status name e.g. 'Quote Approved', 'Art Approved', 'Quote'
                 Use get_statuses() to see all available names.
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err

    status_id, err = _get_status_id_by_name(status_name)
    if err:
        return err

    mutation = """
    mutation($id: ID!, $statusId: ID!) {
        invoiceUpdate(input: { id: $id, statusId: $statusId }) {
            invoice { id visualId status { name } }
            errors { message }
        }
    }
    """
    result = query_printavo(mutation, {"id": internal_id, "statusId": status_id})
    if "error" in result:
        return f"API Error: {result['error']}"

    upd = result.get("invoiceUpdate", {})
    errors = upd.get("errors", [])
    if errors:
        return f"Printavo error: {[e.get('message') for e in errors]}"

    inv = upd.get("invoice", {})
    new_status = (inv.get("status") or {}).get("name", "?")
    return f"Order #{inv.get('visualId')} status → {new_status}"


@mcp.tool()
def upload_production_file(visual_id: str, file_path: str) -> str:
    """
    Upload a local EPS (or other) file to a Printavo order as a production file.
    visual_id: order number shown in Printavo UI
    file_path: absolute path to the file on the local machine
               e.g. '/Users/a13145/Downloads/Full Front.eps'
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err

    if not os.path.exists(file_path):
        return f"File not found: {file_path}"

    result = _upload_file_rest(
        "production_files",
        file_path,
        {"invoice_id": internal_id},
    )

    if "error" in result:
        return f"Upload failed: {result['error']}"

    filename = os.path.basename(file_path)
    return f"Production file uploaded: {filename} → Order #{visual_id}"


@mcp.tool()
def attach_mockup_to_order(visual_id: str, file_path: str) -> str:
    """
    Attach a local PDF (spec sheet) as a mockup to the first line item group of an order.
    visual_id: order number shown in Printavo UI
    file_path: absolute path to the PDF on the local machine
               e.g. '/Users/a13145/Downloads/spec-1151454.pdf'
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err

    if not os.path.exists(file_path):
        return f"File not found: {file_path}"

    # Get first line item group ID
    q = """
    query($id: ID!) {
        invoice(id: $id) {
            lineItemGroups { nodes { id } }
        }
    }
    """
    result = query_printavo(q, {"id": internal_id})
    if "error" in result:
        return f"API Error: {result['error']}"

    groups = (result.get("invoice") or {}).get("lineItemGroups", {}).get("nodes", [])
    if not groups:
        return f"No line item groups found on order #{visual_id}."

    group_id = groups[0]["id"]

    upload_result = _upload_file_rest(
        "mockups",
        file_path,
        {"line_item_group_id": group_id},
    )

    if "error" in upload_result:
        return f"Mockup upload failed: {upload_result['error']}"

    filename = os.path.basename(file_path)
    return f"Mockup attached: {filename} → Order #{visual_id} (Group ID: {group_id})"


@mcp.tool()
def list_available_mutations() -> str:
    """
    Diagnostic: list all GraphQL mutations available in the Printavo API.
    Use this if a mutation call returns an unexpected 'key missing' error —
    paste the output to verify the correct mutation name.
    """
    q = """
    query {
        __schema {
            mutationType {
                fields { name }
            }
        }
    }
    """
    result = query_printavo(q)
    if "error" in result:
        return f"API Error: {result['error']}"

    mt = result.get("__schema", {}).get("mutationType", {})
    if not mt:
        return "Could not retrieve mutation list — __schema.mutationType not returned."

    fields = sorted(f.get("name") for f in mt.get("fields", []))
    return "AVAILABLE MUTATIONS:\n" + "\n".join(f"  {n}" for n in fields)


# ── DAILY SLACK SCHEDULER ─────────────────────────────────────────────────────

def _is_us_federal_holiday(dt: datetime) -> bool:
    """Returns True if the given date is a major US federal holiday."""
    month, day, weekday = dt.month, dt.day, dt.weekday()
    # New Year's Day
    if month == 1 and day == 1:
        return True
    # MLK Day — 3rd Monday in January
    if month == 1 and weekday == 0 and 15 <= day <= 21:
        return True
    # Presidents Day — 3rd Monday in February
    if month == 2 and weekday == 0 and 15 <= day <= 21:
        return True
    # Memorial Day — last Monday in May
    if month == 5 and weekday == 0 and day >= 25:
        return True
    # Juneteenth
    if month == 6 and day == 19:
        return True
    # Independence Day
    if month == 7 and day == 4:
        return True
    # Labor Day — 1st Monday in September
    if month == 9 and weekday == 0 and day <= 7:
        return True
    # Thanksgiving — 4th Thursday in November
    if month == 11 and weekday == 3 and 22 <= day <= 28:
        return True
    # Christmas
    if month == 12 and day == 25:
        return True
    return False


def run_daily_scheduler():
    """Background thread: post production schedule to Slack at 6am CST weekdays."""
    CST_OFFSET = timedelta(hours=-6)

    while True:
        now_cst = datetime.now(timezone.utc) + CST_OFFSET
        # Only run on weekdays (Mon–Fri)
        if now_cst.weekday() < 5 and not _is_us_federal_holiday(now_cst):
            target = now_cst.replace(hour=6, minute=0, second=0, microsecond=0)
            if now_cst >= target and now_cst < target + timedelta(minutes=5):
                try:
                    send_schedule_to_slack(days_ahead=7)
                except Exception:
                    pass
        time.sleep(300)  # Check every 5 minutes


scheduler_thread = threading.Thread(target=run_daily_scheduler, daemon=True)
scheduler_thread.start()

if __name__ == "__main__":
    _port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", host="0.0.0.0", port=_port)
