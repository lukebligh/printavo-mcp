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
        pos.update(str(o.get("poNumber") or "") for o in (r.json() or []) if o.get("poNumber"))
    return pos
