from fastmcp import FastMCP
import httpx
import os

mcp = FastMCP("Printavo Assistant")

EMAIL = os.environ.get("PRINTAVO_EMAIL", "")
TOKEN = os.environ.get("PRINTAVO_TOKEN", "")
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


# ---- TOOL 1: Get Recent Orders ----
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


# ---- TOOL 2: Search by Customer Name ----
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


# ---- TOOL 3: Get Order Details ----
@mcp.tool()
def get_order_details(order_number: str) -> str:
    """Get full details on a specific order including style, color, and size quantities."""
    query = """
    query($q: String) {
        invoices(first: 1, query: $q) {
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
                                sizes {
                                    count
                                    size
                                }
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
    if not invoices:
        return f"Order #{order_number} not found."
    o = invoices[0]
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
                lines.append(f"    Sizes: None entered")
    return "\n".join(lines)


# ---- TOOL 4: Get All Statuses ----
@mcp.tool()
def get_statuses() -> str:
    """Get all order statuses configured in your Printavo account"""
    query = """
    query {
        statuses(first: 25) {
            nodes {
                id
                name
                color
            }
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


# ---- TOOL 5: Outstanding Balances ----
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


# ---- TOOL 6: Create a Quote ----
@mcp.tool()
def create_quote(customer_email: str, order_name: str, due_date: str) -> str:
    """
    Create a new quote in Printavo.
    customer_email: must already exist as a contact in Printavo
    order_name: nickname for this job (e.g. 'Spring 2026 Tees')
    due_date: format YYYY-MM-DD (e.g. 2026-05-15)
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


# ---- TOOL 7: Inspect API Field Names (Diagnostic) ----
@mcp.tool()
def inspect_fields(type_name: str) -> str:
    """Look up the exact field names on any Printavo API type. Try: Invoice, LineItemGroup, LineItem"""
    query = """
    query($typeName: String!) {
        __type(name: $typeName) {
            name
            fields {
                name
                type {
                    name
                    kind
                }
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


# ---- TOOL 8: Production Schedule ----
@mcp.tool()
def get_production_schedule(date: str) -> str:
    """
    Get all orders scheduled for production on a given date with imprint counts by type.
    date: format YYYY-MM-DD (e.g. 2026-05-01)
    """
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
            pageInfo {
                hasNextPage
                endCursor
            }
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

        nodes = (result.get("invoices") or {}).get("nodes", [])
        page_info = (result.get("invoices") or {}).get("pageInfo", {})

        for o in nodes:
            start = (o.get("startAt") or "")[:10]
            if start == date:
                all_invoices.append(o)
                found_any = True
                consecutive_misses = 0
            elif found_any and start < date:
                consecutive_misses += 1

        if consecutive_misses >= 25:
            break
        if not page_info.get("hasNextPage"):
            break

        cursor = page_info.get("endCursor")
        pages_searched += 1

    if not all_invoices:
        return f"No orders scheduled for production on {date}."

    imprint_query = """
    query($id: ID!) {
        invoice(id: $id) {
            lineItemGroups {
                nodes {
                    imprints {
                        nodes {
                            details
                            typeOfWork { name }
                        }
                    }
                }
            }
        }
    }
    """

    lines = [f"PRODUCTION SCHEDULE — {date}", ""]
    grand_total_qty = 0
    grand_total_imprints = 0
    breakdown_by_type = {}

    for o in sorted(all_invoices, key=lambda x: x.get("visualId", 0)):
        try:
            qty = int(o.get("totalQuantity") or 0)
            customer = (o.get("contact") or {}).get("fullName") or "Unknown"
            nickname = o.get("nickname") or ""
            status_name = (o.get("status") or {}).get("name") or "?"
            invoice_id = o.get("id")
            is_store_order = "store" in status_name.lower()
            grand_total_qty += qty

            if is_store_order:
                lines.append(f"  #{o.get('visualId')} | {customer} | {nickname}")
                lines.append(f"    Status: {status_name} | Items: {qty}")
                lines.append(f"    → Store Order (InkSoft) — pieces only, imprints not counted")
                breakdown_by_type["Store Order (pieces only)"] = breakdown_by_type.get("Store Order (pieces only)", 0) + qty
                lines.append("")
                continue

            all_imprints = []
            if invoice_id:
                imprint_result = query_printavo(imprint_query, {"id": invoice_id})
                if "error" not in imprint_result:
                    invoice_data = imprint_result.get("invoice") or {}
                    groups = (invoice_data.get("lineItemGroups") or {}).get("nodes") or []
                    for group in groups:
                        imprints = (group.get("imprints") or {}).get("nodes") or []
                        all_imprints.extend(imprints)

            num_locations = len(all_imprints)
            order_imprints = qty * num_locations
            grand_total_imprints += order_imprints

            lines.append(f"  #{o.get('visualId')} | {customer} | {nickname}")
            lines.append(f"    Status: {status_name} | Items: {qty} | Locations: {num_locations} | Imprints: {order_imprints}")

            for imp in all_imprints:
                type_of_work = (imp.get("typeOfWork") or {}).get("name") or ""
                details = imp.get("details") or ""
                details_lower = details.lower()

                if type_of_work and type_of_work.lower() not in ["unknown", ""]:
                    dec_type = type_of_work
                elif "dtf" in details_lower:
                    dec_type = "DTF"
                elif "embroid" in details_lower:
                    dec_type = "Embroidery"
                elif "screenprint" in details_lower or "screen print" in details_lower:
                    dec_type = "Screen Print"
                elif "emb" in details_lower:
                    dec_type = "Embroidery"
                elif "scrn" in details_lower:
                    dec_type = "Screen Print"
                else:
                    dec_type = "Screen Print"

                lines.append(f"    → {dec_type}: {details[:80] if details else '(no detail)'}")
                breakdown_by_type[dec_type] = breakdown_by_type.get(dec_type, 0) + qty

            lines.append("")

        except Exception as e:
            lines.append(f"  [Skipped #{o.get('visualId')} due to error: {str(e)}]")
            lines.append("")

    lines.append("─" * 50)
    lines.append(f"TOTALS FOR {date}:")
    lines.append(f"  Orders on schedule: {len(all_invoices)}")
    lines.append(f"  Total Items: {grand_total_qty}")
    lines.append(f"  Total Imprints (excl. store orders): {grand_total_imprints}")
    if breakdown_by_type:
        lines.append("")
        lines.append("  BY TYPE:")
        for type_name, count in sorted(breakdown_by_type.items()):
            label = "pieces" if "store" in type_name.lower() else "imprints"
            lines.append(f"    {type_name}: {count} {label}")

    return "\n".join(lines)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
