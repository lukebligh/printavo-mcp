"""SanMar Spec Sheet + Measurements → PDF (Nov 2026).

style number → SanMar product page (findProducts.json, needs a browsing session + CSRF token or
SanMar's firewall rejects it) → /p/<code>/specSheetMeasurements (plain server-rendered HTML) →
a one-to-two page PDF built here with fpdf2. SanMar's own "Create a PDF" button only works for a
signed-in sanmar.com user, so we build our own from the same page content.
"""
import io
import re
import time
import unicodedata

import requests
from bs4 import BeautifulSoup
from fpdf import FPDF
from PIL import Image

BASE = "https://www.sanmar.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
_sess = {"s": None, "csrf": "", "at": 0.0}
SESSION_TTL_S = 20 * 60


class SpecError(Exception):
    pass


def _session(fresh: bool = False):
    if not fresh and _sess["s"] and time.time() - _sess["at"] < SESSION_TTL_S:
        return _sess["s"], _sess["csrf"]
    s = requests.Session()
    s.headers["User-Agent"] = UA
    h = s.get(BASE + "/", timeout=30).text
    m = re.search(r'name="_csrf" content="([^"]+)"', h)
    _sess.update(s=s, csrf=m.group(1) if m else "", at=time.time())
    return s, _sess["csrf"]


def find_product(style: str):
    """Exact style → (product_code, style, name). Raises SpecError when SanMar has no exact match."""
    style = (style or "").strip().upper()
    if not style:
        raise SpecError("no style")
    for attempt in (0, 1):
        s, tok = _session(fresh=bool(attempt))
        r = s.post(BASE + "/search/findProducts.json", json={"text": style}, timeout=30,
                   headers={"CSRFToken": tok, "Referer": BASE + "/", "Accept": "application/json",
                            "X-Requested-With": "XMLHttpRequest"})
        if r.status_code == 200 and r.text.lstrip().startswith("{"):
            break
    else:
        raise SpecError(f"SanMar search refused (HTTP {r.status_code})")
    j = r.json()
    for res in j.get("results") or []:
        if (res.get("styleNumber") or "").strip().upper() == style:
            code = (res.get("code") or "").split("_")[0]
            if code.isdigit():
                return code, res["styleNumber"], _clean(BeautifulSoup(res.get("name") or "", "html.parser").get_text())
    raise SpecError(f"{style} is not a SanMar style")


def _clean(t: str) -> str:
    """fpdf core fonts are latin-1: straighten quotes/dashes, drop anything else unprintable."""
    t = (t or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace("•", "-").replace("™", "(TM)")
    t = unicodedata.normalize("NFKC", t)
    return re.sub(r"\s+", " ", t.encode("latin-1", "ignore").decode("latin-1")).strip()


def _abs(u: str) -> str:
    return ("https:" + u) if u.startswith("//") else (BASE + u if u.startswith("/") else u)


def fetch_spec(code: str) -> dict:
    s, _ = _session()
    r = s.get(f"{BASE}/p/{code}/specSheetMeasurements", timeout=30)
    if r.status_code != 200:
        raise SpecError(f"spec page HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.select_one(".product-name-title")
    if not title:
        raise SpecError("spec page has no product title")
    spec = {"title": _clean(title.get_text()), "url": f"{BASE}/p/{code}/specSheetMeasurements"}
    photo = soup.select_one(".product-picture img")
    spec["photo"] = _abs(photo["src"]) if photo and photo.get("src") else ""
    spec["sketches"] = []
    for opt in soup.select(".product-sketches-option"):
        img, cap = opt.find("img"), opt.find("p")
        if img and img.get("src"):
            spec["sketches"].append((_abs(img["src"]), _clean(cap.get_text()) if cap else ""))
    desc = soup.select_one(".product-description .inner-content")
    spec["intro"], spec["bullets"], spec["care"] = [], [], ""
    if desc:
        for p in desc.find_all("p"):
            if _clean(p.get_text()):
                spec["intro"].append(_clean(p.get_text()))
        spec["bullets"] = [_clean(li.get_text()) for li in desc.find_all("li") if _clean(li.get_text())]
        txt = desc.get_text("\n")
        m = re.search(r"CARE INSTRUCTIONS\s*(.+)", txt, re.S | re.I)
        if m:
            spec["care"] = _clean(m.group(1))
    spec["tables"] = []
    for tbl in soup.select("table.spec-table"):
        head = [_clean(th.get_text()) for th in tbl.select("thead th")]
        rows = []
        for tr in tbl.select("tbody tr"):
            rows.append([_clean(c.get_text()) for c in tr.find_all(["th", "td"])])
        if rows:
            spec["tables"].append((head, rows))
    spec["notes"] = [_clean(li.get_text()) for li in soup.select(".measurements-description-list li")]
    return spec


def _image(url: str, box: int):
    if not url:
        return None
    try:
        s, _ = _session()
        r = s.get(url, timeout=30)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content))
        im = im.convert("RGB")
        im.thumbnail((box, box))
        return im
    except Exception:
        return None


def build_pdf(spec: dict, style: str) -> bytes:
    pdf = FPDF(format="letter", unit="pt")
    pdf.set_auto_page_break(True, margin=40)
    pdf.set_margins(40, 40, 40)
    pdf.set_image_filter("DCTDecode")   # JPEG inside the PDF — keeps files ~100 KB
    pdf.add_page()
    W = pdf.w - 80

    pdf.set_fill_color(16, 92, 168)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(W, 24, "  SPEC SHEET + MEASUREMENTS", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(12)
    top = pdf.get_y()

    # Left column: product photo, then sketches
    photo = _image(spec.get("photo"), 700)
    col = 220
    y = top
    if photo:
        h = col * photo.height / photo.width
        pdf.image(photo, x=40, y=y, w=col, h=h)
        y += h + 10
    sk = [(_image(u, 400), cap) for u, cap in spec.get("sketches", [])[:2]]
    sk = [(im, cap) for im, cap in sk if im]
    if sk:
        sw = (col - 10) / 2
        sh = max(sw * im.height / im.width for im, _ in sk)
        for i, (im, cap) in enumerate(sk):
            x = 40 + i * (sw + 10)
            pdf.image(im, x=x, y=y, w=sw, h=sw * im.height / im.width)
            pdf.set_text_color(90, 90, 90)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_xy(x, y + sh + 2)
            pdf.cell(sw, 10, cap, align="C")
        y += sh + 16
    left_bottom = y

    # Right column: title, description, bullets, care
    rx, rw = 40 + col + 20, W - col - 20
    pdf.set_xy(rx, top)
    pdf.set_text_color(20, 20, 20)
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(rw, 19, spec["title"], new_x="LEFT", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 9.5)
    for para in spec.get("intro", []):
        pdf.set_x(rx)
        pdf.multi_cell(rw, 12.5, para, new_x="LEFT", new_y="NEXT")
        pdf.ln(4)
    for b in spec.get("bullets", []):
        pdf.set_x(rx)
        pdf.multi_cell(rw, 12.5, "-  " + b, new_x="LEFT", new_y="NEXT")
    if spec.get("care"):
        pdf.ln(6)
        pdf.set_x(rx)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(rw, 13, "CARE INSTRUCTIONS", new_x="LEFT", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_x(rx)
        pdf.multi_cell(rw, 12.5, spec["care"], new_x="LEFT", new_y="NEXT")
    pdf.set_y(max(left_bottom, pdf.get_y()) + 14)

    # Measurements
    for ti, (head, rows) in enumerate(spec.get("tables", [])):
        if pdf.get_y() > pdf.h - 160:
            pdf.add_page()
        pdf.set_text_color(20, 20, 20)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(W, 18, "Product Measurements" if ti == 0 else "Size Chart", new_x="LMARGIN", new_y="NEXT")
        ncols = max(len(head), max(len(r) for r in rows))
        first = min(150, W * 0.35)
        other = (W - first) / max(1, ncols - 1)
        widths = [first] + [other] * (ncols - 1)
        pdf.set_fill_color(40, 40, 40)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 9)
        hdr = (head + [""] * ncols)[:ncols]
        for w, t in zip(widths, hdr):
            pdf.cell(w, 16, t, border=0, fill=True, align="C")
        pdf.ln(16)
        pdf.set_text_color(20, 20, 20)
        for i, r in enumerate(rows):
            pdf.set_fill_color(242, 242, 242) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)
            r = (r + [""] * ncols)[:ncols]
            for j, (w, t) in enumerate(zip(widths, r)):
                pdf.set_font("Helvetica", "B" if j == 0 else "", 9)
                pdf.cell(w, 15, t, fill=True, align="L" if j == 0 else "C")
            pdf.ln(15)
        pdf.ln(8)
    if spec.get("notes"):
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(60, 60, 60)
        for n in spec["notes"]:
            pdf.multi_cell(W, 11, "-  " + n, new_x="LMARGIN", new_y="NEXT")

    pdf.set_auto_page_break(False)
    pdf.set_y(pdf.h - 30)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(130, 130, 130)
    pdf.cell(W, 10, f"Source: SanMar {style} - {spec['url']}", align="L")
    return bytes(pdf.output())


def spec_pdf(style: str):
    """style → (filename, pdf_bytes, title). Raises SpecError."""
    code, st, _name = find_product(style)
    spec = fetch_spec(code)
    # Spec page titles end with the style (". CP90") — anything else means the lookup landed wrong.
    if not spec["title"].upper().rstrip(". ").endswith(st.upper()):
        raise SpecError(f"{st}: spec page is for '{spec['title']}'")
    return f"{st}_SanMar_Spec_Sheet.pdf", build_pdf(spec, st), spec["title"]
