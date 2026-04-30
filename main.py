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
    """Get the most recent orders/invoices from Printavo"""
    query = """
    query {
        invoices(first: %d, sortDescending: true) {
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
        return "No invoices found. Check API credentials."
    lines = [f"RECENT {len(invoices)} ORDERS:"]
    for o in invoices:
        lines.append(
            f"  #{o.get('visualId')} | {o.get('contact', {}).get('fullName', 'Unknown')} | "
            f"Total: ${o.get('total', 0)} | "
            f"Status: {o.get('status', {}).get('name', '?')} | Due: {o.get('dueAt', 'N/A')}"
        )
    return "\n".join(lines)


# ---- TOOL 2: Search by Customer Name ----
@mcp.tool()
def search_orders(customer_name: str) -> str:
    """Search for orders by customer name"""
    query = """
    query($q: String) {
        invoices(first: 20, query: $q) {
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
        lines.append(
            f"  #{o.get('visualId')} | ${o.get('total', 0)} | "
            f"Status: {o.get('status', {}).get('name', '?')} | Due: {o.get('dueAt', 'N/A')}"
        )
    return "\n".join(lines)


# ---- TOOL 3: Get Full Order Details ----
@mcp.tool()
def get_order_details(order_number: str) -> str:
    """Get full details on a specific order. Use the visual order number like 1042."""
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
                        name
                        lineItems {
                            nodes {
                                name
                                quantity
                                unitPrice
                                total
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
    lines = [
        f"ORDER #{o.get('visualId')} — {o.get('nickname', '')}",
        f"Customer: {o.get('contact', {}).get('fullName')} | {o.get('contact', {}).get('email')} | {o.get('contact', {}).get('phone')}",
        f"Status: {o.get('status', {}).get('name')} | Due: {o.get('dueAt')}",
        f"Total: ${o.get('total')}",
        "",
        "LINE ITEMS:"
    ]
    for group in o.get("lineItemGroups", {}).get("nodes", []):
        lines.append(f"  Group: {group.get('name', '')}")
        for item in group.get("lineItems", {}).get("nodes", []):
            lines.append(
                f"    - {item.get('name')} | Qty: {item.get('quantity')} | "
                f"${item.get('unitPrice')} ea | Total: ${item.get('total')}"
            )
    return "\n".join(lines)


# ---- TOOL 4: Outstanding Balances ----
@mcp.tool()
def get_outstanding_balances() -> str:
    """Get open orders that are not yet marked paid"""
    query = """
    query {
        invoices(first: 25, sortDescending: true) {
            nodes {
                visualId
                total
                dueAt
                contact { fullName email phone }
                status { name }
            }
        }
    }
    """
    result = query_printavo(query)
    if "error" in result:
        return f"API Error: {result['error']}"
    invoices = result.get("invoices", {}).get("nodes", [])
    paid_keywords = ["paid", "done", "complete", "cancelled", "canceled"]
    unpaid = [
        o for o in invoices
        if not any(kw in (o.get("status", {}).get("name", "").lower()) for kw in paid_keywords)
    ]
    if not unpaid:
        return "No open/unpaid orders found."
    total_outstanding = sum(float(o.get("total") or 0) for o in unpaid)
    lines = [f"OPEN ORDERS — {len(unpaid)} orders | Gross value: ${total_outstanding:.2f}", ""]
    for o in unpaid:
        lines.append(
            f"  #{o.get('visualId')} | {o.get('contact', {}).get('fullName')} | "
            f"Total: ${float(o.get('total') or 0):.2f} | Due: {o.get('dueAt', 'N/A')[:10] if o.get('dueAt') else 'N/A'} | "
            f"Status: {o.get('status', {}).get('name')} | "
            f"Phone: {o.get('contact', {}).get('phone', 'N/A')}"
        )
    return "\n".join(lines)


# ---- TOOL 5: Create a Quote ----
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
        f"For: {contact['fullName']} | Due: {quote.get('dueAt')}"
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
