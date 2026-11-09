"""UPS Tracking API (OAuth client credentials). Used to tell when supplier boxes are DELIVERED.

Env: UPS_CLIENT_ID, UPS_CLIENT_SECRET. Without them enabled() is False and nothing calls UPS.
track(numbers) -> {tracking: {"delivered": bool, "when": "YYYY-MM-DD HH:MM" | None,
                              "expected": "YYYY-MM-DD" | None, "status": str} | None (unknown)}
Delivered results are cached forever; others for CACHE_MIN minutes.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime

import httpx

BASE = "https://onlinetools.ups.com"
CACHE_FILE = os.path.join(os.environ.get("DATA_DIR", "/data"), "ups_track_cache.json")
CACHE_MIN = 45
_lock = threading.Lock()
_tok = {"v": None, "exp": 0}


def enabled():
    return ups_enabled() or bool(os.environ.get("EASYPOST_API_KEY"))


def ups_enabled():
    return bool(os.environ.get("UPS_CLIENT_ID") and os.environ.get("UPS_CLIENT_SECRET"))


def _ep_carrier(c):
    c = (c or "").lower()
    for k, v in (("spee", "SpeeDee"), ("fedex", "FedEx"), ("usps", "USPS"), ("ontrac", "OnTrac"), ("ups", "UPS")):
        if k in c:
            return v
    return None


_EP_STATUS = {"delivered": True, "available_for_pickup": False, "in_transit": False, "out_for_delivery": False,
              "pre_transit": False, "return_to_sender": False, "failure": False}


def _easypost(n, carrier, ep_id=None):
    """Non-UPS tracking (SpeeDee, FedEx…) via EasyPost trackers. -> (result|None, tracker_id)"""
    key = os.environ.get("EASYPOST_API_KEY")
    if not key:
        return None, ep_id
    try:
        if ep_id:
            r = httpx.get(f"https://api.easypost.com/v2/trackers/{ep_id}", auth=(key, ""), timeout=30)
        else:
            t = {"tracking_code": n}
            if carrier:
                t["carrier"] = carrier
            r = httpx.post("https://api.easypost.com/v2/trackers", auth=(key, ""), timeout=30, json={"tracker": t})
        if r.status_code not in (200, 201):
            return None, ep_id
        j = r.json()
    except Exception:
        return None, ep_id
    st = str(j.get("status") or "")
    if st not in _EP_STATUS:
        return None, j.get("id") or ep_id
    det = j.get("tracking_details") or []
    when = None
    if st == "delivered" and det:
        dt = str(det[-1].get("datetime") or "")
        when = f"{dt[:10]} {dt[11:16]}" if len(dt) >= 16 else dt[:10] or None
    est = str(j.get("est_delivery_date") or "")[:10] or None
    return ({"delivered": st == "delivered", "when": when, "expected": est,
             "status": (j.get("carrier") or "") + " " + st.replace("_", " ")}, j.get("id") or ep_id)


def is_ups(n):
    return str(n or "").strip().upper().startswith("1Z")


def _token():
    if _tok["v"] and time.time() < _tok["exp"] - 120:
        return _tok["v"]
    r = httpx.post(f"{BASE}/security/v1/oauth/token", timeout=30,
                   auth=(os.environ["UPS_CLIENT_ID"], os.environ["UPS_CLIENT_SECRET"]),
                   data={"grant_type": "client_credentials"},
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    r.raise_for_status()
    j = r.json()
    _tok["v"], _tok["exp"] = j["access_token"], time.time() + int(j.get("expires_in", 3600))
    return _tok["v"]


def _ymd(s):
    s = str(s or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:8].isdigit() else None


def _parse(j):
    try:
        pkg = j["trackResponse"]["shipment"][0]["package"][0]
    except (KeyError, IndexError, TypeError):
        return None
    cur = pkg.get("currentStatus") or {}
    acts = pkg.get("activity") or []
    top = (acts[0].get("status") or {}) if acts else {}
    dates = {d.get("type"): _ymd(d.get("date")) for d in (pkg.get("deliveryDate") or [])}
    delivered = (str(cur.get("code")) == "011" or (top.get("type") or "").upper() == "D" or "DEL" in dates)
    when = None
    if delivered:
        a = acts[0] if acts else {}
        d, t = _ymd(a.get("date")) or dates.get("DEL"), str(a.get("time") or "")
        when = f"{d} {t[:2]}:{t[2:4]}" if d and len(t) >= 4 else d
    return {"delivered": delivered, "when": when,
            "expected": dates.get("RDD") or dates.get("SDD") or dates.get("EDD"),
            "status": cur.get("description") or top.get("description") or ""}


def _load():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(c):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(c, f)
    except Exception:
        pass


def track(numbers, carriers=None):
    """carriers: optional {tracking: carrier text from the supplier}."""
    out = {}
    carriers = {str(k).strip().upper(): v for k, v in (carriers or {}).items()}
    if not enabled():
        return {n: None for n in numbers}
    with _lock:
        cache = _load()
        now = time.time()
        for n in dict.fromkeys(str(x).strip().upper() for x in numbers if x):
            c = cache.get(n)
            if c and (c["r"] and c["r"]["delivered"] or now - c["t"] < CACHE_MIN * 60):
                out[n] = c["r"]
                continue
            if not is_ups(n) or not ups_enabled():
                res, ep_id = _easypost(n, _ep_carrier(carriers.get(n)) if not is_ups(n) else "UPS",
                                       (c or {}).get("ep"))
                out[n] = res
                if res is not None or ep_id:
                    cache[n] = {"t": now, "r": res, "ep": ep_id}
                continue
            try:
                r = httpx.get(f"{BASE}/api/track/v1/details/{n}", timeout=30,
                              params={"locale": "en_US", "returnSignature": "false"},
                              headers={"Authorization": f"Bearer {_token()}",
                                       "transId": uuid.uuid4().hex[:32], "transactionSrc": "estmerch"})
                res = _parse(r.json()) if r.status_code == 200 else None
            except Exception:
                res = None
            out[n] = res
            if res is not None:
                cache[n] = {"t": now, "r": res}
            time.sleep(0.2)
        _save(cache)
    return out


def summary(po_infos, results):
    """po_infos: [(po, info)] with info['tracking'], info['shipped'], info['partial'], info['canceled'].
    -> (line or None, all_in True/False/None)"""
    extra = sum(int(i.get("other_boxes") or 0) for _p, i in po_infos)
    tail = (f" · {extra} other box{'es' if extra != 1 else ''} on the shared PO "
            f"{'are' if extra != 1 else 'is'}n't this order's"
            if extra else "")
    pending_po = [po for po, i in po_infos
                  if not i.get("canceled") and (not i.get("shipped") or i.get("partial"))]
    trks = [t.strip().upper() for _, i in po_infos for t in (i.get("tracking") or []) if t]
    trks = list(dict.fromkeys(trks))
    if not trks and not pending_po:
        return None, None
    known = {t: results.get(t) for t in trks}
    got = [t for t, r in known.items() if r and r["delivered"]]
    out_ = [t for t, r in known.items() if r and not r["delivered"]]
    unk = [t for t, r in known.items() if r is None]
    if not pending_po and not out_ and not unk:
        last = max((known[t]["when"] or "" for t in got), default="")
        try:
            _dt = datetime.strptime(last, "%Y-%m-%d %H:%M")
            w = _dt.strftime("%a %m/%d ") + _dt.strftime("%-I:%M%p").lower()
        except ValueError:
            w = last
        return (f"✅ ALL GOODS DELIVERED ({len(got)} of {len(trks)} boxes)" + (f" · last {w}" if w else "")
                + tail), True
    parts = [f"{len(got)} of {len(trks)} boxes delivered"] if trks else []
    exp = [known[t]["expected"] for t in out_ if known[t]["expected"]]
    if exp:
        parts.append("last box due " + datetime.strptime(max(exp), "%Y-%m-%d").strftime("%a %m/%d"))
    if pending_po:
        parts.append("not shipped yet: " + ", ".join(f'"{p}"' for p in pending_po))
    if unk:
        parts.append(f"{len(unk)} box{'es' if len(unk) != 1 else ''} not trackable")
    all_in = False if (pending_po or out_) else None
    return "⏳ ALL GOODS: " + " · ".join(parts) + tail, all_in
