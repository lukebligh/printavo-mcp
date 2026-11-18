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
# Approx. UPS Ground business days to St. Louis, nearest first (Luke, Nov 16: closer is better, any DC is fine).
TRANSIT = {"SanMar": {"cincinnati": 1, "minneapolis": 2, "dallas": 2, "richmond": 2, "robbinsville": 3,
                      "jacksonville": 3, "phoenix": 4, "reno": 4, "seattle": 5},
           "S&S": {"il": 1, "ks": 1, "ky": 1, "oh": 2, "tx": 2, "ga": 2, "pa": 3, "nj": 3, "fl": 3, "nv": 4, "wa": 5}}


def _ship_from(sup, rows, smap, need, short):
    """Nearest DC that can fill each size; the line ships as slow as its slowest size."""
    worst, name = 0, ""
    for z, qty in need.items():
        if z in short:
            continue
        cands = []
        for n, have in (rows.get(smap[z]) or {}).get("whs") or []:
            if have >= qty:
                d = next((v for k, v in TRANSIT[sup].items() if n.lower().startswith(k)), 3)
                cands.append((d, n))
        if not cands:
            return "ships from 2+ DCs"
        d, n = min(cands)
        if d > worst:
            worst, name = d, n
    if not name:
        return ""
    return f"ships {name.split(',')[0]} (~{worst} day{'s' if worst != 1 else ''})"


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
                               "stock": {z: int((rows.get(smap[z]) or {}).get("stock") or 0) for z in need},
                               "ship": _ship_from(sup, rows, smap, need, short),
                               "sizemap": smap}
    if not res["options"]:
        errs = [s_ for s_ in ("SanMar", "S&S") if (cache.get(s_, style, brand) or {}).get("error")
                and "ambiguous" not in cache.get(s_, style, brand)["error"]]
        res["flag"] = (f"couldn't look it up at {' / '.join(errs)} just now — will retry next build" if errs
                       else "not a SanMar or S&S item")
    if not youth_style and any(z.startswith("Y") for z in need):
        res["flag"] = "youth sizes on an adult item # — use the youth style (e.g. 18000B, PC54Y)"
    return res


def ok(l, s):
    o = l["options"].get(s)
    return bool(o and not o["short"] and o["sure"] and o["priced"])


# SanMar is the preferred vendor (Luke, Nov 16). An order moves to S&S on price only when S&S can fill
# ALL of it (single source) and saves at least this much — never line by line for pennies.
PRICE_MOVE_MIN = 50.0          # Luke, Nov 16: move whole order to S&S only at >=8% AND >=$50 saved
PRICE_MOVE_PCT = 0.08

# SanMar quote thresholds (Susan Maley, via Luke Nov 16): same piece across sizes at or over these
# quantities -> Mechelle requests a quote; the bot does not place it.
_RETAIL = ("nike", "ogio", "carhartt", "eddie bauer", "north face", "new era", "travismathew", "travis mathew",
           "brooks brothers", "cotopaxi", "tentree", "columbia", "under armour", "tommy bahama", "mercer+mettle",
           "mercer mettle", "outdoor research", "spyder", "volunteer knitwear", "champion", "russell outdoors")


def quote_threshold(line, unit):
    """-> (pieces, label) or None."""
    desc = (line.get("desc") or "").lower()
    brand = desc.split(" - ")[0] if " - " in desc else desc[:25]
    st = (line.get("style") or "").upper()
    if any(b in brand for b in _RETAIL):
        return (50, "retail brand over $39.99") if unit > 39.99 else (100, "retail brand")
    if "sport-tek" in brand or "sport tek" in brand or re.match(r"^(L|Y|T)?ST\d", st):
        if unit > 39:
            return 50, "Sport-Tek over $39"
        return (100, "Sport-Tek $19.99+") if unit >= 19.99 else (200, "Sport-Tek")
    if re.search(r"fleece|hood|sweatshirt|crewneck|pullover|quarter.zip|1/4.zip", desc):
        return 250, "fleece / hoodie"
    if re.search(r"\btee\b|t-shirt|tshirt|\bt shirt", desc):
        return 500, "basic tee"
    return None


def _short_detail(l):
    """Only the sizes the best supplier can't cover: need vs have. SanMar first. The other supplier is
    mentioned whenever it carries the item (even at 0) — never when it doesn't carry it."""
    opts = l["options"]
    cands = [s for s in ("SanMar", "S&S") if opts.get(s) and opts[s]["short"]]
    if not cands:
        return ""
    prim = cands[0]
    other = "S&S" if prim == "SanMar" else "SanMar"
    bits = []
    for z in sorted(opts[prim]["short"], key=lambda z: (_st._ORDER.index(z) if z in _st._ORDER else 99, z)):
        need, have = l["need"][z], opts[prim]["stock"].get(z, 0)
        t = f"{z}: need {need}, {prim} has {have} ({need - have} short)"
        if opts.get(other):                      # the other supplier carries it: say what it has, even 0
            t += f", {other} has {opts[other]['stock'].get(z, 0)}"
        bits.append(t)
    return "; ".join(bits)


def choose(order_lines, release_shorts=False):
    """-> (assignments {supplier: [(line, opt)]}, byhand [(line, why)], holds [text], notes [text])
    SanMar first. Whole order stays at SanMar when SanMar can fill it (any DC). A line goes to S&S only
    when SanMar can't fill it. Any line nobody can fill holds the WHOLE order unless shorts are released."""
    usable = [l for l in order_lines if l and not l["flag"]]
    byhand = [(l, l["flag"]) for l in order_lines if l and l["flag"]]
    holds, notes, asg = [], [], {}
    if usable and all(ok(l, "SanMar") for l in usable):
        sup = "SanMar"
        if all(ok(l, "S&S") for l in usable):
            a = sum(l["options"]["SanMar"]["cost"] for l in usable)
            b = sum(l["options"]["S&S"]["cost"] for l in usable)
            if a - b >= max(PRICE_MOVE_MIN, PRICE_MOVE_PCT * a):
                sup = "S&S"
                notes.append(f"whole order at S&S — saves ${a - b:,.2f} ({(a - b) / a:.0%}) vs SanMar")
        return {sup: [(l, l["options"][sup]) for l in usable]}, byhand, holds, notes
    for l in usable:
        if ok(l, "SanMar"):
            asg.setdefault("SanMar", []).append((l, l["options"]["SanMar"]))
        elif ok(l, "S&S"):
            asg.setdefault("S&S", []).append((l, l["options"]["S&S"]))
            sm = l["options"].get("SanMar")
            notes.append(f"{l['style']} {l['color']} from S&S — SanMar "
                         + ("is short " + ", ".join(sm["short"]) if sm and sm["short"] else "doesn’t carry it"))
        else:
            shorty = [(s, o) for s, o in l["options"].items() if o["short"] and o["sure"] and o["priced"]]
            if shorty:
                txt = f"SHORT · {l['style']} {l['color']} — {_short_detail(l)}"
                if not release_shorts:
                    holds.append(txt)
                    continue
                s, o = sorted(shorty, key=lambda x: (len(x[1]["short"]), x[0] != "SanMar"))[0]
                left = {z: q for z, q in l["need"].items() if z not in o["short"]}
                notes.append("NOT ordering (short, approved): " + txt[8:])
                if left:
                    asg.setdefault(s, []).append((dict(l, need=left), o))
            else:
                o = next(iter(l["options"].values()))
                holds.append(f"COLOR CHECK · {l['style']} — Printavo '{l['color']}' ≈ supplier '{o['color']}'. "
                             "Fix the color in Printavo." if not o["sure"]
                             else f"NO PRICE · {l['style']} {l['color']} — supplier returned no price")
    return asg, byhand, holds, notes


def plan(orders, today, first_seen, supplier_filter=None, releases=None):
    """orders: [{vid, nick, company, due, lines: [match_line results]}]
    -> {"pos", "held", "attention" [(order, [texts])], "flagged" [(order, line, why)]}"""
    releases = releases or {}
    by_sup = {}
    flagged, attention = [], []
    for o in orders:
        rel = releases.get(str(o["vid"])) or {}
        asg, byhand, holds, notes = choose(o["lines"], release_shorts=bool(rel.get("shorts")))
        if o.get("promo"):
            byhand = []                        # promo items are ordered through the promo flow
        for l, why in byhand:
            flagged.append((o, l, why))
        if not holds and not rel.get("quote"):
            tot = {}
            for l, opt in asg.get("SanMar", []):
                pcs = sum(l["need"].values())
                t = tot.setdefault(l["style"], {"pcs": 0, "th": quote_threshold(l, opt["cost"] / pcs if pcs else 0)})
                t["pcs"] += pcs
            for st, t in tot.items():
                if t["th"] and t["pcs"] >= t["th"][0]:
                    holds.append(f"QUOTE · {t['pcs']} × {st} — at or over SanMar's {t['th'][0]}-piece quote level "
                                 f"({t['th'][1]}). Request a quote from SanMar first, then reply "
                                 f"`quote done #{o['vid']}`.")
        # A held / by-hand order shows on the post of the supplier it belongs to — SanMar unless only
        # S&S is involved (Luke, Nov 16: #7497 is a SanMar order, keep it off the S&S post).
        inv = set(asg) | {("SanMar" if "SanMar" in l["options"] else "S&S") for l in o["lines"]
                          if l and not l["flag"] and not ok(l, "SanMar") and not ok(l, "S&S")}
        home = "SanMar" if ("SanMar" in inv or not inv) else "S&S"       # one home post per held order
        show = (not supplier_filter) or supplier_filter == home
        if holds:
            if not show:
                continue
            if any(h.startswith("SHORT") for h in holds):
                holds.append(f"→ Fix the order in Printavo (next post picks it up), or reply "
                             f"`approve shorts #{o['vid']}` to order everything else.")
            attention.append((o, holds + [f"✋ also order by hand: {l['style']} {l['color']} {_sizes_txt(l['need'])} — {w}"
                                          for l, w in byhand]))
            continue
        if not asg:
            if byhand and show:
                attention.append((o, [f"✋ order by hand — {l['style']} {l['color']} {_sizes_txt(l['need'])}: {w}"
                                      for l, w in byhand]))
            continue
        first = True
        for s, lines in asg.items():
            if supplier_filter and s != supplier_filter:
                continue
            part = {"order": o, "lines": lines, "total": round(sum(x[1]["cost"] for x in lines), 2),
                    "notes": notes, "byhand": byhand if first else [],
                    "elsewhere": [f"{len(v)} line(s) on the {k} post" for k, v in asg.items() if k != s]}
            first = False
            by_sup.setdefault(s, []).append(part)
    pos, held = [], []
    for s, parts in by_sup.items():
        own = [p for p in parts if p["total"] >= FREE_FREIGHT]
        pool = [p for p in parts if p["total"] < FREE_FREIGHT]
        for p in own:
            pos.append({"supplier": s, "parts": [p], "why": "free freight"})
        if not pool:
            continue
        pool_total = sum(p["total"] for p in pool)
        if pool_total >= FREE_FREIGHT:
            pos.append({"supplier": s, "parts": pool, "why": f"{len(pool)} small orders pooled — free freight"})
            continue
        due = []
        for p in pool:
            age = bdays_between(first_seen.get(str(p["order"]["vid"]), today), today)
            # The clock runs from approval (first day in the queue), never from the production date (Luke, Nov 15).
            p["age"], p["reason"] = age, (f"waited {age} business days" if age >= MAX_WAIT_BDAYS else None)
            if p["reason"]:
                due.append(p)
        if not due:
            for p in pool:
                held.append((s, p, f"day {p['age']} of {MAX_WAIT_BDAYS}" + _due_warn(p["order"], today)))
            continue
        if own:
            host = max(own, key=lambda p: p["total"])
            for po in pos:
                if po["parts"][0] is host:
                    po["parts"] += due
                    po["why"] += " · " + ", ".join(f"#{p['order']['vid']}" for p in due) + " rides along"
            for p in pool:
                if p not in due:
                    held.append((s, p, f"day {p['age']} of {MAX_WAIT_BDAYS}" + _due_warn(p["order"], today)))
        else:
            pos.append({"supplier": s, "parts": pool,
                        "why": "under $200 — PAYING FREIGHT (" + ", ".join(
                            f"#{p['order']['vid']} {p['reason']}" for p in due) + ")"})
    # SanMar PO numbers max 28 chars -> at most 5 orders per PO (Nov 10).
    split = []
    for po in pos:
        parts, n = po["parts"], MAX_ORDERS_PER_PO
        while n > 1 and len("-".join(str(p["order"]["vid"]) for p in parts[:n])) > PO_NUM_MAX:
            n -= 1
        chunks = [parts[i:i + n] for i in range(0, len(parts), n)]
        for k, ch in enumerate(chunks, 1):
            split.append({**po, "parts": ch,
                          "why": po["why"] + (f" · part {k}/{len(chunks)}" if len(chunks) > 1 else "")})
    pos = split
    for po in pos:
        po["total"] = round(sum(p["total"] for p in po["parts"]), 2)
        po["po_number"] = po_number(po)
    return {"pos": pos, "held": held, "flagged": flagged, "attention": attention, "other": []}


MAX_ORDERS_PER_PO = 5
DUE_WARN_BDAYS = 5
PO_NUM_MAX = 28


def _due_warn(order, today):
    """Customer due date in jeopardy while the order waits for free freight -> flag only (Luke, Nov 15)."""
    d = order.get("due")
    if d and d <= add_business_days(today, DUE_WARN_BDAYS):
        return f" · ⚠ due {d:%a %-m/%-d}"
    return ""


def po_number(po):
    """Order #s only, dash-joined (Nov 10) — the goods notes / COGS key off these."""
    return "-".join(str(p["order"]["vid"]) for p in po["parts"])


def _sizes_txt(need):
    order = _st._ORDER
    return " ".join(f"{z}{need[z]}" for z in sorted(need, key=lambda z: (order.index(z) if z in order else 99, z)))


def _order_head(o):
    return (f"*#{o['vid']}* {o.get('company') or ''} — {o.get('nick') or ''}"
            + (f" · due {o['due']:%a %-m/%-d}" if o.get("due") else ""))


def _line_txt(l, opt):
    desc = f" {l['desc'][:40]}" if l.get("desc") else ""
    pcs = sum(l["need"].values())
    ship = f" · {opt['ship']}" if opt.get("ship") else ""
    return (f"   • *{l['style']}*{desc} · {opt['color']} — {_sizes_txt(l['need'])} = {pcs} pcs · ${opt['cost']:,.2f}{ship}"
            + (f"\n      {l['dup']}" if l.get("dup") else ""))


def _part_txt(p):
    o = p["order"]
    out = ["  " + _order_head(o)]
    if o.get("lane"):
        out.append(f"   ⚠ already in *{o['lane']}* with no goods PO — goods on hand? `skip #{o['vid']}` if so")
    out += [_line_txt(l, opt) for l, opt in p["lines"]]
    out += [f"   ↳ {n}" for n in p.get("notes") or []]
    out += [f"   ↳ {e}" for e in p.get("elsewhere") or []]
    out += [f"   ✋ order by hand: {l['style']} {l['color']} {_sizes_txt(l['need'])} — {w}" for l, w in p.get("byhand") or []]
    return out


def render(result, title):
    """One entry per order. READY TO ORDER / HOLDING / NOT READY (Luke, Nov 16)."""
    out = [title]
    if result["pos"]:
        out.append("\n🟢 *READY TO ORDER*")
        for po in result["pos"]:
            out.append(f" *PO \"{po['po_number']}\"* · ${po['total']:,.2f} · {po['why']}")
            for p in po["parts"]:
                out += _part_txt(p)
    if result["held"]:
        out.append("\n⏸ *HOLDING for free freight* (under $200 — `include #NNNN` to order today)")
        for s, p, why in result["held"]:
            out.append(f" ${p['total']:,.2f} · {why}")
            out += _part_txt(p)
    if result.get("attention"):
        out.append("\n🔴 *NOT READY — NEEDS ATTENTION* (nothing on these orders gets placed)")
        for o, texts in result["attention"]:
            out.append("  " + _order_head(o))
            out += [f"   {t}" for t in texts]
    if len(out) == 1:
        out.append("Nothing needs ordering.")
    return "\n".join(out)
