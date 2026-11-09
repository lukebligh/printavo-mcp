"""PO vs order check (Luke, Nov 8): does what was ordered from SanMar / S&S match what the
Printavo order(s) need — style, color, size, quantity? Run while the order is still pre-production,
so a wrong size or short count is caught the day it's ordered, not at count-in.

compare(needs, have) -> list of discrepancy strings ([] = matches)
  needs: [(style, color, size, qty)] from Printavo line items (every order on the PO)
  have:  [(style, color, size, qty)] from the supplier PO(s)
"""
import json
import os
import re
import time

from . import sanmar as _sanmar, stock as _stock

PARTS_FILE = os.path.join("/data", "sanmar_parts.json")
PARTS_TTL_S = 7 * 86400
_SIZE = {"XXL": "2XL", "XXXL": "3XL", "XXXXL": "4XL", "XXXXXL": "5XL", "SMALL": "S", "MEDIUM": "M",
         "LARGE": "L", "XLARGE": "XL", "XLG": "XL", "LG": "L", "MD": "M", "SM": "S", "ADJUSTABLE": "OSFA",
         "ONE SIZE": "OSFA", "OS": "OSFA", "ONESIZE": "OSFA"}
_GENERIC = {"SCRN", "SP", "EMB", "DTF", "PROMO", "SETUP", "SET", "ART", "SHIP", "SHIPPING", "FREIGHT",
            "MISC", "TBD", "CUSTOM", "REPRINT", "SAMPLE", "FEE", "RUSH", "DIGITIZING"}


def norm_size(s):
    """Printavo keys look like 'size_2xl' / 'size_yl' / 'size_other'; suppliers say '2XL' / 'L' / 'OSFA'.
    Youth sizes compare as the plain size — the style itself is the youth style."""
    s = re.sub(r"[\s\-]", "", str(s or "").upper())
    s = re.sub(r"^SIZE_?", "", s)
    s = {"OTHER": "OSFA"}.get(s, s)
    m = re.fullmatch(r"Y(XS|S|M|L|XL)", s)
    if m:
        s = m.group(1)
    return _SIZE.get(s, s)


def core(style):
    """BC3001CVC ~ 3001CVC, NL6210 ~ 6210 (S&S lists outside brands by bare number)."""
    s = re.sub(r"[^A-Z0-9]", "", str(style or "").upper())
    return re.sub(r"^(BC|NL|DT|CT|CAR)(?=\d)", "", s)


def real_style(style):
    s = re.sub(r"[^A-Z0-9]", "", str(style or "").upper())
    return bool(s) and s not in _GENERIC and bool(re.search(r"\d", s))


# ───────────── SanMar part id → (color, size), via pricing (one call per style, cached) ─────────────

def _load():
    try:
        with open(PARTS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    try:
        os.makedirs(os.path.dirname(PARTS_FILE), exist_ok=True)
        with open(PARTS_FILE, "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def sanmar_parts(style, cache):
    key = style.strip().upper()
    c = cache.get(key)
    if c and time.time() - c["t"] < PARTS_TTL_S:
        return c["m"]
    m = {r["part_id"]: (r["color"], r["size"]) for r in _sanmar.pricing(key) if r.get("part_id")}
    cache[key] = {"t": time.time(), "m": m}
    return m


def sanmar_have(status, ships, invs, cache):
    """What a SanMar PO holds: ordered qty per part from order status, else shipped qty per part."""
    names = {}
    for inv in invs or []:
        for li in inv["lines"]:
            names[li["part_id"]] = (li["style"], li["color"], li["size"])
    per = {}
    if status:
        for s in status:
            for l in s.get("lines") or []:
                if l.get("part_id"):
                    k = (l.get("style") or "", l["part_id"])
                    per[k] = per.get(k, 0) + float(l.get("ordered") or 0)
    else:
        for b in ships or []:
            for i in b.get("items") or []:
                if i.get("part_id"):
                    k = (i.get("style") or "", i["part_id"])
                    per[k] = per.get(k, 0) + float(i.get("qty") or 0)
    out = []
    for (style, pid), q in per.items():
        if q <= 0:
            continue
        if pid in names:
            st, co, sz = names[pid]
        else:
            m = sanmar_parts(style, cache) if style else {}
            co, sz = m.get(pid, ("", ""))
            st = style
        out.append((st, co, sz, q))
    return out


def ss_have(o):
    return [(str(li.get("styleName") or ""), str(li.get("colorName") or ""), str(li.get("sizeName") or ""),
             float(li.get("qtyOrdered") or 0)) for li in (o.get("lines") or []) if float(li.get("qtyOrdered") or 0)]


# ─────────────────────────────── compare ───────────────────────────────

def _q(x):
    return int(x) if float(x).is_integer() else x


def compare(needs, have, own_styles=None):
    """own_styles: core styles of THIS order — only these can be reported 'not on any PO'
    (other orders on a combined PO may have their blanks on a different PO)."""
    have_by_style = {}
    for st, co, sz, q in have:
        have_by_style.setdefault(core(st), []).append((st, co, norm_size(sz), q))
    need_by = {}
    issues, unknown = [], False
    for st, co, sz, q in needs:
        if not real_style(st) or not q:
            continue
        cs = core(st)
        if cs not in have_by_style:
            if own_styles is None or cs in own_styles:
                issues.append(f"{st} {co}: on the order, not on any SanMar/S&S PO")
            continue
        colors = sorted({c for _s, c, _z, _q2 in have_by_style[cs] if c})
        if not colors:
            unknown = True
            continue
        pick = _stock._pick_colors(colors, co) if co else colors
        if not pick:
            issues.append(f"{st} {co}: color not on the PO (PO has {', '.join(colors[:4])})")
            continue
        k = (cs, pick[0], norm_size(sz))
        need_by[k] = need_by.get(k, 0) + float(q)
    have_by = {}
    for cs, rows in have_by_style.items():
        for st, co, sz, q in rows:
            if co:
                k = (cs, co, sz)
                have_by[k] = have_by.get(k, 0) + q
    styles_needed = {k[0] for k in need_by}
    for k in sorted(set(need_by) | set(have_by)):
        if k[0] not in styles_needed:
            continue                                   # another order's style on a shared PO
        n, h = need_by.get(k, 0), have_by.get(k, 0)
        if n == h:
            continue
        label = f"{k[0]} {k[1]} {k[2] or '—'}"
        if h == 0:
            issues.append(f"{label}: order needs {_q(n)}, PO has none")
        elif n == 0:
            issues.append(f"{label}: PO has {_q(h)}, order doesn't need it")
        elif h < n:
            issues.append(f"{label}: order needs {_q(n)}, PO has {_q(h)} (short {_q(n - h)})")
        else:
            issues.append(f"{label}: order needs {_q(n)}, PO has {_q(h)} ({_q(h - n)} over)")
    total = sum(need_by.values())
    return list(dict.fromkeys(issues)), total, unknown
