"""Blank-goods COGS per Printavo order from supplier invoices (SanMar, S&S).

Rules (Luke, Nov 6 2026):
- A PO carries the Printavo order number of every order it covers ("7458 PiFi-7427 GOTR-Merch").
- PO with no order number (samples SanMar placed for a client, e.g. "10302026") -> skipped.
- Single-order PO -> the whole invoice (goods + freight + tax) belongs to that order.
- Combined PO -> each invoice line goes to the order that ordered that style (then size);
  a line two orders both ordered is split by their Printavo quantities and flagged.
  Lines on no order: if exactly one order on the PO matched nothing, they are its goods;
  if the PO says Merch/Luke/Sample/Stock they are shop overhead (not order COGS);
  otherwise they are spread over the PO's orders by goods share and flagged.
  Freight/tax/fees are split by goods share.
No Printavo imports: the caller passes get_items(order_no) -> list of
{"style","color","sizes":{size:qty}} so this lifts out if the shop system changes.
"""
import re
from collections import defaultdict

_ORDER_RE = re.compile(r"(?<!\d)(\d{4,5})(?!\d)")
ORDER_MIN, ORDER_MAX = 5000, 20000
OVERHEAD_WORDS = ("MERCH", "LUKE", "SAMPLE", "STOCK")


def order_numbers(po):
    nums = []
    for m in _ORDER_RE.findall(po or ""):
        n = int(m)
        if ORDER_MIN <= n <= ORDER_MAX and str(n) not in nums:
            nums.append(str(n))
    return nums


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _core(s):
    """Style code from its first digit on: NL3600 -> 3600, BC3001CVC -> 3001CVC."""
    n = _norm(s)
    m = re.search(r"\d.*", n)
    return m.group(0) if m else n


def _style_match(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 3 and (long_.endswith(short) or long_.startswith(short)):
        return True
    ca, cb = _core(a), _core(b)
    return len(ca) >= 3 and ca == cb


def _size_key(s):
    k = _norm(s)
    if k.startswith("SIZE"):
        k = k[4:]
    return {"XXL": "2XL", "XXXL": "3XL", "XXXXL": "4XL"}.get(k, k)


def _size_qty(item, size):
    s = _size_key(size)
    return sum(float(v or 0) for k, v in (item.get("sizes") or {}).items() if _size_key(k) == s)


def allocate(invoices, get_items):
    """-> (per_order, flags, skipped)
    per_order[order] = {"cogs": float, "parts": [(vendor, invoice, po, amount)]}
    per_order["OVERHEAD"] collects shop-merch lines from combined POs."""
    per_order = defaultdict(lambda: {"cogs": 0.0, "parts": []})
    flags, skipped = [], []
    for inv in invoices:
        orders = order_numbers(inv["po"])
        total = round(inv["goods"] + inv["freight"] + inv["tax"], 2) if inv["goods"] else round(inv["total"], 2)
        tag = f"{inv['vendor']} {inv['invoice']} PO \"{inv['po']}\""
        if not orders:
            skipped.append(f"{tag} ${total:,.2f} — no order number, skipped")
            continue
        if len(orders) == 1:
            o = orders[0]
            per_order[o]["cogs"] += total
            per_order[o]["parts"].append((inv["vendor"], inv["invoice"], inv["po"], total))
            continue
        if not inv["lines"]:
            for o in orders:
                per_order[o]["cogs"] += total / len(orders)
                per_order[o]["parts"].append((inv["vendor"], inv["invoice"], inv["po"], round(total / len(orders), 2)))
            flags.append(f"{tag}: no line detail — split evenly")
            continue
        items = {o: (get_items(o) or []) for o in orders}
        goods_by = defaultdict(float)
        unmatched = []
        for li in inv["lines"]:
            cands = [o for o in orders if any(_style_match(li["style"], it.get("style")) for it in items[o])]
            if len(cands) > 1:
                sized = {o: sum(_size_qty(it, li["size"]) for it in items[o] if _style_match(li["style"], it.get("style")))
                         for o in cands}
                sized = {o: q for o, q in sized.items() if q > 0}
                if len(sized) == 1:
                    cands = list(sized)
                elif sized:
                    tq = sum(sized.values())
                    for o, q in sized.items():
                        goods_by[o] += li["amount"] * q / tq
                    flags.append(f"{tag}: {li['style']} {li['color']} {li['size']} ×{li['qty']:g} on "
                                 f"{', '.join(sized)} — split by Printavo qty")
                    continue
            if len(cands) == 1:
                goods_by[cands[0]] += li["amount"]
            elif cands:
                for o in cands:
                    goods_by[o] += li["amount"] / len(cands)
                flags.append(f"{tag}: {li['style']} {li['size']} matched {', '.join(cands)} by style only — split evenly")
            else:
                unmatched.append(li)
        um_amt = sum(li["amount"] for li in unmatched)
        overhead = 0.0
        if unmatched:
            desc = "; ".join(f"{li['style']} {li['color']} {li['size']} ×{li['qty']:g}" for li in unmatched)
            empty = [o for o in orders if goods_by.get(o, 0) == 0 and items[o]]
            if len(empty) == 1:
                goods_by[empty[0]] += um_amt
                flags.append(f"{tag}: unmatched lines → {empty[0]} (only order with no match): {desc}")
            elif any(w in _norm(inv["po"]) for w in OVERHEAD_WORDS):
                overhead = um_amt
                flags.append(f"{tag}: ${um_amt:,.2f} shop merch/overhead, not order COGS: {desc}")
            else:
                g0 = sum(goods_by.values())
                for o in (list(goods_by) if g0 else orders):
                    goods_by[o] += um_amt * (goods_by[o] / g0 if g0 else 1 / len(orders))
                flags.append(f"{tag}: ${um_amt:,.2f} on no Printavo order, spread by share — check: {desc}")
        base = sum(goods_by.values()) + overhead or 1.0
        extra = total - base   # freight + tax + fees (+ goods/line rounding)
        for o, amt in goods_by.items():
            share = round(amt + extra * amt / base, 2)
            per_order[o]["cogs"] += share
            per_order[o]["parts"].append((inv["vendor"], inv["invoice"], inv["po"], share))
        if overhead:
            share = round(overhead + extra * overhead / base, 2)
            per_order["OVERHEAD"]["cogs"] += share
            per_order["OVERHEAD"]["parts"].append((inv["vendor"], inv["invoice"], inv["po"], share))
    for o in per_order:
        per_order[o]["cogs"] = round(per_order[o]["cogs"], 2)
    return dict(per_order), flags, skipped
