"""S&S Activewear REST API v2 (read-only): invoiced orders with lines.
Auth: basic (SSACTIVEWEAR_ACCOUNT, SSACTIVEWEAR_APIKEY) — both already on the server."""
import os
import time

import httpx

BASE = "https://api.ssactivewear.com/v2"


class SSError(RuntimeError):
    pass


def _auth():
    a = os.environ.get("SSACTIVEWEAR_ACCOUNT", "").strip()
    k = os.environ.get("SSACTIVEWEAR_APIKEY", "").strip()
    if not a or not k:
        raise SSError("S&S credentials not configured (SSACTIVEWEAR_ACCOUNT / SSACTIVEWEAR_APIKEY).")
    return a, k


def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def invoices_by_date(start, end):
    """Invoiced orders between start and end (YYYY-MM-DD), normalized to the same shape
    as SanMar invoices: vendor, invoice, date, po, goods, freight, tax, total, lines."""
    last = None
    for attempt in range(3):
        try:
            r = httpx.get(f"{BASE}/orders/", auth=_auth(), timeout=90,
                          params={"invoicestartdate": start, "invoiceenddate": end,
                                  "lines": "true", "mediatype": "json"})
            break
        except httpx.HTTPError as e:
            last = e
            time.sleep(3)
    else:
        raise SSError(f"S&S unreachable: {last}")
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise SSError(f"S&S HTTP {r.status_code}: {r.text[:200]}")
    out = []
    for o in r.json() or []:
        lines = []
        for li in o.get("lines") or []:
            q = _f(li.get("qtyShipped")) or _f(li.get("qtyOrdered"))
            if not q:
                continue
            lines.append({"style": str(li.get("styleName") or ""), "brand": str(li.get("brandName") or ""),
                          "color": str(li.get("colorName") or ""), "size": str(li.get("sizeName") or ""),
                          "qty": q, "unit": _f(li.get("price")), "amount": round(q * _f(li.get("price")), 2)})
        fees = sum(_f(o.get(k)) for k in ("smallOrderFee", "setUpFee", "restockFee", "lostCashDiscount"))
        out.append({"vendor": "S&S", "invoice": str(o.get("invoiceNumber") or ""),
                    "date": str(o.get("invoiceDate") or "")[:10], "po": str(o.get("poNumber") or ""),
                    "goods": _f(o.get("subtotal")), "freight": _f(o.get("shipping")) + fees,
                    "tax": _f(o.get("tax")), "total": _f(o.get("total")), "lines": lines})
    return out


def recent_pos():
    """PO strings for open orders plus everything placed in the last ~3 months."""
    pos = set()
    for params in ({"mediatype": "json"}, {"All": "True", "mediatype": "json"}):
        try:
            r = httpx.get(f"{BASE}/orders/", auth=_auth(), timeout=90, params=params)
        except httpx.HTTPError as e:
            raise SSError(f"S&S unreachable: {e}")
        if r.status_code == 404:
            continue
        if r.status_code != 200:
            raise SSError(f"S&S HTTP {r.status_code}: {r.text[:200]}")
        pos.update(str(o.get("poNumber") or "") for o in (r.json() or []) if o.get("poNumber") and _real(o))
    return pos


def _real(o):
    """Cancelled orders and auto-ordering TEST orders (testOrder=true creates then cancels an order)
    must never count as 'goods ordered' anywhere (goods notes, ordering queue, COGS)."""
    st = str(o.get("orderStatus") or "").lower()
    po = str(o.get("poNumber") or "").strip().upper()
    return "cancel" not in st and not po.startswith("TEST")


SHIP_TO = {"customer": "ESTABLISHED MERCH", "attn": "", "address": "1920 S VANDEVENTER AVE",
           "city": "ST LOUIS", "state": "MO", "zip": "63110", "residential": False}


def place_order(po_number, lines, test=True, warehouses="", email=""):
    """POST /v2/orders. lines: [{"sku": ..., "qty": n}]. test=True -> S&S validates, creates and
    cancels it (nothing ships). warehouses: 'IL,KS,KY' to restrict auto-select (next-day).
    -> {"ok", "message", "orders": [{orderNumber, warehouse, total}]}"""
    body = {"shippingAddress": SHIP_TO, "shippingMethod": "1", "poNumber": po_number,
            "emailConfirmation": email, "testOrder": bool(test), "autoselectWarehouse": True,
            "AutoSelectWarehouse_Preference": "fewest", "autoselectWarehouse_Warehouses": warehouses,
            "rejectLineErrors": True, "rejectLineErrors_Email": False,
            "lines": [{"identifier": str(l["sku"]), "qty": int(l["qty"])} for l in lines]}
    try:
        r = httpx.post(f"{BASE}/orders/", auth=_auth(), json=body, timeout=120)
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"S&S unreachable: {e}", "orders": []}
    if r.status_code not in (200, 201):
        return {"ok": False, "message": f"S&S HTTP {r.status_code}: {r.text[:300]}", "orders": []}
    data = r.json() or []
    if isinstance(data, dict):
        data = data.get("orders") or data.get("Orders") or [data]
    orders = [{"orderNumber": str(o.get("orderNumber") or ""), "warehouse": str(o.get("warehouseAbbr") or ""),
               "total": _f(o.get("total")), "status": str(o.get("orderStatus") or "")} for o in data]
    return {"ok": bool(orders), "message": "accepted" if orders else f"no order returned: {r.text[:200]}",
            "orders": orders}


def orders_recent():
    """Raw S&S orders (open + last ~3 months) with lines, for goods-status notes."""
    try:
        r = httpx.get(f"{BASE}/orders/", auth=_auth(), timeout=120,
                      params={"All": "True", "lines": "true", "mediatype": "json"})
    except httpx.HTTPError as e:
        raise SSError(f"S&S unreachable: {e}")
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise SSError(f"S&S HTTP {r.status_code}: {r.text[:200]}")
    return [o for o in (r.json() or []) if _real(o)]


def _get(path, params):
    try:
        r = httpx.get(f"{BASE}{path}", auth=_auth(), timeout=60, params={**params, "mediatype": "json"})
    except httpx.HTTPError as e:
        raise SSError(f"S&S unreachable: {e}")
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise SSError(f"S&S HTTP {r.status_code}: {r.text[:200]}")
    return r.json() or []


def products(style):
    """Every SKU (color x size, stock by warehouse, our price) for a style. style may be
    'Gildan 5000', a part number, or a bare style name ('5000' / 'BC3001'); a bare name
    that exists under several brands returns all of them (caller asks for a brand)."""
    items = _get("/products/", {"style": style})
    if items:
        return items
    want = style.strip().lower()
    hits = [s for s in _get("/styles/", {"search": style})
            if str(s.get("styleName", "")).lower() == want
            or f"{s.get('brandName', '')} {s.get('styleName', '')}".lower() == want
            or str(s.get("partNumber", "")).lower() == want]
    out = []
    for s in hits[:4]:
        out += _get("/products/", {"styleid": s.get("styleID")})
    return out
