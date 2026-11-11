"""SanMar Web Services client (SOAP). Read-only: status, shipments, invoices,
inventory, pricing, packing slips. Docs: SanMar Web Services Integration Guide v24.7.

Credentials come from env (Secret Manager): SANMAR_USERNAME, SANMAR_PASSWORD,
SANMAR_CUSTOMER_NUMBER (default 231594). Est Merch POs start with the Printavo
order number, e.g. "7480 MOCann-GOTR-WD", so order lookups match on that prefix.
"""
import os
import re
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

BASE = os.environ.get("SANMAR_BASE_URL", "https://ws.sanmar.com:8080")
PS = "http://www.promostandards.org/WSDL"
WEB_NS = 'xmlns:web="http://webservice.integration.sanmar.com/"'
IMPL_NS = 'xmlns:impl="http://impl.webservice.integration.sanmar.com/"'
WAREHOUSES = {"1": "Seattle WA", "2": "Cincinnati OH", "3": "Dallas TX", "4": "Reno NV",
              "5": "Robbinsville NJ", "6": "Jacksonville FL", "7": "Minneapolis MN",
              "12": "Phoenix AZ", "31": "Richmond VA"}


class SanMarError(RuntimeError):
    pass


def _cust():
    return os.environ.get("SANMAR_CUSTOMER_NUMBER", "231594")


def _creds():
    u = os.environ.get("SANMAR_USERNAME", "").strip()
    p = os.environ.get("SANMAR_PASSWORD", "").strip()
    if not u or not p:
        raise SanMarError("SanMar credentials not configured (SANMAR_USERNAME / SANMAR_PASSWORD).")
    return escape(u), escape(p)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _all(el, name):
    return [e for e in el.iter() if _local(e.tag) == name]


def _first(el, name):
    for e in el.iter():
        if _local(e.tag) == name:
            return e
    return None


def _t(el, name, default=""):
    e = _first(el, name)
    return (e.text or "").strip() if e is not None and e.text else default


def _kids(el, name):
    """Direct children with local name."""
    return [c for c in list(el) if _local(c.tag) == name]


def _post(path, body, ns_decl, timeout=60, base=None):
    env = ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
           f'{ns_decl}><soapenv:Header/><soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>')
    last = None
    for attempt in range(2):
        try:
            r = httpx.post((base or BASE) + path, content=env.encode("utf-8"), timeout=timeout,
                           headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""})
            break
        except httpx.HTTPError as e:
            last = e
            time.sleep(2)
    else:
        raise SanMarError(f"SanMar unreachable: {last}")
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        raise SanMarError(f"SanMar returned non-XML (HTTP {r.status_code}): {r.text[:300]}")
    fault = _first(root, "faultstring")
    if fault is not None:
        msg = (fault.text or "").strip()
        if "not found" in msg.lower():
            return None
        raise SanMarError(f"SanMar fault: {msg}")
    return root


def _ps(svc, ver):
    return (f'xmlns:ns="{PS}/{svc}/{ver}/" xmlns:shar="{PS}/{svc}/{ver}/SharedObjects/"')


# ─────────────────────────── Order status (PromoStandards V2) ───────────────────────────

def parse_order_status(root):
    out = []
    for os_ in _all(root, "OrderStatus"):
        po = _t(os_, "purchaseOrderNumber")
        for d in _all(os_, "OrderStatusDetail"):
            lines = []
            for p in _all(d, "Product"):
                qo, qs = _first(p, "QuantityOrdered"), _first(p, "QuantityShipped")
                lines.append({"style": _t(p, "productId"), "part_id": _t(p, "partId"),
                              "ordered": _t(qo, "value") if qo is not None else "",
                              "shipped": _t(qs, "value") if qs is not None else "",
                              "status": _t(p, "status")})
            issues = sorted({_t(i, "issueCategory") for i in _all(d, "Issue") if _t(i, "issueCategory")})
            out.append({"po": po, "sales_order": _t(d, "salesOrderNumber"),
                        "status": _t(d, "status"), "issue": _t(d, "issueCategory"),
                        "issues": issues, "as_of": _t(d, "validTimestamp"), "lines": lines})
    return out


def order_status(query_type="poSearch", reference="", since=None, product_detail=True):
    u, p = _creds()
    ref = f"<shar:referenceNumber>{escape(reference)}</shar:referenceNumber>" if reference else ""
    ts = f"<shar:statusTimeStamp>{since}</shar:statusTimeStamp>" if since else ""
    body = (f"<ns:GetOrderStatusRequest><shar:wsVersion>2.0.0</shar:wsVersion>"
            f"<shar:id>{u}</shar:id><shar:password>{p}</shar:password>"
            f"<shar:queryType>{query_type}</shar:queryType>{ref}{ts}"
            f"<shar:returnIssueDetailType>openIssues</shar:returnIssueDetailType>"
            f"<shar:returnProductDetail>{'true' if product_detail else 'false'}</shar:returnProductDetail>"
            f"</ns:GetOrderStatusRequest>")
    root = _post("/promostandards/OrderStatusServiceBindingV2", body, _ps("OrderStatus", "2.0.0"))
    return parse_order_status(root) if root is not None else []


# ─────────────────────── Shipment notification (PromoStandards V1) ───────────────────────

def parse_shipments(root):
    out = []
    for n in _all(root, "OrderShipmentNotification"):
        po = _t(n, "purchaseOrderNumber")
        for so in _all(n, "SalesOrder"):
            for loc in _all(so, "ShipmentLocation"):
                frm = _first(loc, "ShipFromAddress")
                origin = f"{_t(frm, 'city')} {_t(frm, 'region')}".strip() if frm is not None else ""
                for pk in _all(loc, "Package"):
                    items = [{"style": _t(i, "supplierProductId"), "part_id": _t(i, "supplierPartId"),
                              "qty": _t(i, "quantity")} for i in _all(pk, "Item")]
                    out.append({"po": po, "sales_order": _t(so, "salesOrderNumber"),
                                "complete": _t(so, "complete"), "from": origin,
                                "box": _t(pk, "id"), "tracking": _t(pk, "trackingNumber"),
                                "ship_date": _t(pk, "shipmentDate")[:10],
                                "carrier": f"{_t(pk, 'carrier')} {_t(pk, 'shipmentMethod')}".strip(),
                                "items": items})
    return out


def shipments(po="", since=None):
    """By exact PO (queryType 1) or by ship date (queryType 3, max 7 days back)."""
    u, p = _creds()
    if po:
        q = f"<ns:queryType>1</ns:queryType><ns:referenceNumber>{escape(po)}</ns:referenceNumber>"
    else:
        q = f"<ns:queryType>3</ns:queryType><ns:shipmentDateTimeStamp>{since}</ns:shipmentDateTimeStamp>"
    body = (f"<ns:GetOrderShipmentNotificationRequest><shar:wsVersion>1.0.0</shar:wsVersion>"
            f"<shar:id>{u}</shar:id><shar:password>{p}</shar:password>{q}"
            f"</ns:GetOrderShipmentNotificationRequest>")
    root = _post("/promostandards/OrderShipmentNotificationServiceBinding", body,
                 _ps("OrderShipmentNotificationService", "1.0.0"))
    return parse_shipments(root) if root is not None else []


# ─────────────────────────── Invoices (SanMar standard) ───────────────────────────

def parse_invoices(root):
    out = []
    for inv in _all(root, "Invoice") + _all(root, "InvoiceHeader"):
        h = _first(inv, "Header") if _local(inv.tag) == "Invoice" else inv
        if h is None:
            continue
        lines = [{"style": _t(li, "StyleNo"), "color": _t(li, "StyleColor"), "size": _t(li, "StyleSize"),
                  "qty": _t(li, "Quantity"), "unit": _t(li, "UnitPrice"), "amount": _t(li, "Amount"),
                  "part_id": _t(li, "UniqueKey") or _t(li, "uniqueKey")}
                 for li in _kids(inv, "LineItem")]
        out.append({"invoice": _t(h, "InvoiceNo"), "date": _t(h, "InvoiceDate"),
                    "status": _t(h, "InvoiceStatus"), "po": _t(h, "PurchaseOrderNo"),
                    "order_date": _t(h, "OrderDate"), "fob": _t(h, "FOB"),
                    "subtotal": _t(h, "SubTotal"), "freight": _t(h, "ShippingHandlingCharges"),
                    "tax": _t(h, "SalesTax"), "total": _t(h, "TotalAmount"),
                    "tracking": _t(h, "TrackingIDs"), "lines": lines})
    return out


def _inv_call(method, extra=""):
    u, p = _creds()
    body = (f"<web:{method}><web:CustomerNo>{_cust()}</web:CustomerNo>"
            f"<web:UserName>{u}</web:UserName><web:Password>{p}</web:Password>{extra}</web:{method}>")
    root = _post("/SanMarWebService/InvoicePort", body, WEB_NS, timeout=120)
    return parse_invoices(root) if root is not None else []


def invoices_by_po(po):
    return _inv_call("GetInvoicesByPurchaseOrderNo",
                     f"<web:PurchaseOrderNo>{escape(po)}</web:PurchaseOrderNo>")


def invoice_headers(start, end):
    """Header-only invoices in a date range (YYYY-MM-DD, max 1 year)."""
    return _inv_call("GetInvoicesHeaderByInvoiceDateRange",
                     f"<web:StartDate>{start}</web:StartDate><web:EndDate>{end}</web:EndDate>")


def invoices_by_date(start, end):
    """Full invoices (with lines) in a date range (max 3 months)."""
    return _inv_call("GetInvoicesByInvoiceDateRange",
                     f"<web:StartDate>{start}</web:StartDate><web:EndDate>{end}</web:EndDate>")


# ─────────────────────────── Inventory (PromoStandards V2) ───────────────────────────

def inventory(style, color="", size=""):
    u, p = _creds()
    flt = ""
    if color or size:
        flt = "<shar:Filter>"
        if size:
            flt += f"<shar:LabelSizeArray><shar:labelSize>{escape(size)}</shar:labelSize></shar:LabelSizeArray>"
        if color:
            flt += f"<shar:PartColorArray><shar:partColor>{escape(color)}</shar:partColor></shar:PartColorArray>"
        flt += "</shar:Filter>"
    body = (f"<ns:GetInventoryLevelsRequest><shar:wsVersion>2.0.0</shar:wsVersion>"
            f"<shar:id>{u}</shar:id><shar:password>{p}</shar:password>"
            f"<shar:productId>{escape(style)}</shar:productId>{flt}</ns:GetInventoryLevelsRequest>")
    root = _post("/promostandards/InventoryServiceBindingV2final", body, _ps("Inventory", "2.0.0"))
    if root is None:
        return []
    out = []
    for pi in _all(root, "PartInventory"):
        qa = _first(pi, "quantityAvailable")
        whs = []
        for loc in _all(pi, "InventoryLocation"):
            whs.append((_t(loc, "inventoryLocationName"), int(float(_t(loc, "value", "0") or 0))))
        out.append({"part_id": _t(pi, "partId"), "color": _t(pi, "partColor"), "size": _t(pi, "labelSize"),
                    "total": int(float(_t(qa, "value", "0") or 0)) if qa is not None else 0,
                    "warehouses": whs})
    return out


# ─────────────────────────── Pricing (SanMar standard) ───────────────────────────

def pricing(style, color="", size=""):
    u, p = _creds()
    body = (f"<impl:getPricing><arg0><style>{escape(style)}</style><color>{escape(color)}</color>"
            f"<size>{escape(size)}</size></arg0><arg1><sanMarCustomerNumber>{_cust()}</sanMarCustomerNumber>"
            f"<sanMarUserName>{u}</sanMarUserName><sanMarUserPassword>{p}</sanMarUserPassword></arg1>"
            f"</impl:getPricing>")
    root = _post("/SanMarWebService/SanMarPricingServicePort", body, IMPL_NS)
    if root is None:
        return []
    if _t(root, "errorOccurred").lower() == "true" or _t(root, "errorOccured").lower() == "true":
        raise SanMarError(_t(root, "message") or "pricing error")
    return [{"style": _t(i, "style"), "color": _t(i, "color"), "size": _t(i, "size"),
             "piece": _t(i, "piecePrice"), "case": _t(i, "casePrice"), "sale": _t(i, "salePrice"),
             "my_price": _t(i, "myPrice"), "sale_end": _t(i, "saleEndDate"),
             "part_id": _t(i, "inventoryKey") + _t(i, "sizeIndex")}
            for i in _all(root, "listResponse")]


# ─────────────────────────── Packing slip / LPN ───────────────────────────

def packing_slip(lpn):
    u, p = _creds()
    body = (f"<pac:GetPackingSlip><pac:wsVersion>1.0.0</pac:wsVersion><pac:UserId>{u}</pac:UserId>"
            f"<pac:Password>{p}</pac:Password><pac:PackingSlipId>{escape(lpn)}</pac:PackingSlipId>"
            f"</pac:GetPackingSlip>")
    root = _post("/SanMarWebService/webservices/PackingSlipService", body,
                 'xmlns:pac="http://ws.sanmar.com/webservices/PackingSlip"')
    if root is None:
        return None
    return {"po": _t(root, "PurchaseOrderReference"), "box": _t(root, "ShipmentUnitIndex"),
            "boxes": _t(root, "ShipmentUnitQuantity"), "ship_date": _t(root, "ShipmentDate"),
            "tracking": _t(root, "TrackingId"),
            "items": [{"style": _t(i, "StyleNo"), "color": _t(i, "Color"), "size": _t(i, "Size"),
                       "qty": _t(i, "Quantity"), "part_id": _t(i, "SkuId")} for i in _all(root, "Item")]}


# ─────────────────────── Find POs for a Printavo order number ───────────────────────

_PO_CACHE = {"at": 0.0, "pos": set()}


def _known_pos():
    """All PO strings SanMar knows about recently: open orders, updated in last 30 days,
    invoiced in last ~6 months. Cached 15 minutes."""
    if time.time() - _PO_CACHE["at"] < 900 and _PO_CACHE["pos"]:
        return _PO_CACHE["pos"]
    pos = set()
    now = datetime.now(timezone.utc)
    errs = []
    for fn in (lambda: order_status("allOpen", product_detail=False),
               lambda: order_status("lastUpdate", since=(now - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00"),
                                    product_detail=False)):
        try:
            pos.update(o["po"] for o in fn() if o["po"])
        except SanMarError as e:
            errs.append(str(e))
    try:
        hdrs = invoice_headers((now - timedelta(days=180)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
        pos.update(h["po"] for h in hdrs if h["po"])
    except SanMarError as e:
        errs.append(str(e))
    if not pos and errs:
        raise SanMarError("; ".join(errs))
    _PO_CACHE.update(at=time.time(), pos=pos)
    return pos


def find_pos(ref):
    """'7480' -> every SanMar PO starting with 7480 ('7480 MOCann-GOTR-WD', '7480-2'...).
    Anything that isn't a bare number is treated as an exact PO."""
    ref = (ref or "").strip()
    if not ref:
        return []
    if not ref.isdigit():
        return [ref]
    # Anywhere in the PO, not touching other digits: combined POs read "7458 PiFi-7427 GOTR-Merch"
    pat = re.compile(rf"(?<!\d){re.escape(ref)}(?!\d)")
    seen = {}
    for po in sorted(_known_pos()):
        if pat.search(po):
            seen.setdefault(po.strip().upper(), po.strip())   # SanMar returns the same PO in two casings
    return sorted(seen.values())


# ─────────────────────────── Human-readable order report ───────────────────────────

def _money(x):
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return x or "-"


def order_report(ref):
    """Everything SanMar knows about the goods for a Printavo order # (or exact PO):
    status, backorders/holds, shipments with tracking + box contents, invoice cost."""
    pos = find_pos(ref)
    if not pos:
        return (f"No SanMar PO found starting with '{ref}' (checked open orders, last 30 days of "
                f"updates, and 6 months of invoices). Either goods aren't ordered from SanMar yet, "
                f"or the PO wasn't written with the Printavo order number first.")
    out = []
    for po in pos:
        out.append(f"■ PO \"{po}\"")
        invs, ships, stats = [], [], []
        try:
            invs = invoices_by_po(po)
        except SanMarError as e:
            out.append(f"  invoice lookup failed: {e}")
        try:
            ships = shipments(po=po)
        except SanMarError as e:
            out.append(f"  shipment lookup failed: {e}")
        try:
            stats = order_status("poSearch", po)
        except SanMarError as e:
            out.append(f"  status lookup failed: {e}")
        names = {}
        for inv in invs:
            for li in inv["lines"]:
                names[li["part_id"]] = f"{li['style']} {li['color']} {li['size']}"
        for s in stats:
            flag = f"  ⚠ {', '.join(s['issues']) or s['issue']}" if (s["issues"] or s["issue"]) else ""
            out.append(f"  Status: {s['status'] or '?'} (SO {s['sales_order']}){flag}")
            short = [l for l in s["lines"] if l["ordered"] and l["shipped"] and
                     float(l["shipped"] or 0) < float(l["ordered"] or 0)]
            for l in short:
                nm = names.get(l["part_id"], f"{l['style']} part {l['part_id']}")
                out.append(f"    not yet shipped: {nm} — {l['shipped']}/{l['ordered']} ({l['status']})")
        by_track = {}
        for b in ships:
            by_track.setdefault((b["tracking"], b["ship_date"], b["carrier"], b["from"]), []).append(b)
        for (trk, dt, car, frm), boxes in by_track.items():
            pcs = sum(int(float(i["qty"] or 0)) for b in boxes for i in b["items"])
            out.append(f"  Shipped {dt} from {frm or '?'} via {car}: {trk} — {pcs} pcs")
            for b in boxes:
                desc = ", ".join(f"{names.get(i['part_id'], i['style'] + ' #' + i['part_id'])} ×{i['qty']}"
                                 for i in b["items"])
                out.append(f"    box {b['box']}: {desc}")
        if not ships and stats:
            out.append("  No shipment yet.")
        for inv in invs:
            out.append(f"  Invoice {inv['invoice']} ({inv['date']}, {inv['status']}): goods "
                       f"{_money(inv['subtotal'])} + freight {_money(inv['freight'])} + tax {_money(inv['tax'])}"
                       f" = {_money(inv['total'])}")
            for li in inv["lines"]:
                out.append(f"    {li['style']} {li['color']} {li['size']} ×{li['qty']} @ {_money(li['unit'])}")
        if not invs and not stats and not ships:
            out.append("  SanMar returned nothing for this PO.")
    return "\n".join(out)


def health():
    r = pricing("PC61", "White", "M")
    if not r:
        return "SanMar reachable but returned no pricing for PC61 White M."
    x = r[0]
    return f"SanMar OK — PC61 White M: your price {_money(x['my_price'])} (case {_money(x['case'])})."


def normalized_invoices(start, end):
    """Invoices with lines in a date range, in the shared supplier shape used by cogs.py."""
    def f(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0
    out = []
    for inv in invoices_by_date(start, end):
        lines = [{"style": li["style"], "brand": "", "color": li["color"], "size": li["size"],
                  "qty": f(li["qty"]), "unit": f(li["unit"]), "amount": f(li["amount"])}
                 for li in inv["lines"]]
        out.append({"vendor": "SanMar", "invoice": inv["invoice"], "date": inv["date"],
                    "po": inv["po"], "goods": f(inv["subtotal"]), "freight": f(inv["freight"]),
                    "tax": f(inv["tax"]), "total": f(inv["total"]), "lines": lines})
    return out


# ─────────────────────────── Related products (PromoStandards Product Data V2) ───────────────────────────

_REL_CACHE = {}


def related(style):
    """SanMar's own related styles for a product: [(relationType, productId)], e.g.
    Substitutable / Companion Sell / Common Grouping. Cached 24h per style."""
    key = style.strip().upper()
    hit = _REL_CACHE.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    u, p = _creds()
    body = (f"<ns:GetProductRequest><shar:wsVersion>2.0.0</shar:wsVersion><shar:id>{u}</shar:id>"
            f"<shar:password>{p}</shar:password><shar:localizationCountry>us</shar:localizationCountry>"
            f"<shar:localizationLanguage>en</shar:localizationLanguage>"
            f"<shar:productId>{escape(style)}</shar:productId></ns:GetProductRequest>")
    root, last = None, None
    for path in ("/promostandards/ProductDataServiceBindingV2", "/promostandards/ProductDataServiceV2.xml"):
        try:
            root = _post(path, body, _ps("ProductDataService", "2.0.0"), timeout=90)
            break
        except SanMarError as e:      # guide lists both URLs; use whichever answers
            last = e
    else:
        raise last
    out = []
    if root is not None:
        seen = set()
        for rp in _all(root, "RelatedProduct"):
            t, pid = _t(rp, "relationType"), _t(rp, "productId")
            if pid and pid.upper() != key and (t, pid.upper()) not in seen:
                seen.add((t, pid.upper()))
                out.append((t, pid))
    _REL_CACHE[key] = (time.time(), out)
    return out


# ─────────────────────────── Product images (SanMar product info) ───────────────────────────

def product_images(style, color=""):
    """-> list of {"color", "front_flat", "back_flat", "model", "product"} — one per color returned.
    getProductInfoByStyleColorSize; color may be blank (all colors)."""
    u, p = _creds()
    body = (f"<impl:getProductInfoByStyleColorSize><arg0><style>{escape(style)}</style>"
            f"<color>{escape(color)}</color><size></size></arg0><arg1><sanMarCustomerNumber>{_cust()}"
            f"</sanMarCustomerNumber><sanMarUserName>{u}</sanMarUserName><sanMarUserPassword>{p}"
            f"</sanMarUserPassword></arg1></impl:getProductInfoByStyleColorSize>")
    root = _post("/SanMarWebService/SanMarProductInfoServicePort", body, IMPL_NS)
    if root is None:
        return []
    out = {}
    for r in _all(root, "listResponse"):
        c = _t(r, "color") or _t(r, "catalogColor")
        if c and c not in out:
            out[c] = {"color": c, "catalog_color": _t(r, "catalogColor"), "front_flat": _t(r, "frontFlat"),
                      "back_flat": _t(r, "backFlat"), "model": _t(r, "colorProductImage"),
                      "product": _t(r, "productImage"), "brand": _t(r, "brandName")}
    return list(out.values())



# ─────────────────────────── Purchase orders (SanMar standard PO service) ───────────────────────────
# EDEV = SanMar's test environment (orders are never filled). Production is a separate switch.
PO_BASES = {"edev": "https://edev-ws.sanmar.com:8080", "prod": "https://ws.sanmar.com:8080"}
SHIP_TO = {"shipTo": "ESTABLISHED MERCH", "attention": "ESTABLISHED MERCH",
           "shipAddress1": "1920 S VANDEVENTER AVE", "shipAddress2": "", "shipCity": "ST LOUIS",
           "shipState": "MO", "shipZip": "63110", "shipMethod": "UPS", "residence": "N"}
PO_NUM_MAX = 28


def _po_creds(env):
    if env == "edev":
        u = os.environ.get("SANMAR_EDEV_USERNAME", "").strip()
        p = os.environ.get("SANMAR_EDEV_PASSWORD", "").strip()
        if not u or not p:
            raise SanMarError("SanMar EDEV credentials not configured (SANMAR_EDEV_USERNAME / _PASSWORD).")
        return escape(u), escape(p)
    return _creds()


def part_keys(style, color):
    """-> {(color, size): (inventoryKey, sizeIndex)} from pricing — the IDs SanMar recommends on POs."""
    u, p = _creds()
    body = (f"<impl:getPricing><arg0><style>{escape(style)}</style><color>{escape(color)}</color>"
            f"<size></size></arg0><arg1><sanMarCustomerNumber>{_cust()}</sanMarCustomerNumber>"
            f"<sanMarUserName>{u}</sanMarUserName><sanMarUserPassword>{p}</sanMarUserPassword></arg1>"
            f"</impl:getPricing>")
    root = _post("/SanMarWebService/SanMarPricingServicePort", body, IMPL_NS)
    out = {}
    for i in (_all(root, "listResponse") if root is not None else []):
        if _t(i, "inventoryKey") and _t(i, "sizeIndex"):
            out[(_t(i, "color"), _t(i, "size").upper())] = (_t(i, "inventoryKey"), _t(i, "sizeIndex"))
    return out


def _po_body(method, po_num, lines, email, u, p):
    if not po_num or len(po_num) > PO_NUM_MAX or "," in po_num:
        raise SanMarError(f"PO number must be 1–{PO_NUM_MAX} characters, no commas: {po_num!r}")
    head = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in SHIP_TO.items())
    det = "".join(
        "<webServicePoDetailList>"
        f"<inventoryKey>{escape(str(l.get('inventoryKey') or ''))}</inventoryKey>"
        f"<sizeIndex>{escape(str(l.get('sizeIndex') or ''))}</sizeIndex>"
        f"<style>{escape(l['style'])}</style><color>{escape(l.get('color') or '')}</color>"
        f"<size>{escape(l.get('size') or '')}</size><quantity>{int(l['qty'])}</quantity><whseNo/>"
        "</webServicePoDetailList>" for l in lines)
    return (f"<web:{method}><arg0>{head}<notes/><poNum>{escape(po_num)}</poNum>"
            f"<shipEmail>{escape(email or '')}</shipEmail><department/>{det}</arg0>"
            f"<arg1><sanMarCustomerNumber>{_cust()}</sanMarCustomerNumber><sanMarUserName>{u}</sanMarUserName>"
            f"<sanMarUserPassword>{p}</sanMarUserPassword></arg1></web:{method}>")


def po_call(method, po_num, lines, env="edev", email=""):
    """method: 'getPreSubmitInfo' (stock check, places nothing) or 'submitPO'.
    lines: [{style, color, size, qty, inventoryKey?, sizeIndex?}]"""
    if method not in ("getPreSubmitInfo", "submitPO"):
        raise SanMarError("bad PO method")
    if env not in PO_BASES:
        raise SanMarError("env must be edev or prod")
    u, p = _po_creds(env)
    root = _post("/SanMarWebService/SanMarPOServicePort", _po_body(method, po_num, lines, email, u, p),
                 WEB_NS, timeout=90, base=PO_BASES[env])
    if root is None:
        raise SanMarError("SanMar PO service returned nothing")
    err = (_t(root, "errorOccurred") or _t(root, "errorOccured")).lower() == "true"
    det = [{"style": _t(d, "style"), "color": _t(d, "color"), "size": _t(d, "size"),
            "qty": _t(d, "quantity"), "whse": _t(d, "whseNo"), "message": _t(d, "message")}
           for d in _all(root, "webServicePoDetailList")]
    return {"ok": not err, "message": _t(root, "message"), "lines": det}
