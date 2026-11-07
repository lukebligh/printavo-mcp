---
name: ugp-quote-entry
description: >
  Full end-to-end workflow for processing Underground Printing (UGP) new orders at Established Merch.
  Scans the UGP Approval tab for new Screen Print orders, checks each is not already entered in
  Printavo (dedup), builds matching quotes by duplicating template order 6817, populates all order
  details, transfers artwork if available, accepts the order on the UGP Approval tab, and advances
  it through the UGP Order Processing tab. Embroidery orders are NOT entered — they are sent to Luke
  on Slack for approval. Runs hands-off on a schedule; only outliers and errors are surfaced to Luke.

  Trigger this skill whenever Luke:
  - Forwards a UGP new order notification email
  - Says "process UGP orders", "run UGP", "new UGP order", "check UGP", or similar
  - Mentions a UGP order number and wants it built in Printavo
  - Asks to process, quote, or accept a UGP order
  - Runs on its schedule (unattended scan of the Approval tab)

  IMPORTANT: Process one order fully before starting the next.
---

# UGP Quote Entry — Full Workflow (hands-off / schedulable)

## Tool Architecture

**MCP tools** (fast, no browser needed) handle most Printavo operations:
- `search_orders` (dedup check), `duplicate_invoice`, `update_invoice_fields`, `delete_production_files`
- `get_invoice_structure`, `update_line_item`, `duplicate_line_item`
- `set_imprint_pricing`, `add_imprint`
- `set_order_status`, `upload_production_file`

**Claude-in-Chrome MCP** (`mcp__Claude_in_Chrome__*`) handles:
- UGP dashboard interactions (reading, accepting, advancing orders)
- Art file URL extraction from the UGP page's Vue component data (Step 5a)
- Pricing refresh on newly created Printavo orders (Step 4g)
- Calendar block position fix via PATCH call (Step 4g, immediately after save)
- Spec-sheet PDF mockup upload via the Printavo Filestack "Link (URL)" picker (Step 5c)

**Slack MCP** — DM Luke (`luke@estmerch.com`) for EMB approvals, outliers, and errors.

Never use computer-use clicks for web pages.

**Art loading note:** EPS production files and the spec-sheet PDF are pulled by their **S3 URLs**
(read out of the UGP Vue data), not by downloading them to disk. `upload_production_file` accepts the
`https://` S3 URL directly. The spec sheet PDF is attached through the browser (Filestack Link
picker) — never with `attach_mockup_to_order`, which rasterizes PDFs to static images.

---

## Run posture (READ FIRST)

This skill runs **fully hands-off**. Do NOT pause for confirmation or spot-checks.
- Process every eligible order end-to-end without stopping to ask.
- **Only Slack Luke for:** an EMB order (needs his approval), an outlier, or an error.
- On any per-order error, DM Luke, skip that order, and continue to the next — never halt
  the whole run for one bad order.
- Post a short run summary in chat at the end. Routine successes do NOT ping Luke.

---

## Quick Reference
- UGP Approval tab: https://hq.ugp.io/admin/queues/contractor-processing-production (click APPROVAL)
- UGP Production tab: same URL, click PRODUCTION
- UGP Order Processing tab: https://hq.ugp.io/admin/queues/contractor-processing-order-processing
- Template order: **6817** (never modify this order)

---

## Step 1 — Determine Which Orders to Process

**Mode 1 — Email triggered:** Extract UGP order number(s) from the forwarded email. Process only those.

**Mode 2 — Scheduled / scan (default when run unattended):** Navigate to the UGP Approval tab and
collect every order number in the queue. Process them one at a time, fully, without waiting for
confirmation. (No "present the list and wait" step — this run is hands-off.)

---

## Step 2 — Screen Order Type (EMB vs SP)

On the UGP Approval tab, check the TYPE icon on the far left of each row:
- **RED icon = Screen Print (SP)** → process normally.
- **PURPLE icon = Embroidery (EMB)** → **DO NOT enter.** DM Luke on Slack for approval:
  UGP order number, name, quantity, and carrier date, with the note "EMB order — approve to enter?"
  Then move on. Luke will run the skill for that specific order, enter it manually, or decline it.
- **If the icon color is ambiguous / can't be read confidently → treat as an outlier:** do NOT
  enter, DM Luke, move on. Never guess EMB vs SP.

---

## Step 3 — Collect All UGP Data (Before Touching Printavo)

Use the search bar on the UGP Approval tab: type the order number and press **Enter** to filter to
one row. Collect everything before opening Printavo.

### From the order row:
- UGP Order Number (ID column)
- Order Name (Name column)
- Carrier Date (far right — this is the ship date)
- Spec Sheet status: **APPROVED SPEC SHEET** (blue) = art available; **UNAPPROVED SPEC SHEET** (orange) = no art yet

### From PRODUCTS dropdown (click "Products" on the row):
For each product:
- Item number (e.g., 64000G)
- Color (e.g., Black)
- Brand + full description (e.g., Gildan SoftStyle T-Shirt)
- Size breakdown with counts (e.g., M:10, L:10, XL:6)

### From OPERATIONS dropdown (click "Operations" on the row):
For each operation:
- Print location (e.g., Full Front, Left Chest)
- Decoration method — confirm Screenprinting; if not, stop this order, DM Luke (outlier), continue
- Ink color count: count the colored swatches shown (each swatch = 1 color, include white)
- Build imprint abbreviation:
  - Locations: Full Front=FF, Full Back=FB, Left Chest=LC, Right Chest=RC, Left Sleeve=LS, Right Sleeve=RS, Hood=HD
  - Format: `[#]C [LOCATION]` e.g. `3C FF`, `2C FB`, `1C LC`

### Date calculations:
- **Customer Due Date** = Carrier Date from UGP (YYYY-MM-DD)
- **Production Date** = 2 business days before Carrier Date, skipping Saturdays, Sundays, and US federal holidays

**Business day algorithm:** Starting from the Carrier Date, count backwards one calendar day at a
time. Skip any day that is a Saturday, Sunday, or US federal holiday listed below. Count only days
that pass all three checks. Stop when 2 qualifying days have been counted — that is the Production Date.

**Example:** Carrier Date = Tuesday, July 7, 2026
- Count back 1: Mon July 6 — weekday, not a holiday → count 1
- Count back 2: Sun July 5 — SKIP (Sunday)
- Count back 3: Sat July 4 — SKIP (Saturday)
- Count back 4: Fri July 3 — SKIP (observed federal holiday)
- Count back 5: Thu July 2 — weekday, not a holiday → count 2 → **Production Date = July 2**

**2026 US Federal Holidays:**

| Date | Day | Holiday |
|------|-----|---------|
| Jan 1 | Thu | New Year's Day |
| Jan 19 | Mon | Martin Luther King Jr. Day |
| Feb 16 | Mon | Presidents' Day |
| May 25 | Mon | Memorial Day |
| Jun 19 | Fri | Juneteenth |
| Jul 3 | Fri | Independence Day (observed — July 4 falls on Saturday) |
| Sep 7 | Mon | Labor Day |
| Oct 12 | Mon | Columbus Day |
| Nov 11 | Wed | Veterans Day |
| Nov 26 | Thu | Thanksgiving Day |
| Dec 25 | Fri | Christmas Day |

---

## Step 3.5 — DEDUP CHECK (mandatory — prevents duplicate orders)

**Before building anything in Printavo, confirm this UGP order isn't already entered.** This is the
guard that makes unattended re-runs safe: if a previous run created the Printavo order but died
before clearing it off the Approval tab, this catches it instead of building a duplicate.

```
search_orders(query="[UGP Order Number]")
```

- **Match found** (an existing Printavo order whose PO # equals the UGP Order Number, or whose
  nickname ends with " - [UGP Order Number]"): the order is already built. **Skip the entire
  build (Steps 4–5)** and jump to Step 6 to finish accepting/advancing it on UGP (resume path).
  Capture its `visual_id` for Step 6.
- **No match:** proceed to Step 4 and build it.

Never duplicate template 6817 for an order that already has a Printavo record.

---

## Step 4 — Build the Printavo Quote via MCP

### 4a — Duplicate Template 6817

```
duplicate_invoice(source_visual_id="6817")
```

Capture the returned **new_visual_id** — every subsequent MCP call uses this ID.

### 4b — Set Header Fields

```
update_invoice_fields(
  visual_id=new_visual_id,
  nickname="[Order Name] - [UGP Order Number]",
  po_number="[UGP Order Number]",
  production_date="YYYY-MM-DD",
  customer_due_date="YYYY-MM-DD"
)
```

Do NOT pass a real `invoice_date`. Template 6817 is on Net 30 terms — passing an invoice date
triggers a payment term validation error. (If the tool schema requires the field, pass an empty
string; Printavo sets it automatically.)

The PROFILE field (Underground Printing) carries over from the template — do not change it.

### 4c — Delete Inherited Files

The duplicate inherits EPS files and a spec sheet mockup from the last order run through template
6817. Delete all of them now.

```
delete_production_files(visual_id=new_visual_id)
```

### 4d — Get Order Structure

```
get_invoice_structure(visual_id=new_visual_id)
```

Capture from the result:
- `line_item_group_id` — the inherited group (exactly one)
- `first_line_item_id` — the inherited line item in that group
- `first_imprint_id` — the inherited imprint (exactly one from the template)

### 4e — Build Line Items

**Description format for every line item:**
- Line 1: full garment description (e.g. `64000G - Gildan SoftStyle T-Shirt`)
- Line 2+: one imprint abbreviation per line (e.g. `3C FF`, then on a new line `2C FB`)
- Imprint abbreviations are the same across ALL products in the group

**Zero-quantity sizes:** Only include sizes with a count > 0 in `sizes_json`. Omit sizes entirely
if their count is 0 — do not pass them as 0.

**First product** — update the inherited line item:

```
update_line_item(
  line_item_id=first_line_item_id,
  color="[garment color]",
  description="[garment description]\n[imprint 1]\n[imprint 2 if present]",
  sizes_json='{"M": 10, "L": 10, "XL": 6}'   ← only non-zero sizes
)
```

**Each additional product** — duplicate then update:

```
duplicate_line_item(line_item_id=most_recent_line_item_id)
→ returns new_line_item_id

update_line_item(
  line_item_id=new_line_item_id,
  color="[garment color]",
  description="[garment description]\n[imprint 1]\n[imprint 2 if present]",
  sizes_json='{"M": 5, "L": 5}'   ← only non-zero sizes
)
```

All products go in ONE group. Do not create a second group.

### 4f — Set Imprint Pricing (MUST be AFTER all line items)

Pricing is calculated from total quantity across all line items. Running this early with only
partial products entered produces wrong prices.

**First imprint** (the template's inherited imprint):
```
set_imprint_pricing(imprint_id=first_imprint_id, color_count=[ink colors for this location])
```

**Second imprint** (if order has 2 print locations — add a new one):
```
add_imprint(line_item_group_id=line_item_group_id, color_count=[ink colors for this location])
```

**Third imprint** (rare — repeat if needed):
```
add_imprint(line_item_group_id=line_item_group_id, color_count=[ink colors for this location])
```

### 4g — Refresh Pricing and Fix Calendar Block (Chrome MCP)

**Do NOT use `refresh_invoice_pricing`** — that MCP tool does not trigger Printavo's pricing engine
and will not update prices correctly.

The real pricing engine is a Rails endpoint (`/pricing_matrices/calculate_lig_pricing`) that the
Printavo UI calls when you click "Refresh Pricing" in the edit view. Trigger it via Chrome MCP:

1. Navigate to the order's overview to capture the internal database ID:
   ```
   navigate(url="https://www.printavo.com/invoices/{new_visual_id}")
   ```
   The internal DB ID is also returned by `duplicate_invoice` (Step 4a) as "Internal ID" — capture
   that `db_id`; every browser URL for this order below uses it.

2. Navigate to the edit page:
   ```
   navigate(url="https://www.printavo.com/invoices/{db_id}/edit")
   ```

3. Wait 2–3 seconds for the page to fully load.

4. Find and click the **"Refresh Pricing"** button (appears below the imprint card, next to
   "+ Line Item" and "Imprint").

5. Wait 3 seconds for the pricing engine to respond.

6. Verify both line items show real dollar values in the Price and Total columns (not $0.00).
   **If prices show $0: do NOT save. DM Luke (outlier), skip this order, continue to the next.**

7. Click **"Save & finish"**.

8. Confirm the "Order has been updated" banner appears.

9. **Fix the calendar block position** — immediately after the save banner confirms, run this in the
   same browser tab via `javascript_tool`. Note: `javascript_tool` does not serialize an async
   return value cleanly, so stash the status on `window` and read it back in a second call:

```javascript
window.__calStatus = 'pending';
(async () => {
  try {
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const dbId = 'DB_ID_HERE';            // replace with the db_id from step 1 above
    const productionDate = 'YYYY-MM-DD';  // replace with Production Date from Step 3
    const body = 'order%5Bstart_date%5D=' + productionDate + 'T08%3A30%3A00&order%5Bdue_date%5D=' + productionDate + 'T09%3A30%3A00';
    const resp = await fetch('/calendar/orders/' + dbId, {
      method: 'PATCH',
      headers: {
        'X-CSRF-Token': csrf,
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: body
    });
    window.__calStatus = 'STATUS=' + resp.status + ' ok=' + resp.ok;
  } catch (e) { window.__calStatus = 'ERROR=' + e.message; }
})();
'started'
```

   Then read `window.__calStatus` in a follow-up `javascript_tool` call. A **200** confirms success.
   If anything other than 200 is returned, DM Luke (outlier) and continue — the order is entered but
   its calendar block needs a manual fix.

### 4h — Set Status to Quote Approved, then Contract

```
set_order_status(visual_id=new_visual_id, status_name="Quote Approved")
```

Wait 2 seconds, then:

```
set_order_status(visual_id=new_visual_id, status_name="CONTRACT - WAITING ON ARTWORK")
```

The MCP status lookup is paginated over the FULL status list and matches case-insensitively, so the
name resolves reliably. Always set status by NAME — do not hard-code status IDs.

---

## Step 5 — Load Artwork (Only If APPROVED SPEC SHEET Showed Blue)

Skip this entire step if the spec sheet was orange (unapproved). The order stays at
CONTRACT - WAITING ON ARTWORK and art is loaded later (Luke / the ugp-art-transfer skill).

This step mirrors the **ugp-art-transfer** skill: pull the file URLs straight from the UGP page's Vue
component data (no local downloads), upload the EPS files to Printavo via MCP using the S3 URLs
directly, and attach the spec-sheet PDF through the Printavo Filestack "Link (URL)" picker so the
true PDF is preserved.

### 5a — Extract File URLs from UGP (Chrome MCP, Vue data)

1. Navigate to the UGP **Production** tab
   (`https://hq.ugp.io/admin/queues/contractor-processing-production`).
2. Open the FILES dropdown on any row — this mounts the file list items in the DOM.
3. Run this via `javascript_tool` on the UGP tab. It walks every "Production File" button up to its
   Vue order object and returns the URLs for **this exact order** (matched by UGP number, so it never
   grabs a neighbouring row's files):

```javascript
(() => {
  const TARGET = 'UGP_ORDER_NUMBER';  // e.g. '1159991'
  const listItems = [...document.querySelectorAll('.v-list-item')];
  const prodBtns = listItems
    .filter(l => l.querySelector('.v-list-item__subtitle')?.textContent?.trim() === 'Production File')
    .map(l => l.querySelector('button'));
  let match = null;
  prodBtns.forEach(btn => {
    let parent = btn?.__vue__?.$parent, depth = 0, orderData = null;
    while (parent && depth < 15) {
      if (parent.$data?.order) { orderData = parent.$data.order; break; }
      if (parent.$props?.order) { orderData = parent.$props.order; break; }
      parent = parent.$parent; depth++;
    }
    if (orderData) {
      const id = String(orderData.id || orderData.orderNumber || orderData.order_number || orderData.number || '');
      if (id === TARGET) {
        const s = JSON.stringify(orderData);
        match = {
          id,
          name: orderData.name,
          epsUrls: [...new Set(s.match(/https:\/\/ugp-files[^"]*\.eps/g) || [])],
          specUrl: orderData.specSheetUrl || null
        };
      }
    }
  });
  return JSON.stringify(match, null, 1);
})()
```

4. Note `epsUrls` and `specUrl`.
   - **If `match` is null, `specUrl` is null, OR `epsUrls` is empty → art isn't fully approved yet.**
     Leave the order at CONTRACT - WAITING ON ARTWORK, do NOT load partial art, and continue to
     Step 6 (accept + advance). The ugp-art-transfer skill will finish the art on a later run.
   - Only proceed when BOTH the EPS URLs and the spec URL are present.

**Do NOT click the "Production File" download buttons and do NOT download to disk.** The download
buttons generate the S3 URL server-side on click and never expose it to the page — extract the URLs
from the Vue data (above) and pass those S3 URLs straight to the MCP / Filestack tools.

### 5b — Upload EPS Production Files (MCP, S3 URLs)

For each URL in `epsUrls`:
```
upload_production_file(visual_id=new_visual_id, file_path="[EPS_S3_URL]")
```
`upload_production_file` accepts the `https://` S3 URL directly. Upload one at a time. On failure,
retry once; on a second failure DM Luke (art needs manual load), leave status at
CONTRACT - WAITING ON ARTWORK, and still continue to Step 6.

### 5c — Attach Spec Sheet PDF as a True PDF Mockup (Chrome MCP, Filestack Link picker)

`attach_mockup_to_order` rasterizes PDFs to static images — do NOT use it. Attach the real PDF
through the Printavo edit page's Filestack picker instead (the S3 URL is fetched inside the Printavo
tab, so no download is needed):

1. Navigate to the Printavo edit page: `https://www.printavo.com/invoices/{db_id}/edit`
   (use the internal DB ID from Step 4g). Wait for it to load.
2. Click the dropdown arrow (▾) on the far right of the **first line item row**, then click
   **Attach Mockups**.
3. The Filestack picker opens. Select the **Link (URL)** source reliably via `javascript_tool`
   (the source sidebar collapses to icons, so don't pixel-click it):
   ```javascript
   const items=[...document.querySelectorAll('.fsp-source-list__item')];
   const u=items.find(i=>/link|url/i.test(i.textContent)); if(u)u.click();
   JSON.stringify({clicked:!!u});
   ```
   Screenshot to confirm an **"Enter a URL"** input appeared.
4. Click the "Enter a URL" field, type the `specUrl` from Step 5a, and click the submit (→) button to
   its right. The PDF queues under **FILES** with its filename and an **Upload** button appears.
5. Click **Upload**, wait ~5s, and confirm the dialog closes and a mockup thumbnail appears on the
   first line item. If the PDF never queues or the upload fails: DM Luke, leave status at
   CONTRACT - WAITING ON ARTWORK, continue to Step 6.
6. Click **Save & finish** and confirm the page leaves edit mode (URL no longer ends in `/edit`).

**Note:** Printavo/Filestack displays PDF mockups as image thumbnails — the stored file is still the
genuine PDF (`content-type: application/pdf`). Expected.

### 5d — Set Status to Art Approved (MCP)

Only after BOTH the EPS files and the spec-sheet mockup are confirmed (optionally re-run
`get_invoice_structure` to verify the production files are attached):

```
set_order_status(visual_id=new_visual_id, status_name="Art Approved")
```

---

## Step 6 — Accept Order on UGP Approval Tab

1. Navigate to UGP Approval tab, search by order number + Enter to filter to ONE row.
2. Click the **checkbox** on the left of the order row (the individual row — never a header/select-all).
3. A green checkmark button appears at the bottom right of the screen.
4. Click the **green checkmark** to accept the order.
5. Confirm the order disappears from the queue.
6. If the green checkmark does not appear after checking the box: screenshot, DM Luke (outlier),
   do not retry, continue to the next order.

---

## Step 6.5 — Advance Order Through the Order Processing Tab

After the order is accepted off the Approval tab, advance it through Order Processing.

1. Navigate to `https://hq.ugp.io/admin/queues/contractor-processing-order-processing`.
2. Type the UGP order number in the top nav search bar + Enter. Confirm **exactly 1 order** shows.
3. If found:
   - Click the individual **row** checkbox (never the header/select-all).
   - Verify the FAB badge shows **"1"** before clicking.
   - Click the **green FAB button** and confirm the order disappears from the filtered list.
4. If not found after searching: it may already be past this tab — note it and move on (this is
   fine; the downstream shipped-checkin skill also tolerates "already advanced").
5. On any error here: screenshot, DM Luke (outlier), continue.

---

## Step 7 — Report

Post a concise run summary in chat covering every order handled:
- UGP Order # and Name
- Printavo order # (visual_id) — or "already existed (dedup)" / "EMB — sent to Luke" / "skipped: reason"
- Products / line items, imprints set (e.g. `3C FF`, `2C FB`)
- Artwork loaded: yes / no (and, if no on a blue spec, why)
- Final Printavo status
- Accepted on Approval: ✓ | Advanced Order Processing: ✓
- Any discrepancy, outlier, or error

**Do NOT pause for a spot-check.** Routine successes stay in the chat summary. Only the EMB
approvals, outliers, and errors get a Slack DM to Luke.

---

## Error Handling

DM Luke (`luke@estmerch.com`) via Slack for anything below, then **skip that order and continue** —
never halt the whole run for one bad order.

| Situation | Action |
|-----------|--------|
| EMB (purple) or ambiguous type icon | Do NOT enter. DM Luke for approval. Continue. |
| Order already in Printavo (dedup match) | Skip build, resume at Step 6 (accept + advance). |
| `duplicate_invoice` fails or returns unexpected result | DM Luke, skip order, continue |
| Non-Screenprinting decoration found in Operations | DM Luke, skip order, continue |
| "Refresh Pricing" shows $0 or doesn't update | Do NOT save. DM Luke, skip order, continue |
| Calendar PATCH returns anything other than 200 | DM Luke (order entered, block needs manual fix), continue |
| Blue spec but Vue extraction returns no EPS/spec URLs | Leave at CONTRACT - WAITING ON ARTWORK, no art load, still accept + advance (art-transfer skill will finish it later) |
| EPS upload fails after one retry (blue spec) | DM Luke, leave at CONTRACT - WAITING ON ARTWORK, still accept + advance |
| Spec PDF fails to queue/upload in Filestack Link picker | DM Luke, leave at CONTRACT - WAITING ON ARTWORK, still accept + advance |
| Size mismatch between UGP and Printavo size columns | Enter what exists, DM Luke with the discrepancy |
| Green checkmark doesn't appear on UGP Approval | Screenshot, DM Luke, do not retry, continue |
| Order Processing FAB doesn't appear / badge ≠ "1" | Screenshot, DM Luke, do not retry, continue |
| Any other mid-order failure | DM Luke with order # and failure details, continue |

---

## Key Notes

**Dedup is the safety net for unattended runs.** Step 3.5 is what keeps a crashed-and-rerun cycle
from building duplicate orders. Never skip it.

**EMB orders are never auto-entered.** Purple icon → Slack Luke for approval. He decides.

**Pricing timing is critical.** `set_imprint_pricing` and the Chrome MCP pricing refresh must run
AFTER all line items are entered. The price is based on total quantity across all products — running
it early produces wrong per-piece pricing.

**Pricing uses a Rails endpoint, not GraphQL.** The "Refresh Pricing" button calls
`/pricing_matrices/calculate_lig_pricing`. The MCP tool `refresh_invoice_pricing` does NOT use this
endpoint and will not correctly update prices. Always use the Chrome MCP approach in Step 4g.

**Art file URLs come from the UGP Vue data — never from clicking the download buttons.** The
Production File download buttons generate the S3 URL server-side on click and never expose it to the
page (no network request, new tab, or DOM link is capturable). Read `epsUrls` and `specUrl` out of
the order's Vue component data (Step 5a) and pass those S3 URLs straight to the tools.

**Match the Vue order data by UGP number.** Multiple orders' FILES dropdowns can be mounted in the
DOM at once. Filter the "Production File" buttons to the order whose `id` equals the UGP number —
grabbing the first one (`[0]`) will pull a neighbouring order's art.

**`upload_production_file` takes the S3 URL directly.** Pass `file_path="[EPS_S3_URL]"` — no local
download, no `/Downloads` path.

**Spec sheet mockup goes through the browser Filestack "Link (URL)" picker.** Never use
`attach_mockup_to_order` (it rasterizes the PDF to an image) and never use `upload_production_file`
for the spec sheet (wrong section). The Filestack Link picker preserves the true PDF.

**Zero-quantity sizes.** Only pass sizes with count > 0 in `sizes_json`.

**Status flow.** Quote Approved → CONTRACT - WAITING ON ARTWORK. If artwork loads in Step 5, set
Art Approved as the final status. Always set by name — never a hard-coded status ID.

**Template file carryover.** Every duplicate of 6817 inherits EPS files and a spec mockup from the
last order. `delete_production_files` (Step 4c) removes both. Never skip it.

**UGP search.** Always type the order number and press Enter before interacting with a row.

**Invoice Date — do NOT pass a real date to `update_invoice_fields`.** Template 6817 is Net 30;
passing an invoice date triggers a payment term validation error. If the schema requires the field,
pass an empty string. Printavo sets it automatically.

**Calendar block position** requires the PATCH call in Step 4g — Printavo sets `startAt` to the
duplication timestamp, which the PATCH corrects to an 8:30–9:30 AM block on the production date.
