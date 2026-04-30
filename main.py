from fastmcp import FastMCP
import httpx
import os

# Create the MCP server — this is the "translator box"
mcp = FastMCP("Printavo Assistant")

# These get filled in from Railway (we set them in Phase 3)
EMAIL = os.environ.get("PRINTAVO_EMAIL", "")
TOKEN = os.environ.get("PRINTAVO_TOKEN", "")
API_URL = "https://www.printavo.com/api/v2"


def query_printavo(query: str, variables: dict = None):
    """Send a request to Printavo and return the result"""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = httpx.post(
        API_URL,
        json=payload,
        params={"email": EMAIL, "token": TOKEN},
        headers={"Content-Type": "application/json"},
        timeout=30
    )
    data = response.json()
    if "errors" in data:
        return {"error": data["errors"]}
    return data.get("data", {})


# ---- TOOL 1: Get Recent Orders ----
@mcp.tool()
def get_recent_orders(limit: int = 10) -> str:
    """Get the most recent orders from Printavo"""
    query = """
    query {
        orders(first: %d, sortOn: CREATED_AT, descending: true) {
            nodes {
                id
                visualId
                nickname
                total
                balance
                dueDate
                productionDate
                status { name }
                contact { fullName email phone }
            }
        }
    }
    """ % limit
    result = query_printavo(query)
    orders = result.get("orders", {}).get("nodes", [])
    if not orders:
        return "No orders found."
    lines = [f"RECENT {len(orders)} ORDERS:"]
    for o in orders:
        lines.append(
            f"  #{o.get('visualId')} | {o.get('contact', {}).get('fullName', 'Unknown')} | "
            f"${o.get('total', 0)} | Balance: ${o.get('balance', 0)} | "
            f"Status: {o.get('status', {}).get('name', '?')} | Due: {o.get('dueDate', 'N/A')}"
        )
    return "\n".join(lines)


# ---- TOOL 2: Search Orders by Customer Name ----
@mcp.tool()
def search_orders(customer_name: str) -> str:
    """Search for orders by customer name"""
    query = """
    query($q: String) {
        orders(first: 20, query: $q) {
            nodes {
                id
                visualId
                nickname
                total
                balance
                dueDate
                status { name }
                contact { fullName email phone }
            }
        }
    }
    """
    result = query_printavo(query, {"q": customer_name})
    orders = result.get("orders", {}).get("nodes", [])
    if not orders:
        return f"No orders found for '{customer_name}'."
    lines = [f"Found {len(orders)} order(s) matching '{customer_name}':"]
    for o in orders:
        lines.append(
            f"  #{o.get('visualId')} | ${o.get('total', 0)} | "
            f"Balance: ${o.get('balance', 0)} | "
            f"Status: {o.get('status', {}).get('name', '?')} | Due: {o.get('dueDate', 'N/A')}"
        )
    return "\n".join(lines)


# ---- TOOL 3: Get Full Order Details ----
@mcp.tool()
def get_order_details(order_number: str) -> str:
    """Get full details on a specific order including line items. Use the order number (like 1042)."""
    query = """
    query($q: String) {
        orders(first: 1, query: $q) {
            nodes {
                id
                visualId
                nickname
                total
                balance
                productionDate
                dueDate
                status { name }
                contact { fullName email phone }
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
    """
    result = query_printavo(query, {"q": order_number})
    orders = result.get("orders", {}).get("nodes", [])
    if not orders:
        return f"Order #{order_number} not found."
    o = orders[0]
    lines = [
        f"ORDER #{o.get('visualId')} — {o.get('nickname', '')}",
        f"Customer: {o.get('contact', {}).get('fullName')} | {o.get('contact', {}).get('email')} | {o.get('contact', {}).get('phone')}",
        f"Status: {o.get('status', {}).get('name')} | Production: {o.get('productionDate')} | Due: {o.get('dueDate')}",
        f"Total: ${o.get('total')} | Balance Owed: ${o.get('balance')}",
        "",
        "LINE ITEMS:"
    ]
    for item in o.get("lineItems", {}).get("nodes", []):
        lines.append(
            f"  - {item.get('name')} | Qty: {item.get('quantity')} | "
            f"${item.get('unitPrice')} ea | Line Total: ${item.get('total')}"
        )
    return "\n".join(lines)


# ---- TOOL 4: Get Outstanding Balances ----
@mcp.tool()
def get_outstanding_balances() -> str:
    """Get all orders that still have money owed — your AR list"""
    query = """
    query {
        orders(first: 100, sortOn: DUE_AT, descending: false) {
            nodes {
                visualId
                total
                balance
                dueDate
                contact { fullName email phone }
                status { name }
            }
        }
    }
    """
    result = query_printavo(query)
    orders = result.get("orders", {}).get("nodes", [])
    unpaid = [o for o in orders if float(o.get("balance") or 0) > 0]
    if not unpaid:
        return "You're all caught up — no outstanding balances!"
    total_owed = sum(float(o.get("balance") or 0) for o in unpaid)
    lines = [f"OUTSTANDING BALANCES — {len(unpaid)} orders | Total owed: ${total_owed:.2f}", ""]
    for o in unpaid:
        lines.append(
            f"  #{o.get('visualId')} | {o.get('contact', {}).get('fullName')} | "
            f"Balance: ${float(o.get('balance') or 0):.2f} | Due: {o.get('dueDate', 'N/A')} | "
            f"Phone: {o.get('contact', {}).get('phone', 'N/A')}"
        )
    return "\n".join(lines)


# ---- TOOL 5: Create a Quote ----
@mcp.tool()
def create_quote(customer_email: str, order_name: str, due_date: str) -> str:
    """
    Create a new quote in Printavo.
    customer_email: the customer's email address (must already exist in Printavo)
    order_name: a nickname for this job (e.g., 'Spring 2026 Tees')
    due_date: in YYYY-MM-DD format (e.g., 2026-05-15)
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
        return f"No customer found with email '{customer_email}'. Add them to Printavo first, then try again."

    contact = contacts[0]
    mutation = """
    mutation($contactId: ID!, $nickname: String, $dueDate: String) {
        createOrder(input: { contactId: $contactId, nickname: $nickname, dueAt: $dueDate }) {
            order { id visualId nickname dueDate }
            errors
        }
    }
    """
    result = query_printavo(mutation, {
        "contactId": contact["id"],
        "nickname": order_name,
        "dueDate": due_date
    })
    order_data = result.get("createOrder", {})
    errors = order_data.get("errors", [])
    if errors:
        return f"Printavo returned an error: {errors}"
    order = order_data.get("order", {})
    return (
        f"Quote created in Printavo!\n"
        f"Order #{order.get('visualId')} | Name: {order.get('nickname')} | "
        f"For: {contact['fullName']} | Due: {order.get('dueDate')}"
    )


# Start the server
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
