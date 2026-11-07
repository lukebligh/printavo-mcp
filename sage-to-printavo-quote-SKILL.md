---
name: sage-to-printavo-quote
description: >
  Builds a Printavo quote from SAGE promotional-product selections for ANY customer, fully via the
  Printavo MCP API (no browser). Given a customer and one or more SAGE items (by prod_eid/SPC or from
  a sage_product_search), the skill: finds the customer, creates a quote, sets header + placeholder
  dates + UPS Ground, then per item creates a line-item group and a quantity-only line item
  (category Misc, SAGE item number, TBD color, description with specs, tier list price), attaches the
  SAGE product image as a mockup, removes any auto credit-card fee, and Slacks the requester.

  Trigger when the user says: "quote [customer] [qty] [item] from SAGE", "build a Printavo quote for
  these SAGE items", "put these promo items on a quote for [customer]", or right after a
  sage_product_search when they say "quote these for [customer]".
---

# SAGE → Printavo Quote (API-native)

## Tools (all Printavo MCP — no browser needed)
- `sage_product_search` — find items (returns name, SPC, **prod_eid** in the image URL as `P=`, item#, price).
- `sage_product_detail(prod_eid=…)` — item number, description, colors, dimensions, imprint area,
  **price-by-qty tiers (list + net)**, image URLs. *(Use `prod_eid`, not `spc` — SPC fails on the
  Public/test login.)*
- `create_quote(customer_email, order_name, due_date)` — creates the quote shell.
- `update_invoice_fields(visual_id, nickname, po_number, production_date, customer_due_date, invoice_date)`
- `set_delivery_method(visual_id, "UPS Ground")`
- `add_line_item_group(visual_id)` → returns group id (one group per product).
- `add_line_item(line_item_group_id, item_number, description, color, quantity, price)` — quantity-only
  (uses the `size_other` bucket) + unit list price.
- `set_line_item_category(line_item_id, "Misc")`
- `attach_mockup_to_order(visual_id, file_path=<image URL>, line_item_id=<id>)` — Printavo fetches the
  SAGE image server-side.
- `remove_credit_card_fee(visual_id)` — safety (API quotes usually have none).
- Slack MCP — notify the requester.

## Placeholders (per Est Merch)
- **Nickname** = `<Company> – <item type>` (e.g., "Regal Midwest – Golf Bag Coolers")
- **Production date** = today + 2 weeks · **Due & Invoice date** = today + 4 weeks · **PO** = blank
- **Delivery** = UPS Ground · **Color** = user value or `TBD`

---

## Steps

**1. Gather the request.** Customer name; the item(s) (from a `sage_product_search`, keep each item's
`prod_eid` — it's the `P=` value in the image URL); quantity per item (ask if missing); color per
item (ask, else `TBD`).

**2. Per item — pull detail.** `sage_product_detail(prod_eid=…)`. Capture item number, short
description, **colors**, **dimensions**, **imprint area**, the **image URL**, and the **list price at
the requested qty tier** (use the tier row at/below the qty).

**3. Create the quote.** `create_quote(customer_email, nickname, due_date=<today+4wk>)`.
(create_quote finds the customer by contact email.)

**4. Header.** `update_invoice_fields(visual_id, nickname, po_number="",
production_date=<today+2wk>, customer_due_date=<today+4wk>, invoice_date=<today+4wk>)`.

**5. Delivery.** `set_delivery_method(visual_id, "UPS Ground")`.

**6. Per item — build the line.**
   a. `add_line_item_group(visual_id)` → group id.
   b. `add_line_item(group_id, item_number=<SAGE item#>, color=<color/TBD>, quantity=<qty>,
      price=<tier list price>, description=<see format below>)` → line item id.
   c. `set_line_item_category(line_item_id, "Misc")`.
   d. `attach_mockup_to_order(visual_id, file_path=<SAGE image URL, RS=1800>, line_item_id=<line item id>)`.

   **Description format (puts specs on the quote):**
   `"<product blurb> | Colors available: <colors> | Size: <dimensions> | Imprint: <imprint area>"`

**7. Finish.** `remove_credit_card_fee(visual_id)` (usually nothing to remove). Then **Slack the
requester** (Luke `luke@estmerch.com`, Clint, or Mechelle) with the quote link + a one-line summary.

---

## Rules & gotchas (learned in testing)
- **Detail lookup:** use `prod_eid`, not SPC (SPC is account-specific and fails on the Public login).
- **Quantity-only:** the tools use the `size_other` bucket = total quantity (no size breakdown).
- **Mockup = the SAGE product image URL** (Printavo fetches it). No spec-sheet PDF is needed or
  available from SAGE — so specs go in the **description** (step 6, format above).
- **Price tier:** if requested qty is below the lowest tier, use the lowest tier's price and flag it.
- **Net cost is confidential** — never put net pricing on the quote or in any client-facing output.
- Do NOT expose the supplier name to clients.
