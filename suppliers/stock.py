"""Blank check for Clint (Luke, Nov 6 2026): live stock + OUR price at SanMar and S&S
for one style/color, sized against what the job needs. Answers "can we get it, from
whom, and what do the blanks cost" before a quote goes out. Read-only."""
import re

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


def _cn(c):
    return re.sub(r"[^a-z0-9]", "", (c or "").lower())


def _pick_colors(colors, want):
    """Exact (normalized) match wins; else every color containing the text."""
    if not want:
        return sorted(colors)
    w = _cn(want)
    exact = [c for c in colors if _cn(c) == w]
    return exact or sorted(c for c in colors if w in _cn(c))


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
    except Exception:
        pass
    colors = sorted({r["color"] for r in inv})
    picked = _pick_colors(colors, color)
    rows = {}
    for r in inv:
        if r["color"] in picked[:1]:
            sz = norm_size(r["size"])
            whs = sorted(r["warehouses"], key=lambda w: -w[1])
            rows[sz] = {"stock": r["total"], "price": prices.get((_cn(r["color"]), sz)),
                        "top": whs[0] if whs and whs[0][1] else None}
    return {"supplier": "SanMar", "found": True, "colors": colors, "picked": picked, "rows": rows}


def ss_side(style, color, brand=""):
    items = _ss.products(f"{brand} {style}".strip())
    if not items:
        return {"supplier": "S&S", "found": False}
    colors = sorted({i.get("colorName") or "" for i in items})
    picked = _pick_colors(colors, color)
    rows = {}
    for i in items:
        if i.get("colorName") in picked[:1]:
            sz = norm_size(i.get("sizeName"))
            price = next((float(i[f]) for f in ("customerPrice", "salePrice", "casePrice", "piecePrice")
                          if isinstance(i.get(f), (int, float)) and i[f] > 0), None)
            whs = sorted(((w.get("warehouseAbbr") or "?", int(w.get("qty") or 0))
                          for w in (i.get("warehouses") or [])), key=lambda w: -w[1])
            rows[sz] = {"stock": int(i.get("qty") or 0), "price": price,
                        "top": whs[0] if whs and whs[0][1] else None}
    styles = sorted({f"{i.get('brandName', '')} {i.get('styleName', '')}".strip() for i in items})
    return {"supplier": "S&S", "found": True, "colors": colors, "picked": picked, "rows": rows,
            "styles": styles}


def check(style, color="", sizes="", brand=""):
    need = parse_need(sizes)
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
            why = "no color given" if not color else f"no color matches '{color}'"
            out.append(f"\n{name}: {why} — {len(s['colors'])} colors: " + ", ".join(s["colors"][:40])
                       + (" …" if len(s["colors"]) > 40 else ""))
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
            top = f" · most at {r['top'][0]} ({r['top'][1]:,})" if r["top"] else ""
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
        out.append("\n⚠ Neither supplier can fill the full size run in this color — split it or pick another color.")
    out.append("Stock is live right now, not reserved. Our price = Est Merch account price (before freight).")
    return "\n".join(out)
