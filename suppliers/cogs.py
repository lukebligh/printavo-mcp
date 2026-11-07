"""Blank-goods COGS per Printavo order from supplier invoices (SanMar, S&S).

Rules (Luke, Nov 6 2026):
- A PO carries the Printavo order number of every order it covers ("7458 PiFi-7427 GOTR-Merch").
- PO with no order number (samples SanMar placed for a client, e.g. "10302026") -> skipped.
- Single-order PO -> the whole invoice (goods + freight + tax) belongs to that order.
- Combined PO -> each invoice line goes to the order that ordered that style (then size);
  a line two orders both ordered is split by their Printavo quantities and flagged;
  freight/tax/fees are split by each order's share of goods.
No Printavo imports: the caller passes get_items(order_no) -> list of
{"style","color","sizes":{size:qty}} so this lifts out if the shop system changes.
"""
import re
from collections import defaultdict

_ORDER_RE = re.compile(r"(?<!\d)(\d{4,5})(?!\d)")
ORDER_MIN, ORDER_MAX = 5000, 20000


def order_numbers(po):
    nums = []
    for m in _ORDER_RE.findall(po or ""):
        n = int(m)
        if ORDER_MIN <= n <= ORDER_MAX and str(n) not in nums:
            nums.append(str(n))
    return nums


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _style_match(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and (long_.endswith(short) or long_.startswith(short))


def _size_qty(item, size):
    s = _norm(size)
    for k, v in (item.get("sizes") or {}).items():
        if _norm(k) == s or _norm(k).replace("XXL", "2XL") == s.replace("XXL", "2XL"):
            return float(v or 0)
    return 0.0


def allocate(invoices, get_items):
    """-> (per_order, flags, skipped)
    per_order[order] = {"cogs": float, "parts": [(vendor, invoice, po, amount)]}"""
    per_order = defaultdict(lambda: {"cogs": 0.0, "parts": []})
    flags, skipped = [], []
    for inv in invoices:
        orders = order_numbers(inv["po"])
        total = round(inv["goods"] + inv["freight"] + inv["tax"], 2) if inv["goods"] else round(inv["total"], 2)
        if not orders:
            skipped.append(f"{inv['vendor']} {inv['invoice']} PO \"{inv['po']}\" ${total:,.2f} — no order number, skipped")
            continue
        if len(orders) == 1:
            o = orders[0]
            per_order[o]["cogs"] += total
            per_order[o]["parts"].append((inv["vendor"], inv["invoice"], inv["po"], total))
            continue
        # Combined PO: split line by line.
        items = {o: (get_items(o) or []) for o in orders}
        goods_by = defaultdict(float)
        unmatched = 0.0
        for li in inv["lines"]:
            cands = [o for o in orders if any(_style_match(li["style"], it.get("style")) for it in items[o])]
            if len(cands) > 1:
                by_size = {o: sum(_size_qty(it, li["size"]) for it in items[o]
                                  if _style_match(li["style"], it.get("style"))) for o in cands}
                sized = {o: q for o, q in by_size.items() if q > 0}
                if len(sized) == 1:
                    cands = list(sized)
                elif sized:
                    tq = sum(sized.values())
                    for o, q in sized.items():
                        goods_by[o] += li["amount"] * q / tq
                    flags.append(f"{inv['vendor']} {inv['invoice']} PO \"{inv['po']}\": {li['style']} {li['color']} "
                                 f"{li['size']} ×{li['qty']:g} is on {', '.join(sized)} — split by Printavo qty")
                    continue
            if len(cands) == 1:
                goods_by[cands[0]] += li["amount"]
            elif cands:   # several orders have the style, none has the size: split evenly
                for o in cands:
                    goods_by[o] += li["amount"] / len(cands)
                flags.append(f"{inv['vendor']} {inv['invoice']} PO \"{inv['po']}\": {li['style']} {li['size']} "
                             f"matched {', '.join(cands)} by style only — split evenly")
            else:
                unmatched += li["amount"]
                flags.append(f"{inv['vendor']} {inv['invoice']} PO \"{inv['po']}\": {li['style']} {li['color']} "
                             f"{li['size']} ×{li['qty']:g} (${li['amount']:,.2f}) not on any Printavo order {', '.join(orders)}")
        matched = sum(goods_by.values())
        if unmatched:
            if matched:
                for o in list(goods_by):
                    goods_by[o] += unmatched * goods_by[o] / matched
            else:
                for o in orders:
                    goods_by[o] += unmatched / len(orders)
        g = sum(goods_by.values()) or 1.0
        extra = total - g if inv["lines"] else 0.0   # freight + tax + fees (+ rounding)
        if not inv["lines"]:
            for o in orders:
                goods_by[o] = total / len(orders)
            flags.append(f"{inv['vendor']} {inv['invoice']} PO \"{inv['po']}\": no line detail — split evenly")
            g = total or 1.0
        for o, amt in goods_by.items():
            share = round(amt + extra * amt / g, 2)
            per_order[o]["cogs"] += share
            per_order[o]["parts"].append((inv["vendor"], inv["invoice"], inv["po"], share))
    for o in per_order:
        per_order[o]["cogs"] = round(per_order[o]["cogs"], 2)
    return dict(per_order), flags, skipped
