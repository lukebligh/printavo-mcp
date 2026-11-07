"""Blank check for Clint (Luke, Nov 6 2026): live stock + OUR price at SanMar and S&S
for one style/color, sized against what the job needs. Answers "can we get it, from
whom, and what do the blanks cost" before a quote goes out. Read-only."""
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from suppliers import sanmar as _sm, ss as _ss

_ORDER = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "6XL"]
_ALIAS = {"XXL": "2XL", "XXXL": "3XL", "XXXXL": "4XL", "XXXXXL": "5XL", "2X": "2XL", "3X": "3XL",
          "4X": "4XL", "SM": "S", "MD": "M", "LG": "L", "SMALL": "S", "MEDIUM": "M", "LARGE": "L"}


def norm_size(s):
    s = (s or "").strip().upper().replace(" ", "")
    return _ALIAS.get(s, s)


def parse_need(text):
    """'S10 M30 L40 XL20 2XL10' / 'S:10, M:30' / 'M L XL' -> OrderedDict size->qty|None."""
    text = re.sub(r"\s*[:=]\s*", ":", (text or "").strip())
    need = {}
    for tok in [t for t in re.split(r"[,\s;]+", text) if t]:
        m = re.match(r"^(.*[A-Za-z/])[:x\-]?(\d+)$", tok)
        if m:
            need[norm_size(m.group(1))] = need.get(norm_size(m.group(1)), 0) + int(m.group(2))
        else:
            need.setdefault(norm_size(tok), None)
    return need


def _skey(s):
    return (_ORDER.index(s) if s in _ORDER else 99, s)


_ABBR = {"ht": "heather", "htr": "heather", "hthr": "heather", "hth": "heather", "athl": "athletic",
         "dk": "dark", "lt": "light", "vtg": "vintage", "nvy": "navy", "grn": "green", "bl": "blue",
         "gry": "grey", "gray": "grey", "prpl": "purple", "blk": "black", "wht": "white", "org": "orange",
         "ylw": "yellow", "chrcl": "charcoal", "roy": "royal", "elec": "electric", "trbl": "triblend"}


def _cn(c):
    """Normalize a color: split SanMar camelCase ('HthrNavy'), expand abbreviations."""
    words = re.split(r"[^a-z0-9]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", c or "").lower())
    return "".join(_ABBR.get(w, w) for w in words if w)


def _sk(c):
    """Consonant skeleton — SanMar's stock feed abbreviates ('HunterGrn', 'Hthr Navy'):
    'Hunter Green' and 'HunterGrn' both -> 'hntrgrn'."""
    c = _cn(c)
    return (c[:1] + re.sub(r"[aeiouy]", "", c[1:])) if c else ""


def _pick_colors(colors, want):
    """Exact > same skeleton (abbreviation) > contains > skeleton contains."""
    if not want:
        return sorted(colors)
    w, ws = _cn(want), _sk(want)
    for test in (lambda c: _cn(c) == w, lambda c: _sk(c) == ws,
                 lambda c: w in _cn(c), lambda c: ws and ws in _sk(c)):
        hit = sorted(c for c in colors if test(c))
        if hit:
            return hit
    return []


# Closest to St. Louis first — that's what decides transit (1 day from Cincinnati / Lockport IL).
_NEAR = {"SanMar": ["Cincinnati", "Minneapolis", "Dallas", "Richmond", "Robbinsville", "Jacksonville"],
         "S&S": ["IL", "KS", "KY", "TX", "OH", "PA", "GA", "NV"]}


def _near(supplier, whs):
    """-> (name, qty) for the nearest warehouse holding stock."""
    have = {n: q for n, q in whs if q}
    for n in _NEAR[supplier]:
        for k, q in have.items():
            if k.lower().startswith(n.lower()):
                return k, q
    return max(have.items(), key=lambda x: x[1]) if have else None


_NEED = {}


def _best(picked, by):
    """Several colors matched -> the one that covers the most needed sizes, then most stock."""
    def score(c):
        rows = by.get(c, {})
        cover = sum(1 for z, q in _NEED.items() if z in rows and rows[z]["stock"] >= (q or 1))
        return (cover, len(rows), sum(r["stock"] for r in rows.values()))
    return sorted(picked, key=score, reverse=True)


def _money(x):
    return f"${x:,.2f}"


def sanmar_side(style, color):
    inv = _sm.inventory(style)
    if not inv:
        return {"supplier": "SanMar", "found": False}
    prices = {}
    try:
        for p in _sm.pricing(style):
            try:
                v = float(p["my_price"] or p["case"] or p["piece"] or 0)
            except ValueError:
                v = 0
            if v:
                prices[(_cn(p["color"]), norm_size(p["size"]))] = v
                prices.setdefault(("sk:" + _sk(p["color"]), norm_size(p["size"])), v)
    except Exception:
        pass
    colors = sorted({r["color"] for r in inv})
    picked = _pick_colors(colors, color)
    by = {}
    for r in inv:
        sz = norm_size(r["size"])
        if True:
            by.setdefault(r["color"], {})[sz] = {
                "stock": r["total"], "top": _near("SanMar", r["warehouses"]),
                "price": prices.get((_cn(r["color"]), sz)) or prices.get(("sk:" + _sk(r["color"]), sz))}
    picked = _best(picked, by)
    return {"supplier": "SanMar", "found": True, "colors": colors, "picked": picked, "by": by,
            "rows": by.get(picked[0], {}) if picked else {}}


# SanMar prefixes some outside brands; S&S lists them by the bare number.
_SS_BRAND = {"BC": "BELLA + CANVAS", "NL": "Next Level", "CAR": "Carhartt", "CT": "Carhartt"}


def ss_side(style, color, brand=""):
    items = _ss.products(f"{brand} {style}".strip())
    m = re.match(r"^([A-Za-z]+)(\d.*)$", style)
    if not items and m:
        b = brand or _SS_BRAND.get(m.group(1).upper(), "")
        items = _ss.products(f"{b} {m.group(2)}".strip())
    if not items:
        return {"supplier": "S&S", "found": False}
    colors = sorted({i.get("colorName") or "" for i in items})
    picked = _pick_colors(colors, color)
    by = {}
    for i in items:
        if True:
            rows = by.setdefault(i.get("colorName"), {})
            sz = norm_size(i.get("sizeName"))
            price = next((float(i[f]) for f in ("customerPrice", "salePrice", "casePrice", "piecePrice")
                          if isinstance(i.get(f), (int, float)) and i[f] > 0), None)
            whs = [(w.get("warehouseAbbr") or "?", int(w.get("qty") or 0)) for w in (i.get("warehouses") or [])]
            rows[sz] = {"stock": int(i.get("qty") or 0), "price": price, "top": _near("S&S", whs)}
    picked = _best(picked, by)
    rows = by.get(picked[0], {}) if picked else {}
    styles = sorted({f"{i.get('brandName', '')} {i.get('styleName', '')}".strip() for i in items})
    return {"supplier": "S&S", "found": True, "colors": colors, "picked": picked, "rows": rows,
            "styles": styles, "by": by}


# ─────────────────────────── Substitutes ───────────────────────────
# Color families: a "close color" swap only within the same family.
_FAMILIES = [
    ["forest", "hunter", "evergreen", "darkgreen", "bottle", "pine", "spruce"],
    ["kelly", "irishgreen", "clover", "sportgreen"],
    ["navy", "midnight", "darknavy", "deepnavy", "oxfordnavy"],
    ["royal", "cobalt", "sapphire", "trueroyal"],
    ["red", "truered", "fire", "scarlet", "cherry", "candyapple"],
    ["maroon", "cardinal", "burgundy", "garnet", "cranberry", "wine", "oxblood"],
    ["black", "jetblack", "deepblack", "vintageblack"],
    ["charcoal", "darkheather", "graphite", "darkgrey", "asphalt", "smoke"],
    ["ash", "sportgrey", "athleticheather", "heathergrey", "lightgrey", "oxford", "silver", "greyheather"],
    ["white", "brightwhite"],
    ["orange", "texasorange", "burntorange", "safetyorange"],
    ["gold", "yellow", "athleticgold", "goldenrod", "daisy"],
    ["purple", "violet", "teampurple", "deeppurple"],
    ["brown", "chocolate", "coffee", "darkchocolate", "espresso"],
    ["sand", "khaki", "tan", "natural", "oatmeal", "cream", "heatherdust", "dust"],
    ["olive", "military", "army", "militarygreen", "darkolive"],
    ["pink", "lightpink", "azalea", "charitypink"],
    ["carolina", "lightblue", "babyblue", "columbia", "skyblue"],
]


def _family(color):
    c = _cn(color)
    for fam in _FAMILIES:
        if any(k in c for k in fam):
            return fam
    return []


def _cover(rows):
    """-> (ok, total$, short_sizes) for the current need against one color's size rows."""
    total, short = 0.0, []
    for z, q in _NEED.items():
        r = rows.get(z)
        if not r or r["stock"] < (q or 1):
            short.append(z)
            continue
        if q and r["price"]:
            total += q * r["price"]
    return (not short, total, short)


def _swaps():
    try:
        with open(os.path.join(os.path.dirname(__file__), "swaps.json")) as f:
            return json.load(f).get("groups", [])
    except Exception:
        return []


def _base(style):
    return style.strip().upper()


def _equivalents(style):
    """[(label, style, brand)] from the Est Merch swap list, then SanMar's related products."""
    out, seen = [], {_base(style)}
    for g in _swaps():
        if any(_base(x["style"]) == _base(style) for x in g["styles"]):
            for x in g["styles"]:
                if _base(x["style"]) not in seen:
                    seen.add(_base(x["style"]))
                    out.append((f"swap list: {g['name']}", x["style"], x.get("brand", "")))
    try:
        for t, pid in _sm.related(style):
            if _base(pid) not in seen and "substitut" in t.lower():
                seen.add(_base(pid))
                out.append(("SanMar: substitutable", pid, ""))
        for t, pid in _sm.related(style):
            if _base(pid) not in seen and "companion" not in t.lower():
                seen.add(_base(pid))
                out.append((f"SanMar: {t.lower()}", pid, ""))
    except Exception:
        pass
    return out


def _alt_colors(side, color, limit=3):
    """Same style, close colors (same family) that cover the run, cheapest first."""
    fam = _family(color)
    if not fam or not side.get("found") or not side.get("by"):
        return []
    cur = side["picked"][0] if side.get("picked") else None
    res = []
    for c, rows in side["by"].items():
        if c == cur or not any(k in _cn(c) for k in fam):
            continue
        ok, total, _ = _cover(rows)
        if ok:
            res.append((total, c))
    return sorted(res)[:limit]


def substitutes(style, color, sides):
    lines = []
    for sd in sides:
        for total, c in _alt_colors(sd, color):
            lines.append((total, f"  • {style} {c} @ {sd['supplier']} — covers it · {_money(total)}  (close color)"))
    eq = _equivalents(style)[:5]

    def look(e):
        label, st, br = e
        found = []
        for fn in (lambda: sanmar_side(st, color), lambda: ss_side(st, color, br)):
            try:
                sd = fn()
            except Exception:
                continue
            if not sd.get("found") or len(sd.get("styles", [])) > 1 or not sd.get("picked"):
                continue
            ok, total, short = _cover(sd["rows"])
            if ok:
                found.append((total, f"  • {(br + ' ') if br else ''}{st} {sd['picked'][0]} @ {sd['supplier']}"
                                     f" — covers it · {_money(total)}  ({label})"))
        return found

    if eq and color:
        with ThreadPoolExecutor(max_workers=5) as ex:
            for f in ex.map(look, eq):
                lines += f
    lines.sort(key=lambda x: x[0])
    if not lines:
        tried = ", ".join(e[1] for e in eq) or "none on the swap list"
        return ["\nSUBSTITUTES: nothing found that covers the full run "
                f"(close colors checked; styles tried: {tried})."]
    return ["\nSUBSTITUTES (cheapest first):"] + [l for _t, l in lines[:8]]


_LOCK = threading.Lock()   # _NEED is module state: one blank check at a time


def check(style, color="", sizes="", brand="", subs=False):
    with _LOCK:
        return _check(style, color, sizes, brand, subs)


def _check(style, color="", sizes="", brand="", subs=False):
    need = parse_need(sizes)
    _NEED.clear()
    _NEED.update(need)
    sides = []
    for fn in (lambda: sanmar_side(style, color), lambda: ss_side(style, color, brand)):
        try:
            sides.append(fn())
        except Exception as e:
            sides.append({"supplier": "?", "found": False, "error": str(e)[:200]})
    out = [f"BLANK CHECK · {style}" + (f" · {color}" if color else "")
           + (f" · need {' '.join(f'{k}{v}' if v else k for k, v in need.items())}" if need else "")]
    verdicts = []
    for s in sides:
        name = s["supplier"]
        if s.get("error"):
            out.append(f"\n{name}: lookup failed — {s['error']}")
            continue
        if not s["found"]:
            out.append(f"\n{name}: doesn't carry {style}" + (" (try brand=, e.g. brand=\"Gildan\")" if name == "S&S" else ""))
            continue
        if len(s.get("styles", [])) > 1:
            out.append(f"\n{name}: '{style}' matches several styles — {', '.join(s['styles'][:6])}. Add brand=.")
            continue
        if not color or not s["picked"]:
            if not color:
                out.append(f"\n{name}: {len(s['colors'])} colors: " + ", ".join(s["colors"][:60])
                           + (" …" if len(s["colors"]) > 60 else ""))
                continue
            words = [x for x in re.split(r"\W+", color.lower()) if len(x) > 2]
            near = [c for c in s["colors"] if any(w in _cn(c) or (len(w) >= 5 and _sk(w) in _sk(c)) for w in words)]
            out.append(f"\n{name}: no '{color}' in {style}. "
                       + (f"Closest: {', '.join(near[:15])}." if near else f"{len(s['colors'])} colors — leave color blank to list them.")
                       + (f" Heathers are often in the CVC version ({style}CVC)." if "heather" in color.lower()
                          and not style.upper().endswith("CVC") else ""))
            continue
        clr = s["picked"][0]
        hdr = f"\n{name} — {clr}"
        if len(s["picked"]) > 1:
            hdr += f"  (also matched: {', '.join(s['picked'][1:5])})"
        out.append(hdr)
        sizes_shown = [z for z in need] if need else sorted(s["rows"], key=_skey)
        total, short, missing = 0.0, [], []
        for z in sizes_shown:
            r = s["rows"].get(z)
            q = need.get(z)
            if not r:
                out.append(f"  {z}: not offered in this color")
                missing.append(z)
                continue
            p = f"{_money(r['price'])} ea" if r["price"] else "price n/a"
            top = (f" · nearest {r['top'][0]} {r['top'][1]:,}{'+' if r['top'][1] == {'SanMar': 3000, 'S&S': 500}[s['supplier']] else ''}"
                   if r["top"] else "")
            flag = ""
            if q:
                if r["stock"] >= q:
                    flag = f"  ✅ need {q}"
                else:
                    flag = f"  ⚠ SHORT — need {q}, have {r['stock']:,}"
                    short.append(z)
                if r["price"]:
                    total += q * r["price"]
            out.append(f"  {z}: {r['stock']:,} in stock · {p}{top}{flag}")
        if need and any(need.values()):
            ok = not short and not missing
            out.append(f"  → {'covers it' if ok else 'can NOT fill: ' + ', '.join(short + missing)}"
                       f" · blanks {_money(total)}" + (" (priced sizes only)" if total and not ok else ""))
            verdicts.append((ok, total, name))
    good = sorted([v for v in verdicts if v[0] and v[1]], key=lambda v: v[1])
    if good:
        best = good[0]
        line = f"\nBEST: {best[2]} — {_money(best[1])} for the blanks"
        if len(good) > 1:
            line += f" ({_money(good[1][1] - best[1])} less than {good[1][2]})"
        out.append(line)
    elif verdicts:
        out.append("\n⚠ Neither supplier can fill the full size run in this color.")
    no_color = color and not any(sd.get("picked") for sd in sides if sd.get("found"))
    if need and any(need.values()) and color and (subs or not good or no_color):
        out += substitutes(style, color, [sd for sd in sides if sd.get("found")])
    out.append("Stock is live right now, not reserved. Our price = Est Merch account price, before freight (free over $200 per supplier order).")
    return "\n".join(out)
