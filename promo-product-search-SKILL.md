---
name: promo-product-search
description: >
  Searches SAGE for promotional products and renders a consistent, client-safe visual gallery
  (Est Merch-branded HTML) of the best options — image, colors, size, imprint area, lead time, and a
  volume-pricing ladder per item. Use whenever someone asks to find/search promo products: "find
  [promo item] under $X for [customer]", "search SAGE for koozies", "top 5 drinkware options",
  "what golf-bag coolers can we get", or any promo product-finding request. The gallery is the output
  the team reviews and may screenshot for a client, so formatting must be identical every time.
  (To then put items on a quote, hand off to the sage-to-printavo-quote skill.)
---

# Promo Product Search → Client-Safe Gallery

## Steps
1. **Search.** Call `sage_product_search` with the request criteria (`query`, `price_high`/`price_low`,
   `qty`, `sort`, `limit`). Use `sort=PRICE` for budget-led asks, `POPULARITY`/`BESTMATCH` otherwise.
2. **Curate.** SAGE returns loosely-matched results — **hand-pick the 5–7 that truly match the intent**
   (e.g., "golf bag cooler" = the slip-in-the-bag type, NOT cart coolers or generic totes). Prefer
   reliable/domestic suppliers and reasonable lead times; flag long/import lead times.
3. **Detail per item.** For each shortlisted item, call `sage_product_detail(prod_eid=…)` (the
   `prod_eid` is the `P=` value in the search image URL). Capture **colors, dimensions, imprint area,
   image URL, and the price-by-qty tiers**.
4. **Render the gallery** (self-contained HTML — see template) and present the file. Images render when
   opened in a browser (the in-chat sandbox blocks the promo image URLs).

## Output = one self-contained HTML file, formatted EXACTLY like this every time
- **Branding:** dark (`#111`) header with the **white "Established Merch" logo** embedded as base64
  (file: `WHITE BLOCK KNOCK OUT.png`); font **Plus Jakarta Sans**; palette Primary `#C9B99A`,
  Secondary `#796F5C`, Accent `#E9C050`.
- **Header:** "Prepared for <Customer>" + "volume pricing shown".
- **Per-card, in this order:** product **image** · product **name** · an accent **"At <qty> → $X / ea"**
  callout · a spec table (**Item #, Colors, Size, Imprint, Lead**) · a **Volume pricing** table (Qty ›
  Price) with the tier row that the requested qty falls into **highlighted**.
- **Images:** use the SAGE image URL at `RS=1800`, contained (never cropped).

## HARD RULES (client-safe — never violate)
- **No supplier name** anywhere (clients must not source direct).
- **No net cost / margin** — list price tiers only.
- **No buttons or links** that aren't live/clickable (it may be screenshotted).
- No mention of "SAGE" on the gallery.

## Reference CSS (keep consistent)
Cards: white, 1px `#e8e2d6` border, 14px radius, soft shadow. Image box 250px tall, white, contained.
Callout: Accent background, dark text, pill. Spec table: 12.5px, muted `#9a9384` key column. Tier table:
13px, highlighted row background `#faf3dc`, bold. Lead time in green (`#2e7d5b`).

*Handoff: when the user picks items to quote, trigger **sage-to-printavo-quote** with those items.*
