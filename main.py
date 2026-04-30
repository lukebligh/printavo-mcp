# ---- TOOL 8: Production Schedule & Placement Count ----
@mcp.tool()
def get_production_schedule(date: str) -> str:
    """
    Get all orders scheduled for production on a given date, with total imprint/embroidery/DTF placements.
    date: format YYYY-MM-DD (e.g. 2026-05-01)
    """
    # Pull multiple pages of invoices and filter by startAt date in Python
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

    while pages_searched < 20:  # max 500 invoices searched
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
                # We've passed the date going backwards — stop paginating
                consecutive_misses += 1

        # Stop early if we've gone well past the target date
        if consecutive_misses >= 25:
            break

        if not page_info.get("hasNextPage"):
            break

        cursor = page_info.get("endCursor")
        pages_searched += 1

    if not all_invoices:
        return f"No orders scheduled for production on {date}."

    # For each invoice, get imprint details
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
    grand_total_placements = 0
    breakdown_by_type = {}

    for o in sorted(all_invoices, key=lambda x: x.get("visualId", 0)):
        try:
            qty = int(o.get("totalQuantity") or 0)
            customer = (o.get("contact") or {}).get("fullName") or "Unknown"
            nickname = o.get("nickname") or ""
            status_name = (o.get("status") or {}).get("name") or "?"
            invoice_id = o.get("id")

            # Determine if this is a store order
            is_store_order = "store" in status_name.lower()

            # Get imprints for this invoice
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
            order_placements = qty * num_locations
            grand_total_qty += qty
            grand_total_placements += order_placements

            lines.append(f"  #{o.get('visualId')} | {customer} | {nickname}")
            lines.append(f"    Status: {status_name} | Items: {qty} | Locations: {num_locations} | Placements: {order_placements}")

            if is_store_order:
                # Store orders — label as Store Order, skip decoration inference
                lines.append(f"    → Store Order (InkSoft)")
                breakdown_by_type["Store Order"] = breakdown_by_type.get("Store Order", 0) + order_placements
            else:
                for imp in all_imprints:
                    type_of_work = (imp.get("typeOfWork") or {}).get("name") or ""
                    details = imp.get("details") or ""

                    # Infer decoration type from TypeOfWork, then details text, then imprint name
                    if type_of_work and type_of_work.lower() != "unknown":
                        dec_type = type_of_work
                    elif "dtf" in details.lower():
                        dec_type = "DTF"
                    elif "embroid" in details.lower():
                        dec_type = "Embroidery"
                    elif "screenprint" in details.lower() or "screen print" in details.lower():
                        dec_type = "Screen Print"
                    elif "dtf" in (imp.get("details") or "").lower():
                        dec_type = "DTF"
                    else:
                        # Fall back to imprint name keywords
                        name_lower = details.lower()
                        if "emb" in name_lower:
                            dec_type = "Embroidery"
                        elif "scrn" in name_lower or "sp" in name_lower:
                            dec_type = "Screen Print"
                        else:
                            dec_type = "Screen Print"  # default for non-store orders

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
    lines.append(f"  Total Placements: {grand_total_placements}")
    if breakdown_by_type:
        lines.append("")
        lines.append("  BY TYPE:")
        for type_name, count in sorted(breakdown_by_type.items()):
            lines.append(f"    {type_name}: {count} placements")

    return "\n".join(lines)
