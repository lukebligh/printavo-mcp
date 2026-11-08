"""Goods status for one shop order, from SanMar + S&S, as a production-note block.

No Printavo imports. Caller supplies: the order number, its production date, the
SanMar POs for it, a SanMar open-status map, per-PO SanMar shipment/invoice lookups,
and the S&S recent orders. Output: note block text + exception list.
"""
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta

BEGIN, END = "— GOODS (auto", "— END GOODS —"
_BLOCK_RE = re.compile(r"\n*— GOODS \(auto[^\n]*—.*?— END GOODS —\n*", re.S)

# UPS Ground business days from each SanMar origin to St. Louis (estimate).
_TRANSIT = {"OH": 1, "MN": 2, "TX": 2, "VA": 2, "NJ": 2, "FL": 3, "AZ": 3, "NV": 4, "WA": 4}


def strip_block(note):
    return _BLOCK_RE.sub("\n", note or "").strip()


def merge_block(note, block):
    """Replace (or append) the GOODS block; everything else in the note is kept."""
    rest = strip_block(note)
    return (rest + "\n\n" + block).strip() if rest else block


def block_body(note):
    """The GOODS block minus its timestamp line, for change detection."""
    m = _BLOCK_RE.search(note or "")
    if not m:
        return ""
    return "\n".join(m.group(0).strip().splitlines()[1:])


def add_business_days(d, n):
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _d(s):
    try:
        return datetime.fromisoformat(str(s)[:10]).date()
    except (TypeError, ValueError):
        return None


def _sizes(items):
    """[(style,color,size,qty)] -> 'STYLE Color: S20 M30 · ...' grouped lines."""
    order = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "6XL"]
    g = OrderedDict()
    for st, co, sz, q in items:
        sz = {"XXL": "2XL", "XXXL": "3XL", "XXXXL": "4XL"}.get((sz or "").upper(), sz)
        g.setdefault((st, co), OrderedDict())
        g[(st, co)][sz] = g[(st, co)].get(sz, 0) + q
    out = []
    for (st, co), sz in g.items():
        if not co:                                    # not invoiced yet → no color/size detail
            out.append(f"  {st}: {int(sum(sz.values()))} pcs (sizes show once SanMar invoices)")
            continue
        ks = sorted(sz, key=lambda k: (order.index(k) if k in order else 99, k))
        out.append(f"  {st} {co}: " + " ".join(f"{k}{int(sz[k]) if float(sz[k]).is_integer() else sz[k]}" for k in ks))
    return out


def sanmar_po_section(po, status, ships, invs):
    """status: list of PromoStandards order-status dicts (may be []), ships: shipment
    boxes, invs: SanMar invoices. -> (lines, info) info={shipped, eta, backorder, hold, open}"""
    names = {}
    for inv in invs:
        for li in inv["lines"]:
            names[li["part_id"]] = (li["style"], li["color"], li["size"])
    st = (status[0]["status"] if status else "").lower()
    issues = sorted({i for s in status for i in (s.get("issues") or []) + ([s["issue"]] if s.get("issue") else [])})
    info = {"shipped": bool(ships), "eta": None, "backorder": "backOrderHold" in issues,
            "hold": "generalHold" in issues, "open": bool(status) and st not in ("complete", "canceled"),
            "canceled": st == "canceled", "partial": "partial" in st, "tracking": []}
    lines = []
    if ships:
        by_trk = OrderedDict()
        for b in ships:
            by_trk.setdefault(b["tracking"], b)
        last_ship = max((_d(b["ship_date"]) for b in ships if _d(b["ship_date"])), default=None)
        origin = ships[0]["from"]
        state = (origin.split()[-1] if origin else "").upper()
        if last_ship:
            info["eta"] = add_business_days(last_ship, _TRANSIT.get(state, 3))
        pcs = sum(int(float(i["qty"] or 0)) for b in ships for i in b["items"])
        label = "PARTIALLY SHIPPED" if "partial" in st else "SHIPPED"
        info["tracking"] = [t for t in by_trk if t]
        info["carriers"] = {t: (b.get("carrier") or "") for t, b in by_trk.items() if t}
        trk = ", ".join(t for t in by_trk if t)
        cars = {c.split()[0].upper() for c in info["carriers"].values() if c}
        trk_label = "UPS" if cars <= {"UPS"} else "/".join(sorted(c.title() for c in cars))
        lines.append(f"SanMar \"{po}\": {label} {last_ship:%m/%d} from {(' '.join(origin.split()[:-1]).title() + ' ' + state).strip()} · "
                     f"{pcs} pcs, {len(ships)} box{'es' if len(ships) != 1 else ''} · "
                     f"est. arrival {info['eta']:%a %m/%d}" if last_ship else f"SanMar \"{po}\": {label}")
        if trk:
            lines.append(f"  {trk_label}: {trk}")
        items = []
        for b in ships:
            for i in b["items"]:
                stl, co, sz = names.get(i["part_id"], (i["style"], "", ""))
                items.append((stl, co, sz, float(i["qty"] or 0)))
        lines += _sizes(items)
    else:
        label = {"received": "ORDERED — not shipped yet", "confirmed": "ORDERED — confirmed, not shipped",
                 "canceled": "CANCELED"}.get(st, (st.upper() or "ORDERED") + " — not shipped yet")
        lines.append(f"SanMar \"{po}\": {label}")
    for s in status:
        short = [l for l in s["lines"] if l["ordered"] and float(l["shipped"] or 0) < float(l["ordered"] or 0)]
        if short and ships:
            info["partial"] = True
            lines.append("  Not shipped yet: " + ", ".join(
                f"{' '.join(x for x in names.get(l['part_id'], (l['style'], '', '')) if x)} "
                f"{int(float(l['ordered']) - float(l['shipped'] or 0))}" for l in short[:12]))
    if info["backorder"]:
        lines.append("  ⚠ BACKORDER at SanMar")
    if info["hold"]:
        lines.append("  ⚠ ON HOLD at SanMar")
    return lines, info


def ss_po_section(o):
    st = str(o.get("orderStatus") or "").strip()
    shipped = bool(o.get("trackingNumber")) or st.lower() in ("shipped", "invoiced", "complete", "completed")
    eta = _d(o.get("expectedDeliveryDate"))
    info = {"shipped": shipped, "eta": eta, "backorder": "back" in st.lower(), "hold": "hold" in st.lower(),
            "open": not shipped, "canceled": "cancel" in st.lower(), "partial": False,
            "tracking": [t.strip() for t in str(o.get("trackingNumber") or "").replace(";", ",").split(",") if t.strip()]}
    info["carriers"] = {t: str(o.get("shippingCarrier") or "") for t in info["tracking"]}
    head = f"S&S \"{o.get('poNumber')}\": {st.upper() or 'ORDERED'}"
    if _d(o.get("shipDate")) and shipped:
        head += f" {_d(o.get('shipDate')):%m/%d}"
    if eta:
        head += f" · ETA {eta:%a %m/%d}"
    lines = [head]
    if o.get("trackingNumber"):
        car = str(o.get("shippingCarrier") or "").strip()
        lines.append(f"  {car + ' ' if car and car.upper() != 'UPS' else ''}Tracking: {o['trackingNumber']}")
    items = [(str(li.get("styleName") or ""), str(li.get("colorName") or ""), str(li.get("sizeName") or ""),
              float(li.get("qtyShipped") or li.get("qtyOrdered") or 0)) for li in (o.get("lines") or [])]
    lines += _sizes([i for i in items if i[3]])
    short = [li for li in (o.get("lines") or [])
             if shipped and float(li.get("qtyShipped") or 0) < float(li.get("qtyOrdered") or 0)]
    if short:
        lines.append("  Not shipped: " + ", ".join(
            f"{li.get('styleName')} {li.get('colorName')} {li.get('sizeName')} "
            f"{int(float(li.get('qtyOrdered') or 0) - float(li.get('qtyShipped') or 0))}" for li in short[:12]))
        info["backorder"] = True
        info["partial"] = True
    if info["backorder"]:
        lines.append("  ⚠ BACKORDER / SHORT at S&S")
    return lines, info


def exceptions_for(order, prod_date, infos, has_po, today=None):
    """-> list of (key, text). Keys are stable so each alert is sent once."""
    today = today or date.today()
    out = []
    if not has_po:
        out.append((f"{order}|nopo", f"#{order}: no SanMar or S&S PO with this order # — "
                                      f"goods not ordered yet? (ignore if another vendor / customer-supplied)"))
        return out
    for po, inf in infos:
        if inf["backorder"]:
            out.append((f"{order}|{po}|backorder", f"#{order}: BACKORDER/short on \"{po}\" — customer may need a heads-up"))
        if inf["hold"]:
            out.append((f"{order}|{po}|hold", f"#{order}: \"{po}\" is ON HOLD at the supplier"))
        if prod_date:
            if inf["eta"] and inf["eta"] > prod_date:
                out.append((f"{order}|{po}|late|{inf['eta']}",
                            f"#{order}: \"{po}\" arrives ~{inf['eta']:%a %m/%d}, after the production date "
                            f"{prod_date:%a %m/%d}"))
            elif not inf["shipped"] and inf["open"] and add_business_days(today, 3) > prod_date:
                out.append((f"{order}|{po}|notshipped",
                            f"#{order}: \"{po}\" hasn't shipped and production is {prod_date:%a %m/%d}"))
    return out
