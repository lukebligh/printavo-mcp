"""Product photos for production files: style + color from a Printavo line item -> blank image URLs
(SanMar first, then S&S). Direct orders carry the style in the item # field; contract orders
usually only in the description, so candidates are pulled from both."""
import re
import time

from . import sanmar as _sanmar, ss as _ss, stock as _stock

SS_IMG = "https://www.ssactivewear.com/"
_GENERIC = {"SCRN", "SP", "EMB", "DTF", "PROMO", "SETUP", "SET", "ART", "DIGI", "DIGITIZING", "SHIP", "SHIPPING",
            "FREIGHT", "MISC", "TBD", "CUSTOM", "REPRINT", "SAMPLE", "FEE", "RUSH"}
_PREFIX = (("next level", "NL"), ("bella", "BC"), ("district", "DT"), ("carhartt", "CT"))
MISS_TTL_S = 7 * 86400
# Undecided blanks ("TBD T-Shirt") get a stand-in so the mockup has something (Luke, Nov 8)
_FALLBACK = ((re.compile(r"hood|hoody|hoodie"), "18500"),
             (re.compile(r"t-?\s?shirt|\btees?\b"), "5000"))
_BASIC = {"blue": "Royal", "red": "Red", "green": "Irish Green", "grey": "Sport Grey", "gray": "Sport Grey",
          "black": "Black", "white": "White", "navy": "Navy", "purple": "Purple", "orange": "Orange",
          "yellow": "Daisy", "pink": "Light Pink", "brown": "Dark Chocolate", "maroon": "Maroon",
          "tan": "Sand", "khaki": "Sand", "gold": "Gold", "teal": "Jade Dome", "charcoal": "Charcoal"}


class LookupError_(Exception):
    """Supplier call failed — don't cache, try again next run."""
_TOKEN = re.compile(r"\b([A-Z]{0,5}\d{2,5}[A-Z]{0,5}(?:CVC|LS|Y|T|B)?)\b")


def candidates(item_number, description):
    """Style candidates: the item # first (direct orders), then style-looking tokens in the
    description (contract orders). Generic codes (SCRN, EMB…) and size runs are skipped; a bare
    number gets the brand prefix SanMar uses when the description names the brand (6210 → NL6210)."""
    out = []
    it = (item_number or "").strip().upper()
    if it and re.fullmatch(r"[A-Z0-9]{2,14}", it) and it not in _GENERIC:
        out.append(it)
    for src in (item_number or "", description or ""):
        for t in _TOKEN.findall(src.upper().replace("-", "")):
            if (t in _GENERIC or re.fullmatch(r"(?:XS|S|M|L|XL|\dXL|OSFA|\dC)\d*", t)
                    or re.fullmatch(r"20\d\d|\d\d", t)):
                continue
            if t not in out:
                out.append(t)
    desc = (description or "").lower()
    for t in list(out):
        if t.isdigit():
            for brand, pre in _PREFIX:
                if brand in desc and pre + t not in out:
                    out.insert(out.index(t), pre + t)
    return out[:6]


def _match(colors, want):
    hit = _stock._pick_colors(colors, want)
    if hit:
        return hit
    w = _stock._cn(want)                                  # "Dark Heather Grey" → "Dark Heather"
    return sorted((c for c in colors if c and _stock._cn(c) and _stock._cn(c) in w),
                  key=lambda c: -len(_stock._cn(c)))[:1]


def _from_sanmar(style, color):
    try:
        rows = _sanmar.product_images(style, color) if color else []
        if not rows:
            rows = _sanmar.product_images(style)
            if color and rows:
                pick = _match([r["color"] for r in rows] + [r["catalog_color"] for r in rows], color)
                rows = [r for r in rows if r["color"] in pick or r["catalog_color"] in pick][:1]
            else:
                rows = rows[:1]
    except Exception as e:
        raise LookupError_(f"SanMar {style}: {e}")
    if not rows:
        return None
    r = rows[0]
    urls = [x for x in (r["front_flat"], r["back_flat"]) if x] or [x for x in (r["model"], r["product"]) if x]
    return {"supplier": "SanMar", "style": style, "color": r["color"], "urls": urls[:2]} if urls else None


def _ss_lookup(style, color, description):
    try:
        prods = _ss.products(style)
    except Exception as e:
        raise LookupError_(f"S&S {style}: {e}")
    if not prods:
        return None
    desc = (description or "").lower()
    brands = {p.get("brandName") for p in prods}
    if len(brands) > 1:
        hit = [p for p in prods if (p.get("brandName") or "").lower() in desc]
        prods = hit or prods
    colors = sorted({p.get("colorName") for p in prods if p.get("colorName")})
    pick = _match(colors, color) if color else colors[:1]
    if not pick:
        return None
    p = next(p for p in prods if p.get("colorName") == pick[0])
    urls = [SS_IMG + u.replace("_fm.", "_fl.") for u in (p.get("colorFrontImage"), p.get("colorBackImage")) if u]
    return {"supplier": "S&S", "style": style, "color": pick[0], "urls": urls} if urls else None


def fallback(description):
    d = (description or "").lower()
    for rx, style in _FALLBACK:
        if rx.search(d):
            return style
    return None


def lookup(item_number, description, color, cache):
    """-> {"supplier","style","color","urls"} or None. cache: dict persisted by the caller.
    Raises LookupError_ when a supplier call failed (nothing cached for that style).
    No style found but the line is clearly a tee / hoodie → Gildan 5000 / 18500 in the nearest color."""
    now = time.time()
    failed = None
    for style in candidates(item_number, description):
        key = f"{style}|{(color or '').lower()}"
        c = cache.get(key)
        if isinstance(c, dict) and "urls" in c:
            return c
        if isinstance(c, (int, float)) and now - c < MISS_TTL_S:
            continue
        try:
            res = _from_sanmar(style, color) or _ss_lookup(style, color, description)
        except LookupError_ as e:
            failed = e
            continue
        cache[key] = res if res else now
        if res:
            return res
        time.sleep(0.3)
    fb = fallback(description)
    if fb:
        col = (color or "White").strip()
        words = re.findall(r"[a-z]+", col.lower())
        basic = _BASIC.get(_stock._cn(col)) or (_BASIC.get(words[-1]) if words else None)
        order = (basic, col) if _stock._cn(col) in _BASIC else (col, basic)
        for try_color in order + ("White",):
            if not try_color:
                continue
            key = f"{fb}|{try_color.lower()}"
            c = cache.get(key)
            if isinstance(c, dict) and "urls" in c:
                return {**c, "stand_in": True}
            if isinstance(c, (int, float)) and now - c < MISS_TTL_S:
                continue
            try:
                res = _from_sanmar(fb, try_color)
            except LookupError_ as e:
                failed = e
                break
            cache[key] = res if res else now
            if res:
                return {**res, "stand_in": True}
    if failed:
        raise failed
    return None
