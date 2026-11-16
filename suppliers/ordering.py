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
import re
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

    def get(self, supplier, style, brand=""):
        key = (supplier, style.upper(), brand.lower() if supplier == "S&S" else "")
        if key not in self.data:
            try:
                sd = (_st.sanmar_side(style, "") if supplier == "SanMar" else _st.ss_side(style, ""))
            except Exception as e:
                sd = {"found": False, "error": str(e)[:150]}
            # S&S lists one style # under several brands (Gildan 18500 + others): the brand in the
            # Printavo description picks the right one.
            if sd.get("error"):
                try:
                    sd = (_st.sanmar_side(style, "") if supplier == "SanMar" else _st.ss_side(style, ""))
                except Exception as e:
                    sd = {"found": False, "error": str(e)[:150]}
            if supplier == "S&S" and len(sd.get("styles", [])) > 1 and brand:
                try:
                    sd2 = _st.ss_side(style, "", brand)
                    if sd2.get("found") and len(sd2.get("styles", [])) <= 1:
                        sd = sd2
                    else:
                        keep = [x for x in sd.get("styles", []) if x.lower().startswith(brand.lower())]
                        if len(keep) == 1:
                            sd = _st.ss_side(style, "", keep[0][: -len(style)].strip() or brand)
                except Exception:
                    pass
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
    res = {"style": style, "color": color, "need": need, "options": {}, "flag": None,
           "desc": " ".join((item.get("desc") or "").split())}
    if not need:
        return None                        # fee / setup / non-garment line
    if not style:
        res["flag"] = "no item # on the line"
        return res
    # Printavo item #s sometimes carry a suffix ("Y500_YT" = youth Y500): fall back to the base style.
    base = re.split(r"[_ ]", style)[0]
    desc = (item.get("desc") or "")
    brand = desc.split(" - ")[0].strip() if " - " in desc and len(desc.split(" - ")[0]) <= 30 else ""
    youth_style = bool(re.search(r"youth|toddler|kid|infant", desc, re.I)
                       or re.match(r"^Y", style.upper()) or re.search(r"\dY|B$|T$", style.upper()))
    for sup in ("SanMar", "S&S"):
        sd = cache.get(sup, style, brand)
        if (not sd.get("found") or not sd.get("by")) and base and base != style:
            sd = cache.get(sup, base, brand)
            if sd.get("found"):
                res["style"] = base
        if (not sd.get("found") or not sd.get("by")) and re.search(r"\d[A-Z]*[MR]$", style.upper()) and len(style) > 3:
            sd2 = cache.get(sup, style[:-1], brand)
            if sd2.get("found") and sd2.get("by"):
                sd = sd2
                res["style"] = style[:-1]
        if not sd.get("found") or not sd.get("by"):
            continue
        picked = _st._pick_colors(list(sd["by"].keys()), color)
        if not picked:
            continue
        _st._NEED.clear()
        _st._NEED.update(need)
        clr = _st._best(picked, sd["by"])[0]
        rows = sd["by"][clr]
        color_c = re.sub(r"\([^)]*\)", " ", color).strip() or color
        sure = (_st._cn(clr) == _st._cn(color_c) or _st._sk(clr) == _st._sk(color_c)
                or (_st.generic_match(clr, color_c)
                    and len({_st._cn(c) for c in sd["by"] if _st.generic_match(c, color_c)}) == 1)
                or (_st.abbr_match(clr, color) and sum(1 for c in sd["by"] if _st.abbr_match(c, color)) == 1))
        cost, short, nextday = 0.0, [], True
        smap = {}
        for z in need:
            if z in rows:
                smap[z] = z
            elif z.startswith("Y") and z[1:] in rows and youth_style:
                smap[z] = z[1:]          # youth style lists youth sizes as S/M/L
            elif z == "OSFA" and len(rows) == 1:
                smap[z] = next(iter(rows))     # caps sold in one size under another name ("M/L")
            elif z + "T" in rows:
                smap[z] = z + "T"        # tall style: Printavo '3XL' = supplier '3XLT'
            else:
                smap[z] = z
        for z, q in need.items():
            r = rows.get(smap[z])
            if not r or r["stock"] < q:
                short.append(z)
                continue
            cost += q * (r["price"] or 0)
            near = sum(qq for n, qq in (r.get("whs") or [])
                       if any(n.lower().startswith(w) for w in NEXT_DAY[sup]))
            if near < q:
                nextday = False
        priced = all((rows.get(smap[z]) or {}).get("price") for z in need)
        res["options"][sup] = {"color": clr, "sure": sure, "cost": round(cost, 2), "short": short,
                               "nextday": nextday and not short, "priced": priced,
                               "skus": {z: (rows.get(smap[z]) or {}).get("sku") or "" for z in need},
                               "sizemap": smap}
    if not res["options"]:
        errs = [s_ for s_ in ("SanMar", "S&S") if (cache.get(s_, style, brand) or {}).get("error")
                and "ambiguous" not in cache.get(s_, style, brand)["error"]]
        res["flag"] = (f"couldn't look it up at {' / '.join(errs)} just now — will retry next build" if errs
                       else "neither supplier carries this style/color")
    if not youth_style and any(z.startswith("Y") for z in need):
        res["flag"] = "youth sizes on an adult item # — use the youth style (e.g. 18000B, PC54Y)"
    return res


def ok(l, s):
    o = l["options"].get(s)
    return bool(o and not o["short"] and o["sure"] and o["priced"])


def choose(order_lines):
    """Rule 6. -> (assignments {supplier: [(line, opt)]}, flagged [(line, why)])"""
    usable = [l for l in order_lines if l and not l["flag"]]
    flagged = [(l, l["flag"]) for l in order_lines if l and l["flag"]]

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
    flagged, other = [], []
    for o in orders:
        asg, fl = choose(o["lines"])
        if not o.get("promo"):                 # promo items are ordered through the promo flow
            for l, why in fl:
                flagged.append((o, l, why))
        for s, lines in asg.items():
            if supplier_filter and s != supplier_filter:
                other.append((s, o, round(sum(x[1]["cost"] for x in lines), 2)))
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
            # The clock runs from approval (first day in the queue), never from the production date (Luke, Nov 15).
            if age >= MAX_WAIT_BDAYS:
                reason = f"day {age} — max wait"
            p["age"], p["reason"] = age, reason
            if reason:
                due.append(p)
        if not due:
            for p in pool:
                held.append((s, p, f"pool ${pool_total:,.2f} < $200 — day {p['age']} of {MAX_WAIT_BDAYS}"
                             + _due_warn(p["order"], today)))
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
                    held.append((s, p, f"pool < $200 — day {p['age']} of {MAX_WAIT_BDAYS}" + _due_warn(p["order"], today)))
        else:
            pos.append({"supplier": s, "parts": pool,
                        "why": f"pool ${pool_total:,.2f} < $200 — shipping anyway, paying freight ("
                               + ", ".join(f"#{p['order']['vid']} {p['reason']}" for p in due) + ")"})
    # SanMar PO numbers max 28 chars -> at most 5 orders per PO (Nov 10).
    split = []
    for po in pos:
        parts, n = po["parts"], MAX_ORDERS_PER_PO
        while n > 1 and len("-".join(str(p["order"]["vid"]) for p in parts[:n])) > PO_NUM_MAX:
            n -= 1
        chunks = [parts[i:i + n] for i in range(0, len(parts), n)]
        for k, ch in enumerate(chunks, 1):
            split.append({**po, "parts": ch,
                          "why": po["why"] + (f" · part {k}/{len(chunks)} (max {n} orders per PO)" if len(chunks) > 1 else "")})
    pos = split
    for po in pos:
        po["total"] = round(sum(p["total"] for p in po["parts"]), 2)
        po["po_number"] = po_number(po)
    return {"pos": pos, "held": held, "flagged": flagged, "other": other}


MAX_ORDERS_PER_PO = 5
DUE_WARN_BDAYS = 5


def _due_warn(order, today):
    """Customer due date in jeopardy while the order waits for free freight -> flag only (Luke, Nov 15)."""
    d = order.get("due")
    if d and d <= add_business_days(today, DUE_WARN_BDAYS):
        return f" · ⚠ due {d:%a %m/%d} — `include #{order['vid']}` to order it today"
    return ""
PO_NUM_MAX = 28


def po_number(po):
    """Order #s only, dash-joined (Nov 10) — the goods notes / COGS key off these."""
    return "-".join(str(p["order"]["vid"]) for p in po["parts"])


def _sizes_txt(need):
    order = _st._ORDER
    return " ".join(f"{z}{need[z]}" for z in sorted(need, key=lambda z: (order.index(z) if z in order else 99, z)))


def render(result, title):
    out = [title]
    if not result["pos"] and not result["held"] and not result["flagged"] and not result.get("other"):
        return title + "\nNothing needs ordering."
    if not result["pos"]:
        out.append("*Nothing to order from this supplier right now* — no ✅ needed.")
    for po in result["pos"]:
        out.append(f"\n*{po['supplier']} PO \"{po['po_number']}\"* — ${po['total']:,.2f} · {po['why']}")
        for p in po["parts"]:
            o = p["order"]
            out.append(f"  #{o['vid']} {o.get('company') or ''} — {o.get('nick') or ''}"
                       + (f" · due {o['due']:%a %m/%d}" if o.get("due") else "")
                       + (f"\n    ⚠ already in *{o['lane']}* with no goods PO — goods on hand? `skip #{o['vid']}` if so"
                          if o.get("lane") else ""))
            for l, opt in p["lines"]:
                nd = "" if opt["nextday"] else "  ⚠ not next-day"
                desc = f" · {l['desc'][:45]}" if l.get("desc") else ""
                pcs = sum(l["need"].values())
                out.append(f"    *{l['style']}*{desc} · {opt['color']} — {_sizes_txt(l['need'])} = {pcs} pcs · "
                           f"${opt['cost']:,.2f}{nd}" + (f"\n      {l['dup']}" if l.get("dup") else ""))
    if result["held"]:
        out.append("\n*Holding (pooling for free freight):*")
        for s, p, why in result["held"]:
            o = p["order"]
            out.append(f"  {s} · #{o['vid']} {o.get('nick') or ''} ${p['total']:,.2f} — {why}")
    if result.get("other"):
        out.append("\n*Cheaper / better at the other supplier — goes on that supplier's build:*")
        for sup, o, tot in result["other"]:
            nxt = "SanMar build, next weekday 10:30" if sup == "SanMar" else "S&S build, 2:15"
            out.append(f"  #{o['vid']} {o.get('nick') or ''} ${tot:,.2f} → {nxt}")
    if result["flagged"]:
        out.append("\n*Order by hand (not at SanMar/S&S) or fix in Printavo:*")
        for o, l, why in result["flagged"]:
            desc = f" {l['desc'][:35]}" if l.get("desc") else ""
            out.append(f"  #{o['vid']} {l['style']}{desc} {l['color']} {_sizes_txt(l['need'])} — {why}")
    return "\n".join(out)
