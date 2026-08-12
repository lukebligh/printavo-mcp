---
name: sage-to-printavo-quote
description: >
  Builds a Printavo quote from SAGE promotional-product selections for ANY customer.
  Given a customer name and one or more SAGE items (by SPC or from a sage_product_search),
  the skill: finds the customer in Printavo, opens a New Quote, sets the header (nickname,
  placeholder dates, UPS Ground), adds one Line Item Group per item (category Misc, SAGE
  item number, color, short description, quantity-only sizing, list price at the given qty
  tier), attaches an Est Merch-branded spec-sheet PDF (product image + specs) to each line,
  removes the auto credit-card fee, saves, and Slacks the requester.

  Trigger when the user says: "quote [customer] [qty] [item] from SAGE", "build a Printavo
  quote for these SAGE items", "put these promo items on a quote for [customer]", or right
  after a sage_product_search when they say "quote these for [customer]".
---

# SAGE → Printavo Quote (any customer)

## Tool architecture
- **SAGE (Printavo MCP):** `sage_product_search` (find items), `sage_product_detail` (item#,
  description, colors, imprint area, price-by-qty tiers, image URLs — service 105).
- **Printavo (MCP):** customer/quote lookup and creation, line-item + pricing fields, fee removal
  where exposed. Prefer MCP for anything it supports.
- **Claude-in-Chrome MCP:** Printavo UI-only steps — removing size-count fields, attaching the
  spec-sheet PDF via the file loader, removing the credit-card fee if not exposed by MCP, Save & Finish.
  *(Never use computer-use clicks for web pages — use the Chrome MCP.)*
- **Slack MCP:** notify the requester on completion.

**Spec-sheet PDF:** SAGE has no spec-sheet PDF endpoint. Generate one per item from
`sage_product_detail` data + the product image: an Est Merch-branded one-pager (image, product
name, SAGE item#, short description, imprint area/location, price). Save it locally, then attach
it to the line item through the Printavo file loader.

## Run posture
Interactive — this runs on demand for a specific request. Ask for anything missing (quantity,
color) before building. Confirm the finished quote link at the end. One item fully before the next.

---

## Step 1 — Gather the request
Collect from the user (ask for whatever is missing):
- **Customer name** (required).
- **Item(s)** — either SPCs from a prior `sage_product_search`, or run a search now and let the
  user pick.
- **Quantity per item** — if not given, **ask** "how many of [item]?" and use that number.
- **Color per item** — if not given, set the line color to **TBD**.

## Step 2 — Pull SAGE detail (per item)
For each chosen SPC, call `sage_product_detail(spc)`. Capture:
- **itemNum** (goes in the Printavo line "Item number")
- **short description** (line "Description")
- **imprint area / location** (for the spec sheet)
- **price tier** — pick the **list price** row matching the requested quantity (round down to the
  nearest tier at/below the qty); use that as the line **list price**
- **image URL(s)** (for the spec sheet + mockup)

## Step 3 — Generate the spec-sheet PDF (per item)
Build a one-page Est Merch-branded PDF: product image, product name, **SAGE item #**, short
description, imprint area/location, and the price. Save locally (e.g. Downloads). This is the file
attached to the line item. *(Brand: Plus Jakarta Sans; Primary #C9B99A, Accent #E9C050 — see Brand
Style Guide.)*

## Step 4 — Find the customer & open a New Quote
- Look up the customer by the name given. If multiple matches, ask which one.
- Open a **New Quote** for that customer.

## Step 5 — Header fields (placeholders)
- **Nickname** = `<Company Name> – <searched item type>` (e.g., "Regal Midwest – Koozies").
- **Production date** = today **+ 2 weeks**.
- **Due date** = today **+ 4 weeks** (2 weeks after production).
- **Invoice date** = **same as due date**.
- **Delivery method** = **UPS Ground**.
- **Terms** = blank.
- Leave everything else above the line items alone.

## Step 6 — Line items (one Line Item Group per item)
For each item:
1. **Category** = `Misc.`
2. **Item number** = SAGE `itemNum`.
3. **Color** = user value, else `TBD`.
4. **Description** = short description.
5. Open the line's **3-dot menu → remove all size-count fields**, leaving only **Quantity**.
6. **Quantity** = the qty for this item.
7. **List price** = the SAGE list price at that qty tier.
8. **Line-item action menu → Attach Mockup** → file loader → **+** → select the generated
   **spec-sheet PDF** (from Downloads) → load. It appears at the line level.
9. Another item → **+ Line Item Group** and repeat.

## Step 7 — Finish
- **Remove the credit-card fee** that auto-populates (X on the far right of the fee line).
  *(A `Credit Card Fee` is a fee, not a shipping fee — remove it here.)*
- **Save & Finish.**
- **Slack the requester** (Luke `luke@estmerch.com`, Clint, or Mechelle) with the quote link and a
  one-line summary (customer, items, quantities).

---

## Notes / to verify on first live run
- Exact Printavo UI selectors for: removing size-count fields, the Attach-Mockup file loader, and
  the credit-card-fee X — confirm during the first real build and tighten this doc.
- Whether any of the above (line color, list price, fee removal) can be done via Printavo MCP
  instead of the browser — prefer MCP where it works to reduce clicks.
- Price-tier rule: if the requested qty is below the lowest tier, use the lowest tier's price and
  flag it.
