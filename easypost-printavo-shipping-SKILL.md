# EasyPost → Printavo Freight Billing (manual skill)

This skill bills freight on orders the shop has marked **SHIPPED - BILLABLE**.
It reads the exact UPS label cost from EasyPost, marks it up 10%, writes a
**Shipping** fee to the Printavo invoice **without disturbing any existing fee**,
and flips the order to **INVOICED** (which auto-emails the customer their
invoice).

Two channels, no server needed:

- **Printavo MCP** — order lookup, status changes.
- **Chrome (Claude-in-Chrome MCP)** — reading the invoice, reading the UPS
  tracking number from the **More Actions** menu, querying the EasyPost API with
  a same-origin fetch, and entering the Shipping fee on the edit page.

**Nothing is written to Printavo without Luke's explicit approval.** The
INVOICED flip is customer-visible, so diagnose freely and write carefully.

---

## Constants

| Thing | Value |
|---|---|
| Printavo account | sales@estmerch.com |
| Trigger status | **SHIPPED - BILLABLE** (case-insensitive) |
| Never touch | **SHIPPED - NOT BILLABLE** (a separate native automation owns it) |
| Markup | raw EasyPost label cost × **1.10**, rounded to cents |
| Fee name | `Shipping` — Shipping preset, fixed `$`, **NOT** taxed |
| Final status | `INVOICED` (set by NAME, never a hard-coded ID) |
| Printavo base URL | `https://www.printavo.com` |
| EasyPost API key | `assets/easypost_key.txt` in this skill directory |

**Skip any order that already has a non-zero `Shipping` fee** — it was already
processed, and double-charging is the worst failure this workflow has. (A
`Credit Card Fee` is NOT a shipping fee — its presence does not mean the order
was processed.)

---

## Run posture (READ FIRST)

- Diagnosis (Steps 0–3) is entirely **read-only** and safe to run anytime.
- **Always present the summary and STOP for Luke's approval before any write**
  (Step 4). Never auto-write.
- On any per-order problem, flag it, skip that order, and continue — never halt
  the whole run for one bad order.

---

## Step 0 — Read the API key

Read `assets/easypost_key.txt` from this skill's directory. If it is missing or
still says `PASTE_KEY_HERE`, stop and ask Luke for a **read-only** EasyPost key.
The key is used only in an `Authorization` header against `api.easypost.com` —
never paste it into a web form or send it anywhere else.

---

## Step 1 — Build the worklist

**PREFERRED — order numbers provided:** if Luke gives one or more order numbers
(or forwards an email with them), process exactly those. This is the reliable
path and should be the default ask.

**⚠️ Scan mode is unreliable — know why before using it.** The
`SHIPPED - BILLABLE` status holds a large legacy backlog (200+ orders going back
years). `search_orders("SHIPPED - BILLABLE")` returns them **oldest-first and
caps** the result set, so today's freshly shipped orders are NOT reachable near
the top of the list. Do **not** blindly loop the whole status — you will churn
through hundreds of old orders and still miss the new ones. If Luke asks for a
"scan," ask him which order numbers shipped today (he knows them), or confirm a
bounded recent set. Only process orders that (a) are truly `SHIPPED - BILLABLE`
and (b) have **no** existing `Shipping` fee.

For each candidate, call `get_order_details` to capture the **Internal ID**
(a.k.a. db id — needed for every browser URL), customer, status, and total.

---

## Step 2 — Diagnose each order (read-only)

Navigate Chrome to `https://www.printavo.com/invoices/{internal_id}` and read
the page (`get_page_text`). Capture and record per order:

1. **Status** — confirm `SHIPPED - BILLABLE`. If `SHIPPED - NOT BILLABLE` or
   anything else → **SKIP**.
2. **Recipient** — the **Customer Shipping** company, name, and (if shown) zip.
   This is the primary EasyPost match key.
3. **Existing fees** — the full fee list with names and amounts (e.g.
   `Credit Card Fee $8.71 (3%)`). **Record every fee** — you must reproduce this
   exact set when you edit, so nothing gets dropped. If a `Shipping` fee already
   exists → **SKIP** (already processed).
4. **Delivery method** (informational, e.g. UPS Ground).

**Do NOT rely on the invoice body for the UPS tracking number.** On these
invoices the `1Z…` number is usually **not** rendered in the page body. It lives
in **More Actions → Shipping Labels → Tracking Number** (see Step 3b). Don't
treat "no 1Z in the body" as "no tracking."

---

## Step 3 — Get the EXACT label cost from EasyPost

### 3a — Pull EasyPost shipments (same-origin fetch, window-stash pattern)

Navigate a tab to `https://api.easypost.com/v2/shipments` (it renders a 401 JSON
page — that's fine; we only need the origin. Do NOT use the bare
`https://api.easypost.com/` root, which redirects to docs and breaks the fetch).

**`javascript_tool` does not serialize an async return value** — a bare
`(async()=>{…})()` comes back as `{}` or `pending`. Stash the result on `window`
and read it back in a second call after a short wait:

```javascript
window.__ep = 'pending';
(async () => {
  try {
    const key = 'KEY_FROM_STEP_0';
    const resp = await fetch('/v2/shipments?page_size=100&purchased=true', {
      headers: { 'Authorization': 'Basic ' + btoa(key + ':') }
    });
    if (!resp.ok) { window.__ep = 'HTTP ' + resp.status; return; }
    const data = await resp.json();
    // group by order_id (multi-box rule) and keep recipient + trackings + rate
    const groups = {};
    data.shipments.filter(s => s.selected_rate).forEach(s => {
      const oid = s.order_id || ('SINGLE:' + s.id);
      const a = s.to_address || {};
      const g = groups[oid] || (groups[oid] = {
        company: a.company || null, name: a.name || null, zip: a.zip || null,
        rate: s.selected_rate.rate, boxes: 0, trackings: [], created: s.created_at
      });
      g.boxes++;
      g.trackings.push((s.tracking_code || '').toUpperCase());
    });
    window.__ep = JSON.stringify(groups);
  } catch (e) { window.__ep = 'ERR ' + e.message; }
})();
'started'
```

Then, after a 2–3s wait, read `window.__ep` in a separate `javascript_tool`
call. If it is still `pending`, wait and read again. If page 1 doesn't reach far
enough back, paginate with `&before_id={last_shipment_id}`.

### ⛔ MULTI-BOX — the one rule you cannot get wrong

A multi-box order is **several shipments sharing one `order_id`**, each with its
own `1Z…`. **Each shipment's `rate` is the cost of the WHOLE order, not one
box.** The grouping above already collapses this correctly: **each `order_id`
contributes its `rate` exactly once**, no matter how many boxes. `SINGLE:*`
groups are standalone single labels. **Never sum the box rates.** (Three boxes at
`$18.40` on one `order_id` = **$18.40** raw, not $55.20.)

### 3b — Match each Printavo order to one EasyPost group

1. **Match by recipient** — find the group whose `company`/`name`/`zip` equals
   the invoice's Customer Shipping. **Exactly one match → deterministic**, done.
2. **Same recipient, multiple groups (ambiguous)** — e.g. two different orders
   both shipping to the same person. Read the **actual tracking number** off the
   Printavo invoice to disambiguate:
   - On the invoice overview, click **More Actions** (top right). Under
     **Shipping Labels**, each label row has a **Tracking Number** control —
     open/hover it to reveal the `1Z…` (it prefills in a small dialog/tooltip).
     Read it via screenshot or DOM.
   - The Printavo tracking will equal one of the `trackings` in exactly one
     EasyPost group → that's the match. Assign it; the other group is the other
     invoice.
3. **No group matches the recipient, or still ambiguous after tracking** → flag
   ⚠️ "couldn't match EasyPost label — bill manually" and SKIP. **Never guess a
   cost.**

**Fee** = matched group `rate` × 1.10, rounded to cents.

---

## Step 4 — Present the summary and STOP for approval

One block per order:

```
Found X billable orders:

✅ #6981 — StrategicKC / Steve Dubinsky
   Existing fees: Credit Card Fee $8.28
   EasyPost: order_42d0ada (2 boxes, counted once) $44.69 → $49.16 with 10%
   Match: recipient + tracking 1Z…095969
   → Add Shipping $49.16 (keep Credit Card Fee), new total $333.44, flip INVOICED

⚠️ #6943 — Good Times / Rachel Harris
   Two "Wild Animal Safari" labels in EasyPost — one to Rachel Harris (31822),
   one to a different person. Matched the unique Rachel Harris label: $38.72 → $42.59.
```

Use ✅ ready · ⚠️ needs attention · ❌ error. Then ask:
**"Ready to process the X ready orders? Or look at any individually first?"**
Write nothing until Luke clearly says go.

---

## Step 5 — Execute (per order, one at a time)

Finish one order fully before starting the next.

1. Navigate to `https://www.printavo.com/invoices/{internal_id}/edit`. Wait
   3–4s for a **full** load, then scroll to the fee table.

2. **🔴 GUARDRAIL — verify existing fees rendered BEFORE touching anything.**
   Printavo's edit view sometimes loads with the fee table EMPTY even though the
   invoice overview shows fees (this is what silently dropped a Credit Card Fee
   on a prior run). Compare the edit-page fee rows against the fees you recorded
   in Step 2:
   - If every existing fee (especially `Credit Card Fee`) is present → proceed.
   - If any recorded fee is **missing** from the edit table → **do NOT add or
     save.** Reload the edit page, wait 3–5s, and re-check. Only proceed once the
     pre-existing fees are visible. Saving with a fee missing deletes it.

3. Click **+ Fee** to add a NEW row (do not overwrite an existing row). In the
   new row's "Select a Fee" dropdown, type `Shipping` and pick the **Shipping**
   preset (auto-fills description, sets `$` fixed, unchecks Taxed). Triple-click
   the new row's Amount field and type the marked-up fee.

4. **Verify the math before saving:** the new `Fees Total` must equal
   `(sum of all pre-existing fees) + shipping`, and the new invoice `Total` must
   equal `Item Total + Fees Total`. The pre-existing fees must still be listed.
   If anything looks off (a fee vanished, totals don't add up) → **do not save**,
   screenshot, and tell Luke.

5. Click **Save & finish**; confirm the page leaves edit mode (URL no longer
   ends in `/edit`).

6. Flip status: `set_order_status(visual_id, "INVOICED")` (by NAME). If the MCP
   call fails, use the status pill dropdown on the invoice overview. The INVOICED
   flip auto-sends the invoice to the customer.

7. **🔴 Post-write verification — this catches a dropped fee.** Re-read
   `get_order_details(visual_id)` (or the invoice page) and confirm:
   - Status is `INVOICED`, and
   - `Total == Item Total + every pre-existing fee + shipping`.

   If the Total is short by a pre-existing fee amount (e.g. exactly the old
   Credit Card Fee), that fee was dropped during save. Fix it: re-open the edit
   page, re-add the SAME fee (for a 3% Credit Card Fee, use the `Credit Card Fee`
   preset — it recomputes 3% of the item total, matching the original), verify
   the corrected Total, and Save & finish. Do **not** add a second/duplicate
   shipping fee. The order is already INVOICED, so no second status flip is
   needed.

---

## Step 6 — Report

Per order: recipient matched, how it matched (recipient only, or recipient +
tracking), raw cost (note multi-box), fee written, pre-existing fees preserved,
new total, status confirmed. List anything skipped/flagged and what Luke needs to
fix.

---

## Error handling

| Situation | Action |
|---|---|
| API key missing / placeholder | Stop; ask Luke for a read-only EasyPost key. |
| EasyPost returns 401/403 | Key is wrong or revoked. Stop and tell Luke. |
| No billable orders (order numbers given) | "No SHIPPED - BILLABLE orders in that set." |
| Order is SHIPPED - NOT BILLABLE | SKIP — never touch it. |
| `Shipping` fee already on invoice | SKIP — already processed. |
| Recipient not found in EasyPost | Flag ⚠️ (label maybe not purchased). Never guess. |
| Same recipient, can't disambiguate even with tracking | Flag ⚠️; ask Luke which label. Never guess. |
| Pre-existing fee missing from edit view | Reload/wait; never save with it missing. |
| Fee math wrong before save | Don't save. Screenshot, show Luke. |
| Post-write Total short by a pre-existing fee | Re-add that exact fee, re-save (order stays INVOICED). |
| Status flip fails after fee saved | Tell Luke — fee exists but invoice not sent; flip via UI status pill. |

---

## Key notes / what changed from the old version

- **Tracking is in More Actions, not the invoice body.** The old skill read the
  `1Z…` off the invoice bottom — these invoices don't render it there. Match on
  EasyPost recipient first; use the More Actions → Shipping Labels tracking only
  to disambiguate same-recipient orders.
- **EasyPost fetch must use the window-stash pattern** — a bare async IIFE in
  `javascript_tool` returns `{}`/`pending`.
- **Preserve existing fees; never delete/re-create them.** The edit view can
  load without the pre-existing Credit Card Fee; verify fees are present before
  saving and re-verify the Total after. Add only the Shipping fee.
- **SHIPPED - BILLABLE has a huge oldest-first backlog** — scan mode can't
  isolate today's shipments; prefer explicit order numbers.
- **Use `www.printavo.com`** for invoice/edit URLs.
- Multi-box rule, 10% markup, Shipping preset (fixed `$`, not taxed), and setting
  status by NAME are unchanged and confirmed correct.
