"""Auto-ordering blanks — SHADOW MODE (Luke, Nov 7 2026). Builds the SanMar / S&S POs the
shop WOULD place today from Printavo orders that need goods; places nothing.
Spec: project doc claude/Auto_Ordering_Design_Nov2026.md.

Rules (agreed with Luke):
  - Needs goods = direct order at ORDER GOODS or later (pre-lane) with no SanMar/S&S PO
    containing its order #. Exactly the Printavo quantities — no overs.
  - One supplier per order where possible: cheapest that fills the whole order for next-day
    delivery; split only when one can't cover it. Unclean matches are flagged, never guessed.
  - Per supplier: order >= $200 -> own PO. < $200 -> pool; pool >= $200 -> one combined PO.
    Pool < $200 holds up to 2 business days from first seen, then ships (paying freight) —
    riding along on a >= $200 PO to that supplier first if one is going. Production date
    overrides the wait.
"""
from datetime import date, timedelta

from suppliers import stock as _st
from suppliers.goods import add_business_days

FREE_FREIGHT = 200.0
MAX_WAIT_BDAYS = 2
NEXT_DAY = {"SanMar": ("cincinnati",), "S&S": ("il", "ks", "ky")}   # 1 business day to STL


def bdays_between(a, b):
    """Business days from a to b (a < b)."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _size(s):
    s = (s or "").strip().upper()
    if s.startswith("SIZE_"):
        s = s[5:]
    elif s.startswith("SIZE"):
        s = s[4:]
    return _st.norm_size(s)


class StyleCache:
    """One supplier lookup per style per build (all colors), reused across orders."""

    def __init__(self):
        self.data = {}

    def get(self, supplier, style):
        key = (supplier, style.upper())
        if key not in self.data:
            try:
                sd = (_st.sanmar_side(style, "") if supplier == "SanMar" else _st.ss_side(style, ""))
            except Exception as e:
                sd = {"found": False, "error": str(e)[:150]}
            if supplier == "S&S" and len(sd.get("styles", [])) > 1:
                sd = {"found": False, "error": f"ambiguous at S&S ({', '.join(sd['styles'][:3])})"}
            self.data[key] = sd
        return self.data[key]


def match_line(item, cache):
    """-> {style, color, need, options: {supplier: {...}}, flag}"""
    style = (item.get("style") or "").strip()
    color = (item.get("color") or "").strip()
    need = {}
    for k, v in (item.get("sizes") or {}).items():
        q = int(v or 0)
        if q > 0:
            z = _size(k)
            need[z] = need.get(z, 0) + q
    res = {"style": style, "color": color, "need": need, "options": {}, "flag": None}
    if not need:
        return None                        # fee / setup / non-garment line
    if not style:
        res["flag"] = "no item # on the line"
        return res
    for sup in ("SanMar", "S&S"):
        sd = cache.get(sup, style)
        if not sd.get("found") or not sd.get("by"):
            continue
        picked = _st._pick_colors(list(sd["by"].keys()), color)
        if not picked:
            continue
        _st._NEED.clear()
        _st._NEED.update(need)
        clr = _st._best(picked, sd["by"])[0]
        rows = sd["by"][clr]
        sure = _st._cn(clr) == _st._cn(color) or _st._sk(clr) == _st._sk(color)
        cost, short, nextday = 0.0, [], True
        for z, q in need.items():
            r = rows.get(z)
            if not r or r["stock"] < q:
                short.append(z)
                continue
            cost += q * (r["price"] or 0)
            near = sum(qq for n, qq in (r.get("whs") or [])
                       if any(n.lower().startswith(w) for w in NEXT_DAY[sup]))
            if near < q:
                nextday = False
        priced = all((rows.get(z) or {}).get("price") for z in need)
        res["options"][sup] = {"color": clr, "sure": sure, "cost": round(cost, 2), "short": short,
                               "nextday": nextday and not short, "priced": priced}
    if not res["options"]:
        res["flag"] = "neither supplier carries this style/color"
    return res


def choose(order_lines):
    """Rule 6. -> (assignments {supplier: [(line, opt)]}, flagged [(line, why)])"""
    usable = [l for l in order_lines if l and not l["flag"]]
    flagged = [(l, l["flag"]) for l in order_lines if l and l["flag"]]

    def ok(l, s):
        o = l["options"].get(s)
        return o and not o["short"] and o["sure"] and o["priced"]

    whole = []
    for s in ("SanMar", "S&S"):
        if usable and all(ok(l, s) for l in usable):
            whole.append((not all(l["options"][s]["nextday"] for l in usable),
                          sum(l["options"][s]["cost"] for l in usable), s))
    if whole:
        s = sorted(whole)[0][2]
        return {s: [(l, l["options"][s]) for l in usable]}, flagged
    asg = {}
    for l in usable:
        cands = [(not o["nextday"], o["cost"], s) for s, o in l["options"].items() if ok(l, s)]
        if cands:
            s = sorted(cands)[0][2]
            asg.setdefault(s, []).append((l, l["options"][s]))
            continue
        o = min(l["options"].values(), key=lambda x: (len(x["short"]), x["cost"]))
        if o["short"]:
            why = f"short at both suppliers ({', '.join(o['short'])})"
        elif not o["sure"]:
            why = f"color check: Printavo '{l['color']}' ≈ supplier '{o['color']}'"
        else:
            why = "no price returned"
        flagged.append((l, why))
    return asg, flagged


def plan(orders, today, first_seen, supplier_filter=None):
    """orders: [{vid, nick, company, prod (date|None), lines: [match_line results]}]
    -> {"pos": [...], "held": [...], "flagged": [...]}"""
    by_sup = {}
    flagged = []
    for o in orders:
        asg, fl = choose(o["lines"])
        for l, why in fl:
            flagged.append((o, l, why))
        for s, lines in asg.items():
            if supplier_filter and s != supplier_filter:
                continue
            by_sup.setdefault(s, []).append({"order": o, "lines": lines,
                                             "total": round(sum(x[1]["cost"] for x in lines), 2)})
    pos, held = [], []
    for s, parts in by_sup.items():
        own = [p for p in parts if p["total"] >= FREE_FREIGHT]
        pool = [p for p in parts if p["total"] < FREE_FREIGHT]
        for p in own:
            pos.append({"supplier": s, "parts": [p], "why": f"own PO ${p['total']:,.2f}"})
        if not pool:
            continue
        pool_total = sum(p["total"] for p in pool)
        if pool_total >= FREE_FREIGHT:
            pos.append({"supplier": s, "parts": pool,
                        "why": f"pooled {len(pool)} orders ${pool_total:,.2f} — free freight"})
            continue
        due = []
        for p in pool:
            o = p["order"]
            seen = first_seen.get(str(o["vid"]), today)
            age = bdays_between(seen, today)
            reason = None
            if age >= MAX_WAIT_BDAYS:
                reason = f"day {age} — max wait"
            elif o.get("prod") and add_business_days(today, 3) > o["prod"]:
                reason = f"prod date {o['prod']:%a %m/%d}"
            p["age"], p["reason"] = age, reason
            if reason:
                due.append(p)
        if not due:
            for p in pool:
                held.append((s, p, f"pool ${pool_total:,.2f} < $200 — day {p['age']} of {MAX_WAIT_BDAYS}"))
            continue
        if own:
            host = max(own, key=lambda p: p["total"])
            for po in pos:
                if po["parts"][0] is host:
                    po["parts"] += due
                    po["why"] += " + ride-along " + ", ".join(
                        f"#{p['order']['vid']} ({p['reason']})" for p in due)
            for p in pool:
                if p not in due:
                    held.append((s, p, f"pool < $200 — day {p['age']} of {MAX_WAIT_BDAYS}"))
        else:
            pos.append({"supplier": s, "parts": pool,
                        "why": f"pool ${pool_total:,.2f} < $200 — shipping anyway, paying freight ("
                               + ", ".join(f"#{p['order']['vid']} {p['reason']}" for p in due) + ")"})
    for po in pos:
        po["total"] = round(sum(p["total"] for p in po["parts"]), 2)
        po["po_number"] = po_number(po)
    return {"pos": pos, "held": held, "flagged": flagged}


def po_number(po):
    """Every order # in the PO (the goods notes / COGS key off this)."""
    vids = [str(p["order"]["vid"]) for p in po["parts"]]
    if len(vids) == 1:
        nick = "".join(ch for ch in (po["parts"][0]["order"].get("nick") or "") if ch.isalnum() or ch == " ")
        return f"{vids[0]} {nick.strip()[:14].strip()}".strip()
    return "-".join(vids)


def _sizes_txt(need):
    order = _st._ORDER
    return " ".join(f"{z}{need[z]}" for z in sorted(need, key=lambda z: (order.index(z) if z in order else 99, z)))


def render(result, title):
    out = [title]
    if not result["pos"] and not result["held"] and not result["flagged"]:
        return title + "\nNothing needs ordering."
    for po in result["pos"]:
        out.append(f"\n*{po['supplier']} PO \"{po['po_number']}\"* — ${po['total']:,.2f} · {po['why']}")
        for p in po["parts"]:
            o = p["order"]
            out.append(f"  #{o['vid']} {o.get('company') or ''} — {o.get('nick') or ''}"
                       + (f" · prod {o['prod']:%a %m/%d}" if o.get("prod") else ""))
            for l, opt in p["lines"]:
                nd = "" if opt["nextday"] else "  ⚠ not next-day"
                out.append(f"    {l['style']} {opt['color']}: {_sizes_txt(l['need'])} · ${opt['cost']:,.2f}{nd}")
    if result["held"]:
        out.append("\n*Holding (pooling for free freight):*")
        for s, p, why in result["held"]:
            o = p["order"]
            out.append(f"  {s} · #{o['vid']} {o.get('nick') or ''} ${p['total']:,.2f} — {why}")
    if result["flagged"]:
        out.append("\n*Flagged — order by hand or fix in Printavo:*")
        for o, l, why in result["flagged"]:
            out.append(f"  #{o['vid']} {l['style']} {l['color']} {_sizes_txt(l['need'])} — {why}")
    return "\n".join(out)
