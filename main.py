from fastmcp import FastMCP
import httpx
import os
import re
import math
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

mcp = FastMCP("Printavo Assistant")

EMAIL            = os.environ.get("PRINTAVO_EMAIL", "")
TOKEN            = os.environ.get("PRINTAVO_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")      # production briefing → #all-est-merch
CX_SLACK_WEBHOOK_URL = os.environ.get("CX_SLACK_WEBHOOK_URL", "") # CX digest → #cx-daily ONLY
# Luke's private backlog block (old pickups + old/flipped quotes) goes here —
# NOT the #cx-daily channel. Point this at a webhook for a channel only Luke
# sees (or his DM). If unset, the backlog only appears in the tool's return
# text (visible when Luke runs run_cx_digest manually) and never in #cx-daily.
CX_BACKLOG_SLACK_WEBHOOK_URL = os.environ.get("CX_BACKLOG_SLACK_WEBHOOK_URL", "")

# ── ART QUEUE DIGEST → Richie ──────────────────────────────────────────────────
# Weekday 8 AM DM to Richie: every order in the five art-queue statuses, with an
# hours-in-status clock and a 24h SLA flag. Delivery is a true DM via a Slack
# bot token (chat.postMessage to the user), with an optional channel webhook
# fallback. NEITHER is needed for a dry run — dry runs only build/return text.
# HARD GATE: the scheduler never posts unless ART_DIGEST_ENABLED is true, so the
# code can be deployed and dry-run-previewed without messaging anyone.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")            # xoxb-… with chat:write
ART_DM_USER_ID  = os.environ.get("ART_DM_USER_ID", "U080NBG912L,U0504FSDYB0") # Richie T., Luke (comma-separated)
ART_SLACK_WEBHOOK_URL = os.environ.get("ART_SLACK_WEBHOOK_URL", "")# optional channel fallback
ART_DIGEST_ENABLED = os.environ.get("ART_DIGEST_ENABLED", "").strip().lower() in (
    "1", "true", "yes")
ART_SLA_HOURS = float(os.environ.get("ART_SLA_HOURS", "24"))
ART_SNAPSHOT_PATH = os.environ.get("ART_SNAPSHOT_PATH", "")

API_URL          = "https://www.printavo.com/api/v2"

def _dry_run() -> bool:
    """DRY_RUN=true → digest logs/returns text instead of posting to Slack."""
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


# ── STATUS NAMES — single source of truth ─────────────────────────────────────
# Every Printavo status name this codebase references lives HERE and nowhere
# else. A rename in Printavo = a one-line edit in this dict. All matching is
# CASE-INSENSITIVE (see _norm_status / _status_matches).
STATUS_NAMES = {
    # CX digest Z1 — wellness calls
    "QUOTE_APPROVED":           "QUOTE APPROVED",
    # CX digest Z2 — lingering pickups
    "READY_FOR_PICKUP":         "READY FOR PICK UP",
    # CX digest Z3 — quote follow-ups
    "QUOTE_APPROVAL_SENT":      "QUOTE APPROVAL SENT",
    # CX digest Z5 — art follow-ups (art / proof / mock-up / digitizing / sew-out)
    "ART_APPROVAL_SENT":        "ART APPROVAL SENT",
    "MOCKUP_REQUESTED":         "MOCK-UP REQUESTED",
    "PROOF_REQUESTED":          "PROOF REQUESTED",
    "EMB_SEW_OUT_APPROVAL_SENT":"EMB - SEW OUT APPROVAL SENT",
    "PROMO_ART_APPROVAL_SENT":  "PROMO - ART APPROVAL SENT",
    "EMB_ORDER_DIGITIZING":     "EMB - ORDER DIGITIZING",
    "DTF_ORDER_TRANSFER":       "DTF - ORDER TRANSFER",
    # Art queue digest (Richie)
    "ARTWORK_DECLINED":         "ARTWORK DECLINED",
    # CX digest Z4 — blocked
    "CONTRACT_WAITING_ARTWORK": "CONTRACT - WAITING ON ARTWORK",
    "CONTRACT_WAITING_GOODS":   "CONTRACT - WAITING ON GOODS",
    "PROMO_ON_ORDER":           "PROMO - ORDER",
    # Lifecycle statuses used by the Cowork skills
    "SHIPPED":                  "SHIPPED",
    "INVOICED":                 "INVOICED",  # formerly "ORDER SHIPPED & INVOICED"
    "MOCKUP_READY":             "MOCK-UP READY",
    "PICKED_UP":                "Picked Up",
}


def _norm_status(name: str) -> str:
    return (name or "").strip().lower()


def _status_matches(live_name: str, key: str) -> bool:
    """Case-insensitive: does a live Printavo status name equal STATUS_NAMES[key]?"""
    return _norm_status(live_name) == _norm_status(STATUS_NAMES[key])


def _parse_iso(ts):
    """Parse an ISO8601 string ('...Z' or offset) to an aware UTC datetime, or None."""
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

# ── CORE HELPER ───────────────────────────────────────────────────────────────
def query_printavo(query: str, variables: dict = None, allow_partial: bool = False):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = httpx.post(
        API_URL,
        json=payload,
        headers={"Content-Type": "application/json", "email": EMAIL, "token": TOKEN},
        timeout=30,
    )
    data = response.json()
    has_errors = "errors" in data
    has_data = bool(data.get("data"))
    if has_errors and (not allow_partial or not has_data):
        return {"error": data["errors"]}
    return data.get("data", {})


# ── PAGINATION — permanent fix for the 25-record cap ─────────────────────────
# Printavo API v2 uses Relay-style cursor pagination (first/after, pageInfo
# { hasNextPage endCursor }) and enforces a HARD per-request maximum of 25:
#   invoices(first: 100) → error "'first' must not be greater than 25"
#   statuses(first: 50)  → SILENTLY clamps to 25 (no error)
# The only correct fix is walking pages. Printavo also has a SILENT rate
# limit — rapid sequential calls return empty pages with no error — so we
# sleep PAGE_DELAY_S between pages and retry empty pages with exponential
# backoff (3 tries).
MAX_PAGE_SIZE = 25
PAGE_DELAY_S  = 0.4
EMPTY_RETRIES = 3


def _dig(data, path):
    for k in path:
        data = (data or {}).get(k) or {}
    return data


def paginate(query: str, variables: dict = None, connection_path=("invoices",),
             max_records: int = None, max_pages: int = 400) -> dict:
    """
    Fetch EVERY node of a Relay connection — always returns the complete set
    (or up to max_records if given).

    `query` must declare $first and $after and select
    `pageInfo { hasNextPage endCursor }` (and ideally `totalNodes`) on the
    connection named by connection_path.

    Returns {"nodes": [...], "totalNodes": int|None, "pages": int,
             "throttled": int} — plus "error" only on hard failure.
    """
    variables = dict(variables or {})
    nodes, total_nodes, throttled = [], None, 0
    after, page = None, 0
    while page < max_pages:
        variables["first"] = MAX_PAGE_SIZE
        variables["after"] = after
        conn = None
        for attempt in range(EMPTY_RETRIES):
            r = query_printavo(query, variables)
            if isinstance(r, dict) and "error" in r:
                return {"nodes": nodes, "totalNodes": total_nodes, "pages": page,
                        "throttled": throttled, "error": r["error"]}
            c = _dig(r, connection_path)
            if c.get("nodes"):
                conn = c
                break
            # Empty page. Legit only if the whole result set is empty
            # (page 0, no next page) — and even then we confirm once, since
            # the silent rate limit returns exactly this shape.
            legit_empty = (page == 0
                           and not (c.get("pageInfo") or {}).get("hasNextPage"))
            if legit_empty and attempt >= 1:
                conn = c
                break
            throttled += 1
            time.sleep(0.8 * (2 ** attempt))  # 0.8s → 1.6s → 3.2s
        if conn is None:
            return {"nodes": nodes, "totalNodes": total_nodes, "pages": page,
                    "throttled": throttled,
                    "error": (f"Page {page + 1} came back empty {EMPTY_RETRIES} "
                              f"times — silent rate limit suspected.")}
        if conn.get("totalNodes") is not None:
            total_nodes = conn["totalNodes"]
        nodes.extend(conn.get("nodes") or [])
        page += 1
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasNextPage") or not pi.get("endCursor"):
            break
        if max_records is not None and len(nodes) >= max_records:
            break
        after = pi["endCursor"]
        time.sleep(PAGE_DELAY_S)
    if max_records is not None:
        nodes = nodes[:max_records]
    return {"nodes": nodes, "totalNodes": total_nodes, "pages": page,
            "throttled": throttled}


def fetch_all_statuses() -> dict:
    """Return EVERY status in the account (beats the 25-per-page cap)."""
    q = """
    query($first: Int, $after: String) {
        statuses(first: $first, after: $after) {
            totalNodes
            nodes { id name color }
            pageInfo { hasNextPage endCursor }
        }
    }
    """
    return paginate(q, connection_path=("statuses",))


# ── DECORATION TYPE HELPERS ───────────────────────────────────────────────────

# Matrix name → decoration type (lowercase key matching)
_MATRIX_TYPE_MAP = {
    "contract sp":       "Screen Print",
    "direct 2024":       "Screen Print",
    "freshprints":       "Screen Print",
    "wholesale direct":  "Screen Print",
    "contract emb":      "Embroidery",
    "embroidery 2025":   "Embroidery",
    "transfers":         "DTF",
}

def _decoration_type(imprint_node: dict, default: str = "Screen Print") -> str:
    """Determine decoration type from an imprint node.
    default: used only when the imprint carries no signal (no typeOfWork and
    no matching pricing matrix) — callers can pass a status-derived fallback."""
    tow = (imprint_node.get("typeOfWork") or {}).get("name", "")
    if tow:
        t = tow.lower()
        if "emb" in t:
            return "Embroidery"
        if "dtf" in t or "transfer" in t:
            return "DTF"
        return "Screen Print"
    matrix_name = (
        (imprint_node.get("pricingMatrixColumn") or {})
        .get("matrix", {})
        .get("name", "")
        .lower()
    )
    for key, deco in _MATRIX_TYPE_MAP.items():
        if key in matrix_name:
            return deco
    return default  # no signal on the imprint itself


def _status_deco_fallback(status_name: str) -> str:
    """Infer decoration type from an order's status name (e.g. 'EMB - PRE-PRO')
    for imprints that carry no typeOfWork / pricing-matrix signal."""
    s = (status_name or "").upper()
    if "EMB" in s:
        return "Embroidery"
    if "DTF" in s:
        return "DTF"
    return "Screen Print"


def _color_count(imprint_node: dict) -> int:
    """Extract screen/color count from pricingMatrixColumn.columnName."""
    col_name = (imprint_node.get("pricingMatrixColumn") or {}).get("columnName", "")
    m = re.search(r'(\d+)\s*[Cc]olor', col_name)
    return int(m.group(1)) if m else 0


def _parse_obj_imprints(obj: dict) -> list:
    """Parse imprint list from a lineItemGroups-bearing object (invoice or quote node)."""
    groups = (obj.get("lineItemGroups") or {}).get("nodes", [])
    imprints = []
    for g in groups:
        for imp in (g.get("imprints") or {}).get("nodes", []):
            imprints.append({
                "type":     _decoration_type(imp),
                "colors":   _color_count(imp),
                "col_name": (imp.get("pricingMatrixColumn") or {}).get("columnName", ""),
            })
    return imprints


def _parse_obj_qty(obj: dict) -> int:
    """Sum size counts across all line items in a lineItemGroups-bearing object."""
    total = 0
    for g in (obj.get("lineItemGroups") or {}).get("nodes", []):
        for item in (g.get("lineItems") or {}).get("nodes", []):
            total += sum(int(s.get("count") or 0) for s in (item.get("sizes") or []))
    return total


_IMPRINT_FRAG = """
    lineItemGroups {
        nodes {
            imprints {
                nodes {
                    typeOfWork { name }
                    pricingMatrixColumn { columnName matrix { name } }
                }
            }
        }
    }
"""

_QTY_FRAG = """
    lineItemGroups {
        nodes {
            lineItems {
                nodes { sizes { size count } }
            }
        }
    }
"""

# Combined per-group fragment: imprints AND quantities per line-item group.
# Needed for correct imprint math on multi-group orders
# (imprints = Σ group_qty × group_imprint_count, NOT total_qty × total_locations).
_GROUPS_FRAG = """
    lineItemGroups {
        nodes {
            imprints {
                nodes {
                    typeOfWork { name }
                    pricingMatrixColumn { columnName matrix { name } }
                }
            }
            lineItems {
                nodes { sizes { size count } }
            }
        }
    }
"""


def _parse_obj_groups(obj: dict, deco_default: str = "Screen Print") -> list:
    """Parse per-group qty + imprints from a lineItemGroups-bearing object.
    Returns list of {"qty": int, "imprints": [{"type","colors"}]}."""
    out = []
    for g in (obj.get("lineItemGroups") or {}).get("nodes", []):
        qty = 0
        for item in (g.get("lineItems") or {}).get("nodes", []):
            qty += sum(int(s.get("count") or 0) for s in (item.get("sizes") or []))
        imps = [{
            "type":   _decoration_type(imp, default=deco_default),
            "colors": _color_count(imp),
        } for imp in (g.get("imprints") or {}).get("nodes", [])]
        out.append({"qty": qty, "imprints": imps})
    return out


def _fetch_groups_batch(id_list: list) -> dict:
    """Fetch per-group qty + imprint data for a list of orders.
    Combined fragment is ~2x the complexity of _IMPRINT_FRAG, so chunk at 3
    (3 × ~7,600 = ~22,800 < 25,000). Returns dict: internal_id → raw obj
    (parse with _parse_obj_groups so the caller can pass a status-based
    decoration fallback). None values should be retried as quotes."""
    raw = _batch_query(id_list, "inv", "invoice", _GROUPS_FRAG, chunk_size=3)
    null_ids = [iid for iid, v in raw.items() if v is None]
    if null_ids:
        raw.update(_batch_query(null_ids, "qt", "quote", _GROUPS_FRAG,
                                allow_partial=True, chunk_size=3))
    return raw


def _batch_query(id_list: list, prefix: str, field: str, frag: str,
                 allow_partial: bool = False, chunk_size: int = 5) -> dict:
    """
    Run aliased GraphQL queries for a list of IDs, chunked to stay under the
    25k complexity limit. Each invoice(id) with lineItemGroups costs ~3,800
    complexity units, so we use chunks of 5 (5 × 3,800 = 19,000 < 25,000).

    prefix: alias prefix, e.g. "inv" → inv0, inv1 ...
    field:  top-level field name, e.g. "invoice" or "quote"
    frag:   field selection body
    Returns dict: id → raw GraphQL object (or None if not found / error).
    """
    CHUNK = chunk_size
    out = {}
    for chunk_start in range(0, len(id_list), CHUNK):
        chunk = id_list[chunk_start:chunk_start + CHUNK]
        parts = [
            f'  {prefix}{chunk_start + i}: {field}(id: "{iid}") {{ {frag} }}'
            for i, iid in enumerate(chunk)
        ]
        q = "query {\n" + "\n".join(parts) + "\n}"
        result = query_printavo(q, allow_partial=allow_partial)
        if "error" in result:
            # Chunk failed entirely — mark all as None
            for iid in chunk:
                out[iid] = None
        else:
            for i, iid in enumerate(chunk):
                out[iid] = result.get(f"{prefix}{chunk_start + i}")
    return out


def _fetch_imprints_batch(id_list: list) -> dict:
    """
    Fetch imprint data for a list of orders via chunked invoice(id) queries.
    Returns dict: internal_id → list of imprint dicts, or None if not found.
    IDs returning None should be retried via _fetch_imprints_quote_batch.
    """
    if not id_list:
        return {}
    raw = _batch_query(id_list, "inv", "invoice", _IMPRINT_FRAG)
    return {
        iid: (_parse_obj_imprints(obj) if obj is not None else None)
        for iid, obj in raw.items()
    }


def _fetch_imprints_quote_batch(id_list: list) -> dict:
    """
    Quote fallback for IDs that returned null from the invoice batch.
    Returns dict: internal_id → list of imprint dicts ([] if not a quote).
    """
    if not id_list:
        return {}
    raw = _batch_query(id_list, "qt", "quote", _IMPRINT_FRAG, allow_partial=True)
    return {
        iid: (_parse_obj_imprints(obj) if obj is not None else [])
        for iid, obj in raw.items()
    }


def _fetch_qty_batch(id_list: list) -> dict:
    """
    Batch-fetch quantity fallback for orders where totalQuantity=0.
    Returns dict: internal_id → total quantity int.
    """
    if not id_list:
        return {}
    raw = _batch_query(id_list, "inv", "invoice", _QTY_FRAG)
    return {
        iid: (_parse_obj_qty(obj) if obj is not None else 0)
        for iid, obj in raw.items()
    }


_CATEGORY_FRAG = """
    lineItemGroups {
        nodes {
            lineItems {
                nodes { category { name } }
            }
        }
    }
"""


def _parse_obj_categories(obj: dict) -> set:
    """Return the set of lowercased line-item category names on an order."""
    cats = set()
    for g in (obj.get("lineItemGroups") or {}).get("nodes", []):
        for item in (g.get("lineItems") or {}).get("nodes", []):
            name = ((item.get("category") or {}).get("name") or "").strip().lower()
            if name:
                cats.add(name)
    return cats


def _fetch_categories_batch(id_list: list) -> dict:
    """Batch-fetch line-item categories. Returns id → set(lowercased names).
    Kept small on purpose: only ever called on the handful of Z1 hits, so the
    nested lineItemGroups fetch never touches the big paginated scans."""
    if not id_list:
        return {}
    raw = _batch_query(id_list, "inv", "invoice", _CATEGORY_FRAG)
    null_ids = [iid for iid, v in raw.items() if v is None]
    if null_ids:
        raw.update(_batch_query(null_ids, "qt", "quote", _CATEGORY_FRAG,
                                allow_partial=True))
    return {
        iid: (_parse_obj_categories(obj) if obj is not None else set())
        for iid, obj in raw.items()
    }


def _order_is_contract(cats: set) -> bool:
    """True if any line-item category contains 'contract' (Contract SP/EMB/DTF)."""
    return any("contract" in c for c in (cats or set()))


def _fetch_qty_from_line_items(internal_id: str) -> int:
    """Single-order qty fallback — kept for use by other tools."""
    q = f"query($id: ID!) {{ invoice(id: $id) {{ {_QTY_FRAG} }} }}"
    r = query_printavo(q, {"id": internal_id})
    if "error" in r:
        return 0
    obj = r.get("invoice")
    return _parse_obj_qty(obj) if obj is not None else 0


def _fetch_imprints_for_order(internal_id: str) -> list:
    """Single-order imprint fetch — kept for use by other tools (e.g. get_production_time_estimate)."""
    q = f"query($id: ID!) {{ invoice(id: $id) {{ {_IMPRINT_FRAG} }} }}"
    r = query_printavo(q, {"id": internal_id})
    if "error" not in r:
        obj = r.get("invoice")
        if obj is not None:
            return _parse_obj_imprints(obj)
    # Fall back to quote
    q2 = f"query($id: ID!) {{ quote(id: $id) {{ {_IMPRINT_FRAG} }} }}"
    r2 = query_printavo(q2, {"id": internal_id})
    if "error" not in r2:
        obj2 = r2.get("quote")
        if obj2 is not None:
            return _parse_obj_imprints(obj2)
    return None


def _sp_run_rate(qty: int) -> int:
    """
    Tiered screen-print impressions/hour.
    Short runs are slower (press setup overhead); long runs get into a groove.
      ≤ 72 pcs  → 250/hr
      73–300    → 400/hr
      301+      → 650/hr
    """
    if qty <= 72:
        return 250
    if qty <= 300:
        return 400
    return 650


def _format_est_time(total_min: int) -> str:
    if total_min <= 0:
        return "?"
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


# ── EXISTING TOOLS ────────────────────────────────────────────────────────────

@mcp.tool()
def get_recent_orders(limit: int = 10) -> str:
    """Get the most recent Printavo orders."""
    q = """
    query($first: Int, $after: String) {
        invoices(first: $first, after: $after, sortDescending: true) {
            totalNodes
            pageInfo { hasNextPage endCursor }
            nodes {
                id
                visualId
                nickname
                total
                dueAt
                startAt
                status { name }
                contact { fullName email }
            }
        }
    }
    """
    result = paginate(q, max_records=limit)
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("nodes", [])
    if not nodes:
        return "No recent orders found."
    lines = [f"RECENT ORDERS (last {len(nodes)}):"]
    for inv in nodes:
        status = (inv.get("status") or {}).get("name", "?")
        contact = (inv.get("contact") or {}).get("fullName", "?")
        due = (inv.get("dueAt") or "")[:10]
        prod = (inv.get("startAt") or "")[:10]
        lines.append(
            f"  #{inv.get('visualId')} | {inv.get('nickname', '')} | "
            f"${inv.get('total', 0)} | {status} | Customer: {contact} | "
            f"Due: {due} | Prod: {prod}"
        )
    return "\n".join(lines)


@mcp.tool()
def search_orders(query: str, limit: int = 10) -> str:
    """
    Search Printavo orders by keyword, order number, customer name, etc.
    query: search string (e.g. '6817', 'Underground Printing', 'BEBT')
    """
    q = """
    query($q: String, $first: Int, $after: String) {
        invoices(first: $first, after: $after, query: $q) {
            totalNodes
            pageInfo { hasNextPage endCursor }
            nodes {
                id
                visualId
                nickname
                total
                dueAt
                status { name }
                contact { fullName email }
            }
        }
    }
    """
    result = paginate(q, {"q": query}, max_records=limit)
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("nodes", [])
    if not nodes:
        return f"No orders found matching '{query}'."
    lines = [f"SEARCH RESULTS for '{query}' ({len(nodes)} found):"]
    for inv in nodes:
        status = (inv.get("status") or {}).get("name", "?")
        contact = (inv.get("contact") or {}).get("fullName", "?")
        due = (inv.get("dueAt") or "")[:10]
        lines.append(
            f"  #{inv.get('visualId')} | {inv.get('nickname', '')} | "
            f"${inv.get('total', 0)} | {status} | {contact} | Due: {due} | "
            f"Internal ID: {inv.get('id')}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_order_details(visual_id: str) -> str:
    """
    Get detailed information about a specific Printavo order.
    visual_id: the order number shown in the Printavo UI (e.g. '6817')
    """
    q = """
    query($q: String) {
        invoices(first: 5, query: $q) {
            nodes {
                id
                visualId
                nickname
                total
                customerDueAt
                dueAt
                startAt
                invoiceAt
                visualPoNumber
                status { name }
                contact { fullName email }
                productionFiles { nodes { id name } }
            }
        }
        quotes(first: 5, query: $q) {
            nodes {
                id
                visualId
                nickname
                total
                customerDueAt
                dueAt
                startAt
                invoiceAt
                visualPoNumber
                status { name }
                contact { fullName email }
                productionFiles { nodes { id name } }
            }
        }
    }
    """
    result = query_printavo(q, {"q": str(visual_id)}, allow_partial=True)
    if "error" in result:
        return f"Error: {result['error']}"
    invoice_nodes = result.get("invoices", {}).get("nodes", [])
    quote_nodes   = result.get("quotes",   {}).get("nodes", [])
    all_nodes = invoice_nodes + quote_nodes
    matching = [n for n in all_nodes if str(n.get("visualId")) == str(visual_id)]
    if not matching:
        return f"Order #{visual_id} not found."
    inv = matching[0]
    contact = inv.get("contact") or {}
    status  = (inv.get("status") or {}).get("name", "?")
    lines = [
        f"ORDER #{inv.get('visualId')} | {inv.get('nickname', '')}",
        f"  Customer:       {contact.get('fullName', '?')}",
        f"  Status:         {status}",
        f"  Total:          ${inv.get('total', 0)}",
        f"  PO #:           {inv.get('visualPoNumber', '')}",
        f"  Customer Due:   {inv.get('customerDueAt', '')}",
        f"  Production Date:{(inv.get('dueAt') or inv.get('startAt') or '')[:10]}",
        f"  Invoice Date:   {inv.get('invoiceAt', '')}",
        f"  Internal ID:    {inv.get('id')}",
    ]
    prod_files = (inv.get("productionFiles") or {}).get("nodes", [])
    if prod_files:
        lines.append(f"\n  Production Files ({len(prod_files)}):")
        for pf in prod_files:
            lines.append(f"    - {pf.get('name', 'unnamed')} (ID: {pf.get('id')})")
    else:
        lines.append("\n  Production Files: none")
    lines.append("\n  (Use get_invoice_structure for line items and imprints)")
    return "\n".join(lines)


@mcp.tool()
def get_statuses() -> str:
    """List ALL available order statuses in Printavo (paginated — complete set)."""
    result = fetch_all_statuses()
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("nodes", [])
    if not nodes:
        return "No statuses found."
    lines = [f"AVAILABLE STATUSES ({len(nodes)} of {result.get('totalNodes')} total):"]
    for s in nodes:
        lines.append(f"  ID: {s.get('id')} | Name: {s.get('name')} | Color: {s.get('color','')}")
    return "\n".join(lines)


@mcp.tool()
def get_outstanding_balances(limit: int = 20) -> str:
    """Get orders with outstanding balances (unpaid invoices)."""
    q = """
    query($first: Int, $after: String) {
        invoices(first: $first, after: $after, query: "balance_due > 0") {
            totalNodes
            pageInfo { hasNextPage endCursor }
            nodes {
                visualId
                nickname
                total
                amountPaid
                dueAt
                contact { fullName }
                status { name }
            }
        }
    }
    """
    result = paginate(q, max_records=limit)
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("nodes", [])
    if not nodes:
        return "No outstanding balances found."
    lines = [f"OUTSTANDING BALANCES ({len(nodes)} orders):"]
    total_outstanding = 0.0
    for inv in nodes:
        total_val = float(inv.get("total") or 0)
        paid_val  = float(inv.get("amountPaid") or 0)
        balance   = total_val - paid_val
        total_outstanding += balance
        contact   = inv.get("contact") or {}
        lines.append(
            f"  #{inv.get('visualId')} | {inv.get('nickname','')} | "
            f"Balance: ${balance:.2f} | Total: ${total_val:.2f} | "
            f"Customer: {contact.get('fullName','?')} | Due: {(inv.get('dueAt') or '')[:10]}"
        )
    lines.append(f"\nTotal Outstanding: ${total_outstanding:.2f}")
    return "\n".join(lines)


@mcp.tool()
def create_quote(customer_email: str, order_name: str, due_date: str) -> str:
    """
    Create a new Printavo quote for an existing customer.
    customer_email: customer's email address in Printavo
    order_name: nickname/label for the order
    due_date: ISO date string e.g. '2026-06-19'
    """
    cq = """
    query($email: String) {
        contacts(first: 5, query: $email) {
            nodes { id fullName email }
        }
    }
    """
    cresult = query_printavo(cq, {"email": customer_email})
    if "error" in cresult:
        return f"Error finding customer: {cresult['error']}"
    contacts = cresult.get("contacts", {}).get("nodes", [])
    if not contacts:
        return f"No customer found with email '{customer_email}'."
    contact_id = contacts[0]["id"]
    mutation = """
    mutation($contactId: ID!, $nickname: String, $dueAt: ISO8601DateTime!, $customerDueAt: ISO8601Date!) {
        quoteCreate(input: {
            contact: { id: $contactId },
            nickname: $nickname,
            dueAt: $dueAt,
            customerDueAt: $customerDueAt
        }) {
            id visualId nickname dueAt
        }
    }
    """
    result = query_printavo(mutation, {
        "contactId":     contact_id,
        "nickname":      order_name,
        "dueAt":         f"{due_date}T12:00:00Z",
        "customerDueAt": due_date,
    })
    if "error" in result:
        return f"Error: {result['error']}"
    quote = result.get("quoteCreate", {}) or {}
    return (
        f"Quote created!\n"
        f"  Order #: {quote.get('visualId')}\n"
        f"  Nickname: {quote.get('nickname')}\n"
        f"  Due: {(quote.get('dueAt') or '')[:10]}\n"
        f"  Internal ID: {quote.get('id')}"
    )


@mcp.tool()
def inspect_fields(type_name: str) -> str:
    """
    Inspect the GraphQL fields available on a Printavo type.
    type_name: GraphQL type name (e.g. 'Invoice', 'LineItem', 'Imprint', 'Contact')
    """
    q = """
    query($name: String!) {
        __type(name: $name) {
            name kind
            fields {
                name
                type { name kind ofType { name kind } }
                args { name type { name kind ofType { name kind } } }
            }
            inputFields {
                name
                type { name kind ofType { name kind } }
            }
        }
    }
    """
    result = query_printavo(q, {"name": type_name})
    if "error" in result:
        return f"Error: {result['error']}"
    t = result.get("__type")
    if not t:
        return f"Type '{type_name}' not found. Try: Invoice, LineItem, LineItemGroup, Imprint, Contact, Status, PricingMatrix, PricingMatrixColumn"
    lines = [f"TYPE: {t.get('name')} ({t.get('kind')})"]
    fields = t.get("fields") or []
    if fields:
        lines.append("\nFIELDS:")
        for f in fields:
            ftype = f.get("type") or {}
            tname = ftype.get("name") or ((ftype.get("ofType") or {}).get("name"))
            lines.append(f"  {f.get('name')}: {tname or ftype.get('kind','?')}")
    input_fields = t.get("inputFields") or []
    if input_fields:
        lines.append("\nINPUT FIELDS:")
        for f in input_fields:
            ftype = f.get("type") or {}
            tname = ftype.get("name") or ((ftype.get("ofType") or {}).get("name"))
            lines.append(f"  {f.get('name')}: {tname or ftype.get('kind','?')}")
    return "\n".join(lines)


@mcp.tool()
def diagnose_order(visual_id: str) -> str:
    """
    Run a full diagnostic on a Printavo order — checks all key fields,
    imprint pricing, line items, and flags any issues.
    visual_id: order number shown in Printavo UI
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return f"Error: {err}"
    frag = """
                id visualId nickname total customerDueAt startAt invoiceAt visualPoNumber
                status { name }
                contact { fullName email }
                lineItemGroups {
                    nodes {
                        id
                        lineItems {
                            nodes {
                                id itemNumber description color price
                                sizes { size count }
                            }
                        }
                        imprints {
                            nodes {
                                id
                                pricingMatrixColumn { id columnName }
                            }
                        }
                    }
                }
                productionFiles { nodes { id name } }
    """
    q = f"""
    query($id: ID!) {{
        {order_type}(id: $id) {{ {frag} }}
    }}
    """
    result = query_printavo(q, {"id": internal_id})
    if "error" in result:
        return f"Error: {result['error']}"
    inv = result.get(order_type)
    if not inv:
        return f"Order #{visual_id} not found."
    issues = []
    if not inv.get("nickname"):      issues.append("⚠ MISSING: nickname")
    if not inv.get("visualPoNumber"): issues.append("⚠ MISSING: PO number")
    if not inv.get("customerDueAt"): issues.append("⚠ MISSING: customer due date")
    if not inv.get("startAt"):       issues.append("⚠ MISSING: production date")
    if not inv.get("invoiceAt"):     issues.append("⚠ MISSING: invoice date")
    groups = (inv.get("lineItemGroups") or {}).get("nodes", [])
    if not groups:
        issues.append("⚠ MISSING: no line item groups")
    else:
        for gi, g in enumerate(groups, 1):
            items = (g.get("lineItems") or {}).get("nodes", [])
            if not items:
                issues.append(f"⚠ Group {gi}: no line items")
            for item in items:
                total_qty = sum((s.get("count") or 0) for s in (item.get("sizes") or []))
                if total_qty == 0:
                    issues.append(f"⚠ Line item {item.get('id')}: no sizes/qty")
                if not item.get("price") or float(item.get("price") or 0) == 0:
                    issues.append(f"⚠ Line item {item.get('id')}: $0 price (pricing not set?)")
            imprints = (g.get("imprints") or {}).get("nodes", [])
            if not imprints:
                issues.append(f"⚠ Group {gi}: no imprints")
            for imp in imprints:
                col = (imp.get("pricingMatrixColumn") or {})
                if not col.get("id"):
                    issues.append(f"⚠ Imprint {imp.get('id')}: no pricing matrix column set")
    contact = inv.get("contact") or {}
    status  = (inv.get("status") or {}).get("name", "?")
    prod_files = (inv.get("productionFiles") or {}).get("nodes", [])
    lines = [
        f"DIAGNOSTIC — Order #{inv.get('visualId')} | {inv.get('nickname','')}",
        f"  Status: {status} | Total: ${inv.get('total',0)}",
        f"  Customer: {contact.get('fullName','?')}",
        f"  Due: {inv.get('customerDueAt', '')} | "
        f"Prod: {(inv.get('startAt') or '')[:10]} | "
        f"Invoice: {inv.get('invoiceAt', '')}",
        f"  PO #: {inv.get('visualPoNumber','')}",
        f"  Production Files: {len(prod_files)}",
        f"  Line Item Groups: {len(groups)}",
    ]
    if issues:
        lines.append(f"\nISSUES FOUND ({len(issues)}):")
        for issue in issues:
            lines.append(f"  {issue}")
    else:
        lines.append("\n✓ No issues found — order looks complete.")
    return "\n".join(lines)


# ── FIXED: get_production_schedule ────────────────────────────────────────────
# BUG (original): the query never fetched lineItemGroups/imprints at all,
# so Items and Imprints were always 0 for every order.
#
# FIX: two-step approach
#   Step 1 — get order list with minimal fields (avoids complexity limit)
#   Step 2 — per order, call invoice(id) → lineItemGroups → imprints
#             invoice(id) is a single-object query and reliably traverses
#             nested .nodes without hitting the 25k complexity ceiling.

@mcp.tool()
def get_production_schedule(days_ahead: int = 7) -> str:
    """
    Get the production schedule — orders due in the next N days, with full
    imprint, screen count, and estimated production time per order.
    days_ahead: how many days ahead to look (default 7)
    """
    now      = datetime.now(timezone.utc)
    end_date = now + timedelta(days=days_ahead)
    start_str = now.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")

    # ── Step 1: lightweight list query (no nested collections) ──────────────
    # Uses inProductionAfter/inProductionBefore named args — confirmed working
    # in direct GraphQL testing. The query-string `production_at >=` syntax
    # is unreliable; named params are the correct approach.
    q_list = """
    query($prodAfter: ISO8601DateTime, $prodBefore: ISO8601DateTime, $first: Int, $after: String) {
        invoices(inProductionAfter: $prodAfter, inProductionBefore: $prodBefore, first: $first, after: $after) {
            totalNodes
            pageInfo { hasNextPage endCursor }
            nodes {
                id visualId nickname total totalQuantity
                dueAt startAt
                status { name }
                contact { fullName }
            }
        }
    }
    """
    result = paginate(q_list, {
        "prodAfter": f"{start_str}T00:00:00Z",
        "prodBefore": f"{end_str}T23:59:59Z",
    })
    if "error" in result:
        return f"Error: {result['error']}"
    nodes = result.get("nodes", [])
    if not nodes:
        return f"No orders scheduled for production in the next {days_ahead} days."

    # ── Step 2: batch imprint fetch ──────────────────────────────────────────
    # All orders are fetched in ONE GraphQL query using field aliases.
    # This eliminates per-order HTTP calls and avoids Printavo rate limits.
    # IDs that return null from the invoice batch are retried as quotes.

    all_ids = [n["id"] for n in nodes if n.get("id")]
    imprint_map = _fetch_imprints_batch(all_ids)

    # Quote fallback for IDs where invoice returned null
    null_ids = [iid for iid, v in imprint_map.items() if v is None]
    if null_ids:
        qt_map = _fetch_imprints_quote_batch(null_ids)
        imprint_map.update(qt_map)

    # Qty fallback batch for orders where Step 1 totalQuantity=0
    zero_qty_ids = [
        n["id"] for n in nodes
        if n.get("id") and int(n.get("totalQuantity") or 0) == 0
    ]
    qty_map = _fetch_qty_batch(zero_qty_ids) if zero_qty_ids else {}

    today_str = now.strftime("%Y-%m-%d")
    lines = [
        f"PRODUCTION SCHEDULE — {start_str} to {end_str}",
        f"  {len(nodes)} orders | Generated {today_str}\n",
    ]

    for inv in nodes:
        internal_id = inv.get("id")
        status  = (inv.get("status") or {}).get("name", "?")
        contact = (inv.get("contact") or {}).get("fullName", "Unknown")
        prod_dt = (inv.get("startAt") or "")[:10]
        due_dt  = (inv.get("dueAt") or "")[:10]

        # totalQuantity from Step 1 (flat scalar — reliable for most orders)
        total_qty = int(inv.get("totalQuantity") or 0)

        # Header line
        lines.append(f"#{inv.get('visualId')} | {contact} | {inv.get('nickname', '')}")

        imprint_nodes = imprint_map.get(internal_id) if internal_id else None
        if imprint_nodes is None:
            lines.append(
                f"  Status: {status} | Prod: {prod_dt} | Due: {due_dt} | "
                f"⚠️ could not fetch imprint data"
            )
            lines.append("")
            continue

        # If Step 1 totalQuantity is 0, use the qty batch fallback.
        if total_qty == 0 and internal_id:
            total_qty = qty_map.get(internal_id, 0)

        if not imprint_nodes:
            # Order is in schedule but has no imprints set in Printavo
            lines.append(
                f"  Status: {status} | Items: {total_qty} | "
                f"Total Imprints: 0 | Screens: ⚠️ not entered | "
                f"Est. Time: ⚠️ unknown"
            )
            if total_qty > 0:
                lines.append(f"  ⚠️ {total_qty} pcs — no imprints entered")
            lines.append("")
            continue

        # ── Calculate totals ─────────────────────────────────────────────────
        num_locations  = len(imprint_nodes)
        total_imprints = total_qty * num_locations
        total_screens  = sum(i["colors"] for i in imprint_nodes)

        # Group by decoration type for per-type breakdown
        type_groups: dict = defaultdict(lambda: {"locations": 0, "screens": 0})
        for imp in imprint_nodes:
            tg = type_groups[imp["type"]]
            tg["locations"] += 1
            tg["screens"]   += imp["colors"]

        # Estimate time
        # Setup:   screens × 8 min (burn + press setup per screen)
        # SP run:  (qty × sp_locations) / tiered_rate × 60
        # EMB/DTF: 15 min per location (rough)
        sp_locs    = type_groups.get("Screen Print", {}).get("locations", 0)
        sp_screens = type_groups.get("Screen Print", {}).get("screens", 0)
        sp_imprints = total_qty * sp_locs
        rate        = _sp_run_rate(total_qty)
        sp_run_min  = int(sp_imprints / rate * 60) if sp_imprints else 0
        emb_dtf_min = (
            type_groups.get("Embroidery", {}).get("locations", 0) +
            type_groups.get("DTF", {}).get("locations", 0)
        ) * 15
        setup_min  = sp_screens * 8
        total_min  = setup_min + sp_run_min + emb_dtf_min
        est_time   = _format_est_time(total_min)

        screens_str = str(total_screens) if total_screens > 0 else "N/A"
        lines.append(
            f"  Status: {status} | Items: {total_qty} | "
            f"Total Imprints: {total_imprints} | Screens: {screens_str} | "
            f"Est. Time: {est_time}"
        )

        # Per-decoration-type breakdown
        for deco_type, tg in type_groups.items():
            locs        = tg["locations"]
            type_total  = total_qty * locs
            scr         = tg["screens"]
            detail = f"  {deco_type} | {total_qty} pcs × {locs} imprint(s) = {type_total} imprints"
            if scr > 0:
                detail += f" | {scr} screens"
            detail += f" | {est_time}"
            lines.append(detail)

        lines.append("")  # blank line between orders

    return "\n".join(lines)


@mcp.tool()
def send_schedule_to_slack(days_ahead: int = 7) -> str:
    """Post the production schedule to the configured Slack channel."""
    if not SLACK_WEBHOOK_URL:
        return "Error: SLACK_WEBHOOK_URL environment variable not set."
    schedule_text = get_production_schedule(days_ahead)
    payload = {
        "text": f"*Printavo Production Schedule*\n```{schedule_text}```"
    }
    response = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
    if response.status_code == 200:
        return "Schedule posted to Slack successfully."
    return f"Slack error: HTTP {response.status_code} — {response.text}"


# ── FIXED: get_production_time_estimate ───────────────────────────────────────
# BUG (original): used invoices(query) list query with nested lineItemGroups.
# List queries with deeply nested nodes hit the 25k complexity ceiling for
# some orders (especially those not yet in CONTRACT status), returning
# empty lineItemGroups and therefore 0 imprints.
#
# FIX: use _find_order() to get the internal ID, then invoice(id) for
# the nested data — same reliable single-object pattern used everywhere else.

@mcp.tool()
def get_production_time_estimate(visual_id: str) -> str:
    """
    Estimate production time for an order based on quantity, imprint count, and decoration type.
    visual_id: order number shown in Printavo UI
    """
    internal_id, _order_type, err = _find_order(visual_id)
    if err:
        return f"Error finding order: {err}"

    # Fetch basic info
    q_basic = """
    query($id: ID!) {
        invoice(id: $id) {
            visualId nickname totalQuantity
            lineItemGroups {
                nodes {
                    imprints {
                        nodes {
                            typeOfWork { name }
                            pricingMatrixColumn { columnName matrix { name } }
                        }
                    }
                }
            }
        }
    }
    """
    result = query_printavo(q_basic, {"id": internal_id})
    if "error" in result:
        return f"Error: {result['error']}"

    inv = result.get("invoice")
    if not inv:
        return f"Order #{visual_id} not found."

    total_qty = int(inv.get("totalQuantity") or 0)
    groups = inv.get("lineItemGroups", {}).get("nodes", [])
    imprint_info = []
    for g in groups:
        for imp in g.get("imprints", {}).get("nodes", []):
            imprint_info.append({
                "type":   _decoration_type(imp),
                "colors": _color_count(imp),
            })

    total_prints = len(imprint_info)

    if total_qty > 0 and imprint_info:
        sp_info     = [i for i in imprint_info if i["type"] == "Screen Print"]
        sp_locs     = len(sp_info)
        sp_screens  = sum(i["colors"] for i in sp_info)
        sp_imprints = total_qty * sp_locs
        rate        = _sp_run_rate(total_qty)
        sp_run_min  = int(sp_imprints / rate * 60) if sp_imprints else 0
        emb_dtf_min = sum(15 for i in imprint_info if i["type"] in ("Embroidery", "DTF"))
        setup_min   = sp_screens * 8
        total_min   = setup_min + sp_run_min + emb_dtf_min
    else:
        total_min = 0

    lines = [
        f"PRODUCTION TIME ESTIMATE — Order #{inv.get('visualId')} | {inv.get('nickname','')}",
        f"  Total Quantity:  {total_qty} pcs",
        f"  Print Locations: {total_prints}",
        f"  Imprint Details: {imprint_info}",
        f"  TOTAL ESTIMATE:  {_format_est_time(total_min)} ({total_min} min)",
        f"  Note: Tiered SP rate — ≤72 pcs: 250/hr | 73–300: 400/hr | 301+: 650/hr.",
    ]
    return "\n".join(lines)


# ── PRIVATE HELPERS (not tools) ───────────────────────────────────────────────

def _find_invoice_internal_id(visual_id: str):
    internal_id, _order_type, err = _find_order(visual_id)
    return internal_id, err


def _find_order(visual_id: str):
    """Returns (internal_id, order_type, error_string). Searches invoices and quotes."""
    q = """
    query($q: String) {
        invoices(first: 5, query: $q) {
            nodes { id visualId }
        }
        quotes(first: 5, query: $q) {
            nodes { id visualId }
        }
    }
    """
    result = query_printavo(q, {"q": str(visual_id)}, allow_partial=True)
    if "error" in result:
        return None, None, f"API Error: {result['error']}"
    invoice_nodes = result.get("invoices", {}).get("nodes", [])
    quote_nodes   = result.get("quotes",   {}).get("nodes", [])
    for n in invoice_nodes:
        if str(n.get("visualId")) == str(visual_id):
            return n["id"], "invoice", None
    for n in quote_nodes:
        if str(n.get("visualId")) == str(visual_id):
            return n["id"], "quote", None
    return None, None, f"Order #{visual_id} not found in invoices or quotes."


def _get_status_id_by_name(status_name: str):
    """Case-insensitive status lookup over the COMPLETE paginated status list."""
    result = fetch_all_statuses()
    if "error" in result:
        return None, f"API Error: {result['error']}"
    statuses = result.get("nodes", [])
    for s in statuses:
        if _norm_status(s.get("name")) == _norm_status(status_name):
            return s["id"], None
    names = [s.get("name") for s in statuses]
    return None, f"Status '{status_name}' not found. Available: {names}"


def _find_pricing_matrix_column_id(color_count: int):
    q = """
    query {
        account {
            pricingMatrices(first: 50) {
                nodes {
                    id name
                    columns { id columnName }
                }
            }
        }
    }
    """
    result = query_printavo(q)
    if "error" in result:
        return None, f"API Error: {result['error']}"
    matrices = result.get("account", {}).get("pricingMatrices", {}).get("nodes", [])
    target = None
    for m in matrices:
        if "contract sp 2025" in m.get("name", "").lower():
            target = m
            break
    if not target:
        names = [m.get("name") for m in matrices]
        return None, f"'Contract SP 2025 (NEW)' matrix not found. Available: {names}"
    columns = target.get("columns", [])
    for col in columns:
        if re.search(rf'\b{color_count}\s+[Cc]olor', col.get("columnName", "")):
            return col["id"], None
    col_names = [c.get("columnName") for c in columns]
    return None, f"No {color_count}-color column found in matrix '{target.get('name')}'. Columns: {col_names}"


def _upload_file_rest(endpoint: str, file_path: str, extra_data: dict) -> dict:
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}
    with open(file_path, "rb") as f:
        filename = os.path.basename(file_path)
        response = httpx.post(
            f"https://www.printavo.com/api/v2/{endpoint}",
            data=extra_data,
            files={"file": (filename, f, "application/octet-stream")},
            headers={"email": EMAIL, "token": TOKEN},
            timeout=60,
        )
    if response.status_code in (200, 201):
        try:
            return response.json()
        except Exception:
            return {"success": True, "status": response.status_code}
    return {"error": f"HTTP {response.status_code}: {response.text[:400]}"}


def _normalize_size_key(size_str: str) -> str:
    size_map = {
        "XS": "size_xs", "S": "size_s", "M": "size_m", "L": "size_l",
        "XL": "size_xl", "2XL": "size_2xl", "XXL": "size_2xl",
        "3XL": "size_3xl", "XXXL": "size_3xl", "4XL": "size_4xl",
        "5XL": "size_5xl", "6XL": "size_6xl",
        "WXS": "size_wxs", "WS": "size_ws", "WM": "size_wm",
        "WL": "size_wl", "WXL": "size_wxl", "W2XL": "size_w2xl",
    }
    return size_map.get(size_str.strip().upper(), f"size_{size_str.strip().lower()}")


# ── NEW TOOLS: UGP QUOTE ENTRY WORKFLOW ───────────────────────────────────────

@mcp.tool()
def duplicate_invoice(source_visual_id: str) -> str:
    """
    Duplicate a Printavo invoice (used to copy template order 6817 for each UGP order).
    source_visual_id: the order number to duplicate (e.g. '6817')
    """
    internal_id, err = _find_invoice_internal_id(source_visual_id)
    if err:
        return err
    mutation = """
    mutation($id: ID!) {
        invoiceDuplicate(id: $id) {
            id visualId nickname
        }
    }
    """
    result = query_printavo(mutation, {"id": internal_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    invoice = result.get("invoiceDuplicate", {})
    if not invoice:
        return f"Unexpected response — 'invoiceDuplicate' key missing. Raw: {result}"
    return (
        f"Invoice duplicated!\n"
        f"New Order # (visual): {invoice.get('visualId')}\n"
        f"Internal ID: {invoice.get('id')}\n"
        f"Nickname: {invoice.get('nickname', '')}\n"
        f"NEXT: Run delete_production_files('{invoice.get('visualId')}') "
        f"to remove inherited artwork from the template."
    )


@mcp.tool()
def update_invoice_fields(
    visual_id: str,
    nickname: str,
    po_number: str,
    production_date: str,
    customer_due_date: str,
    invoice_date: str,
) -> str:
    """
    Update the header fields on a Printavo invoice.
    visual_id: order number shown in Printavo UI (e.g. '6999')
    nickname: e.g. 'Minot AF Ball Tees - 1151454'
    po_number: UGP order number as PO (e.g. '1151454')
    production_date: YYYY-MM-DD
    customer_due_date: YYYY-MM-DD
    invoice_date: YYYY-MM-DD
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    update_field = "quoteUpdate" if order_type == "quote" else "invoiceUpdate"
    if order_type == "quote":
        mutation = f"""
        mutation($id: ID!, $nickname: String, $visualPoNumber: String, $dueAt: ISO8601DateTime, $customerDueAt: ISO8601Date) {{
            {update_field}(id: $id, input: {{
                nickname: $nickname, visualPoNumber: $visualPoNumber,
                dueAt: $dueAt, customerDueAt: $customerDueAt
            }}) {{
                id visualId nickname visualPoNumber customerDueAt dueAt startAt invoiceAt
            }}
        }}
        """
        variables = {
            "id": internal_id, "nickname": nickname,
            "visualPoNumber": po_number, "dueAt": f"{production_date}T12:00:00Z",
            "customerDueAt": customer_due_date,
        }
    else:
        mutation = f"""
        mutation($id: ID!, $nickname: String, $visualPoNumber: String, $startAt: ISO8601DateTime, $customerDueAt: ISO8601Date, $invoiceAt: ISO8601Date) {{
            {update_field}(id: $id, input: {{
                nickname: $nickname, visualPoNumber: $visualPoNumber,
                startAt: $startAt, customerDueAt: $customerDueAt, invoiceAt: $invoiceAt
            }}) {{
                id visualId nickname visualPoNumber customerDueAt startAt invoiceAt
            }}
        }}
        """
        variables = {
            "id": internal_id, "nickname": nickname, "visualPoNumber": po_number,
            "startAt": f"{production_date}T12:00:00Z", "customerDueAt": customer_due_date,
            "invoiceAt": invoice_date,
        }
    result = query_printavo(mutation, variables)
    if "error" in result:
        return f"API Error: {result['error']}"
    inv = result.get(update_field, {})
    prod_date = (inv.get('dueAt') or inv.get('startAt') or '')[:10]
    return (
        f"Order #{inv.get('visualId')} header updated!\n"
        f"  Nickname:          {inv.get('nickname')}\n"
        f"  PO #:              {inv.get('visualPoNumber')}\n"
        f"  Production Date:   {prod_date}\n"
        f"  Customer Due Date: {inv.get('customerDueAt', '')}\n"
        f"  Invoice Date:      {inv.get('invoiceAt', '')}"
    )


@mcp.tool()
def get_invoice_structure(visual_id: str) -> str:
    """
    Get all line item group IDs, line item IDs, and imprint IDs for an invoice.
    visual_id: order number shown in Printavo UI
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err
    fragment = """
        visualId nickname
        productionFiles { nodes { id name } }
        lineItemGroups {
            nodes {
                id
                lineItems {
                    nodes {
                        id itemNumber description color
                        sizes { size count }
                    }
                }
                imprints {
                    nodes {
                        id
                        pricingMatrixColumn { id columnName }
                    }
                }
            }
        }
    """
    q = f"""
    query($id: ID!) {{
        invoice(id: $id) {{ {fragment} }}
        quote(id: $id) {{ {fragment} }}
    }}
    """
    result = query_printavo(q, {"id": internal_id}, allow_partial=True)
    if "error" in result:
        return f"API Error: {result['error']}"
    inv = result.get("invoice") or result.get("quote") or {}
    if not inv:
        return f"Order #{visual_id} not found via direct ID lookup."
    lines = [f"STRUCTURE — Order #{inv.get('visualId')} | {inv.get('nickname', '')}"]
    prod_files = (inv.get("productionFiles") or {}).get("nodes", [])
    lines.append(f"\nProduction Files ({len(prod_files)}):")
    for pf in prod_files:
        lines.append(f"  ID: {pf.get('id')} | {pf.get('name', 'unnamed')}")
    groups = (inv.get("lineItemGroups") or {}).get("nodes", [])
    lines.append(f"\nLine Item Groups ({len(groups)}):")
    for gi, g in enumerate(groups, 1):
        lines.append(f"\n  Group {gi} | ID: {g.get('id')}")
        items = (g.get("lineItems") or {}).get("nodes", [])
        for ii, item in enumerate(items, 1):
            sizes_str = ", ".join(
                f"{s.get('size','?').replace('size_','').upper()}:{s.get('count',0)}"
                for s in (item.get("sizes") or [])
                if (s.get("count") or 0) > 0
            )
            lines.append(
                f"    Line Item {ii} | ID: {item.get('id')} | "
                f"#{item.get('itemNumber','?')} | {item.get('color','?')} | "
                f"{(item.get('description') or '')[:60]}"
            )
            if sizes_str:
                lines.append(f"      Sizes: {sizes_str}")
        imprints = (g.get("imprints") or {}).get("nodes", [])
        for ii, imp in enumerate(imprints, 1):
            col = imp.get("pricingMatrixColumn") or {}
            lines.append(
                f"    Imprint {ii} | ID: {imp.get('id')} | "
                f"Column: {col.get('columnName', 'none')} (Col ID: {col.get('id', 'none')})"
            )
    return "\n".join(lines)


@mcp.tool()
def delete_production_files(visual_id: str) -> str:
    """
    Delete ALL inherited files from a Printavo order after duplicating template 6817.
    visual_id: order number shown in Printavo UI
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    type_field = order_type
    q = f"""
    query($id: ID!) {{
        {type_field}(id: $id) {{
            productionFiles {{ nodes {{ id name }} }}
            lineItemGroups {{ nodes {{ lineItems {{ nodes {{ id mockups {{ nodes {{ id }} }} }} }} }} }}
        }}
    }}
    """
    result = query_printavo(q, {"id": internal_id}, allow_partial=True)
    if "error" in result:
        return f"API Error: {result['error']}"
    obj = result.get(type_field) or {}
    files = obj.get("productionFiles", {}).get("nodes", [])
    del_pf_mutation = """mutation($id: ID!) { productionFileDelete(id: $id) { id } }"""
    deleted_files, failed_files = [], []
    for pf in files:
        dr = query_printavo(del_pf_mutation, {"id": pf["id"]})
        (failed_files if "error" in dr else deleted_files).append(pf.get("name", pf["id"]))
    mockups_to_delete = []
    for g in (obj.get("lineItemGroups") or {}).get("nodes", []):
        for item in (g.get("lineItems") or {}).get("nodes", []):
            for m in (item.get("mockups") or {}).get("nodes", []):
                mockups_to_delete.append(m["id"])
    del_mockup_mutation = """mutation($id: ID!) { mockupDelete(id: $id) { id } }"""
    deleted_mockups, failed_mockups = [], []
    for mid in mockups_to_delete:
        dr = query_printavo(del_mockup_mutation, {"id": mid})
        (failed_mockups if "error" in dr else deleted_mockups).append(mid)
    lines = [
        f"Order #{visual_id} — inherited files cleared:",
        f"  Production files: {len(deleted_files)}/{len(files)} deleted",
    ]
    for name in deleted_files:
        lines.append(f"    ✓ {name}")
    lines.append(f"  Line item mockups: {len(deleted_mockups)}/{len(mockups_to_delete)} deleted")
    if failed_files:   lines.append(f"  FAILED files: {failed_files}")
    if failed_mockups: lines.append(f"  FAILED mockups: {failed_mockups}")
    return "\n".join(lines)


@mcp.tool()
def update_line_item(
    line_item_id: str,
    color: str,
    description: str,
    sizes_json: str,
    item_number: str = "SCRN",
) -> str:
    """
    Update a line item's item number, color, description, and size quantities.
    line_item_id: internal GraphQL ID of the line item
    color: garment color
    description: multi-line description
    sizes_json: JSON object e.g. '{"S": 12, "M": 12}'
    item_number: always 'SCRN' for screen print orders (default)
    """
    try:
        sizes_dict = json.loads(sizes_json)
    except Exception as e:
        return f"Invalid sizes_json: {e}"
    STANDARD_SIZES = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL"]
    normalized_input = {k.strip().upper(): int(v) for k, v in sizes_dict.items()}
    full_sizes = {s: normalized_input.get(s, 0) for s in STANDARD_SIZES}
    for k, v in normalized_input.items():
        if k not in STANDARD_SIZES:
            full_sizes[k] = v
    sizes_gql = ", ".join(
        f'{{size: {_normalize_size_key(k)}, count: {v}}}'
        for k, v in full_sizes.items()
    )
    mutation = f"""
    mutation($id: ID!, $itemNumber: String, $color: String, $description: String) {{
        lineItemUpdate(id: $id, input: {{
            itemNumber: $itemNumber, color: $color, description: $description,
            position: 1, sizes: [{sizes_gql}]
        }}) {{
            id itemNumber description color
            sizes {{ size count }}
        }}
    }}
    """
    result = query_printavo(mutation, {
        "id": line_item_id, "itemNumber": item_number,
        "color": color, "description": description,
    })
    if "error" in result:
        return f"API Error: {result['error']}"
    item = result.get("lineItemUpdate", {})
    sizes_summary = ", ".join(
        f"{s.get('size')}:{s.get('count')}"
        for s in (item.get("sizes") or [])
        if (s.get("count") or 0) > 0
    ) or ", ".join(f"{k}:{v}" for k, v in sizes_dict.items() if int(v) > 0)
    return (
        f"Line item updated!\n"
        f"  ID: {item.get('id')}\n"
        f"  Item #: {item.get('itemNumber')}\n"
        f"  Color: {item.get('color')}\n"
        f"  Description: {(item.get('description') or '')[:100]}\n"
        f"  Sizes: {sizes_summary}"
    )


@mcp.tool()
def duplicate_line_item(line_item_id: str) -> str:
    """
    Duplicate a line item within its line item group.
    line_item_id: internal GraphQL ID of the line item to duplicate
    """
    fetch_q = """
    query($id: ID!) {
        lineItem(id: $id) {
            id itemNumber color description position
            lineItemGroup { id }
            sizes { size count }
        }
    }
    """
    src = query_printavo(fetch_q, {"id": line_item_id})
    if "error" in src:
        return f"API Error fetching source line item: {src['error']}"
    src_item = src.get("lineItem")
    if not src_item:
        return f"Line item {line_item_id} not found."
    group_id = (src_item.get("lineItemGroup") or {}).get("id")
    if not group_id:
        return "Could not determine line item group ID."
    sizes_gql = ", ".join(
        f'{{size: {s["size"]}, count: {s["count"] or 0}}}'
        for s in (src_item.get("sizes") or [])
    )
    next_pos = (src_item.get("position") or 1) + 1
    create_mutation = f"""
    mutation($groupId: ID!, $itemNumber: String, $color: String, $description: String) {{
        lineItemCreate(lineItemGroupId: $groupId, input: {{
            itemNumber: $itemNumber, color: $color, description: $description,
            position: {next_pos}, sizes: [{sizes_gql}]
        }}) {{
            id itemNumber description color
            sizes {{ size count }}
        }}
    }}
    """
    result = query_printavo(create_mutation, {
        "groupId": group_id, "itemNumber": src_item.get("itemNumber", "SCRN"),
        "color": src_item.get("color", ""), "description": src_item.get("description", ""),
    })
    if "error" in result:
        return f"API Error: {result['error']}"
    item = result.get("lineItemCreate")
    if not item:
        return f"Unexpected response — 'lineItemCreate' key missing. Raw: {result}"
    return (
        f"Line item duplicated!\n"
        f"New Line Item ID: {item.get('id')}\n"
        f"  Color: {item.get('color')}\n"
        f"  Description: {(item.get('description') or '')[:80]}\n"
        f"Now call update_line_item(line_item_id='{item.get('id')}', ...) to set new product details."
    )


@mcp.tool()
def set_imprint_pricing(imprint_id: str, color_count: int) -> str:
    """
    Set an imprint's pricing to 'Contract SP 2025 (NEW)' matrix with the given color count.
    imprint_id: internal GraphQL ID of the imprint
    color_count: number of ink colors (1–6)
    """
    col_id, err = _find_pricing_matrix_column_id(color_count)
    if err:
        return f"Could not find pricing column: {err}"
    mutation = """
    mutation($id: ID!, $colId: ID!) {
        imprintUpdate(id: $id, input: { pricingMatrixColumn: { id: $colId } }) {
            id pricingMatrixColumn { id columnName }
        }
    }
    """
    result = query_printavo(mutation, {"id": imprint_id, "colId": col_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    imp = result.get("imprintUpdate", {})
    col_name = (imp.get("pricingMatrixColumn") or {}).get("columnName", "?")
    return f"Imprint {imprint_id} pricing set to: {col_name}"


@mcp.tool()
def add_imprint(line_item_group_id: str, color_count: int) -> str:
    """
    Add a new imprint row to a line item group and set its pricing matrix.
    line_item_group_id: internal GraphQL ID of the line item group
    color_count: number of ink colors (1–6)
    """
    col_id, err = _find_pricing_matrix_column_id(color_count)
    if err:
        return f"Could not find pricing column: {err}"
    mutation = """
    mutation($groupId: ID!, $colId: ID!) {
        imprintCreate(lineItemGroupId: $groupId, input: { pricingMatrixColumn: { id: $colId } }) {
            id pricingMatrixColumn { id columnName }
        }
    }
    """
    result = query_printavo(mutation, {"groupId": line_item_group_id, "colId": col_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    cr = result.get("imprintCreate", {})
    if not cr:
        return f"Unexpected response — 'imprintCreate' key missing.\nRaw: {result}"
    col_name = (cr.get("pricingMatrixColumn") or {}).get("columnName", "?")
    return (
        f"Imprint added!\n"
        f"New Imprint ID: {cr.get('id')}\n"
        f"Matrix column: {col_name}"
    )


@mcp.tool()
def add_line_item_group(visual_id: str) -> str:
    """Create a new empty line item group on a quote/invoice (returns the group ID).
    Use before add_line_item. One group per distinct product for promo quotes.
    visual_id: order number shown in Printavo."""
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err
    # position = next slot after existing groups
    cq = """
    query($id: ID!) {
        invoice(id: $id) { lineItemGroups { nodes { id } } }
        quote(id: $id)   { lineItemGroups { nodes { id } } }
    }
    """
    cres = query_printavo(cq, {"id": internal_id}, allow_partial=True)
    node = cres.get("invoice") or cres.get("quote") or {}
    pos = len((node.get("lineItemGroups") or {}).get("nodes", [])) + 1
    mutation = f"""
    mutation($parentId: ID!) {{
        lineItemGroupCreate(parentId: $parentId, input: {{ position: {pos} }}) {{ id }}
    }}
    """
    result = query_printavo(mutation, {"parentId": internal_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    g = result.get("lineItemGroupCreate") or {}
    if not g.get("id"):
        return f"Unexpected response — no group id. Raw: {result}"
    return f"Line item group created (position {pos}). Group ID: {g['id']}"


@mcp.tool()
def add_line_item(line_item_group_id: str, item_number: str, description: str = "",
                  color: str = "TBD", quantity: int = 0, price: float = 0.0) -> str:
    """Add a promo / quantity-only line item to a group. Sets the item number, color,
    description, a single 'One Size' quantity (no size breakdown), and the unit list price.
    line_item_group_id: from add_line_item_group. quantity: total units. price: unit list price."""
    mutation = """
    mutation($groupId: ID!, $itemNumber: String, $color: String, $description: String, $price: Float, $count: Int) {
        lineItemCreate(lineItemGroupId: $groupId, input: {
            itemNumber: $itemNumber, color: $color, description: $description,
            price: $price, position: 1, sizes: [{ size: size_other, count: $count }]
        }) { id price items itemNumber color }
    }
    """
    result = query_printavo(mutation, {
        "groupId": line_item_group_id,
        "itemNumber": item_number,
        "color": color or "TBD",
        "description": description or "",
        "price": float(price or 0),
        "count": int(quantity or 0),
    })
    if "error" in result:
        return f"API Error: {result['error']}"
    it = result.get("lineItemCreate") or {}
    if not it.get("id"):
        return f"Unexpected response — no line item id. Raw: {result}"
    return (f"Line item added! ID: {it['id']}\n"
            f"  #{it.get('itemNumber')} | {it.get('color')} | qty {it.get('items')} | ${it.get('price')}")


@mcp.tool()
def remove_credit_card_fee(visual_id: str) -> str:
    """Find and delete the auto-added Credit Card Fee on a quote/invoice (leaves other fees alone)."""
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err
    q = """
    query($id: ID!) {
        invoice(id: $id) { fees { nodes { id description amount } } }
        quote(id: $id)   { fees { nodes { id description amount } } }
    }
    """
    res = query_printavo(q, {"id": internal_id}, allow_partial=True)
    if "error" in res:
        return f"API Error: {res['error']}"
    node = res.get("invoice") or res.get("quote") or {}
    fees = (node.get("fees") or {}).get("nodes", [])
    cc = [f for f in fees if "credit card" in (f.get("description") or "").lower()]
    if not cc:
        return "No credit-card fee found — nothing to remove."
    deleted = 0
    for f in cc:
        r = query_printavo("mutation($id: ID!){ feeDelete(id: $id){ id } }", {"id": f["id"]})
        if "error" not in r:
            deleted += 1
    return f"Removed {deleted} credit-card fee(s)."


def _find_category_id(name):
    r = query_printavo("query { account { categories { nodes { id name } } } }")
    if "error" in r:
        return None
    nodes = (((r.get("account") or {}).get("categories") or {}).get("nodes")) or []
    for n in nodes:
        if (n.get("name") or "").strip().lower() == name.strip().lower():
            return n.get("id")
    for n in nodes:
        if name.strip().lower() in (n.get("name") or "").lower():
            return n.get("id")
    return None


def _find_delivery_method_id(name):
    r = query_printavo("query { account { deliveryMethods { nodes { id name } } } }")
    if "error" in r:
        return None
    nodes = (((r.get("account") or {}).get("deliveryMethods") or {}).get("nodes")) or []
    for n in nodes:
        if (n.get("name") or "").strip().lower() == name.strip().lower():
            return n.get("id")
    for n in nodes:
        if name.strip().lower() in (n.get("name") or "").lower():
            return n.get("id")
    return None


@mcp.tool()
def set_line_item_category(line_item_id: str, category_name: str = "Misc") -> str:
    """Set a line item's category (default 'Misc' — used for promo items)."""
    cat_id = _find_category_id(category_name)
    if not cat_id:
        return f"Category '{category_name}' not found on this account."
    fr = query_printavo("query($id: ID!){ lineItem(id: $id){ position } }", {"id": line_item_id})
    pos = ((fr.get("lineItem") or {}).get("position")) or 1
    m = """
    mutation($id: ID!, $catId: ID!, $pos: Int!) {
        lineItemUpdate(id: $id, input: { position: $pos, category: { id: $catId } }) { id category { id name } }
    }
    """
    r = query_printavo(m, {"id": line_item_id, "catId": cat_id, "pos": pos})
    if "error" in r:
        return f"API Error: {r['error']}"
    li = r.get("lineItemUpdate") or {}
    return f"Category set to {((li.get('category') or {}).get('name'))} on line item {line_item_id}."


@mcp.tool()
def set_delivery_method(visual_id: str, method_name: str = "UPS Ground") -> str:
    """Set the order's delivery method (default 'UPS Ground')."""
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    dm_id = _find_delivery_method_id(method_name)
    if not dm_id:
        return f"Delivery method '{method_name}' not found on this account."
    m = f"""
    mutation($id: ID!, $dmId: ID!) {{
        {order_type}Update(id: $id, input: {{ deliveryMethod: {{ id: $dmId }} }}) {{
            id deliveryMethod {{ id name }}
        }}
    }}
    """
    r = query_printavo(m, {"id": internal_id, "dmId": dm_id})
    if "error" in r:
        return f"API Error: {r['error']}"
    node = r.get(f"{order_type}Update") or {}
    return f"Delivery method set to {((node.get('deliveryMethod') or {}).get('name'))} on #{visual_id}."


@mcp.tool()
def refresh_invoice_pricing(visual_id: str) -> str:
    """
    Verify current pricing on an order.
    visual_id: order number shown in Printavo UI
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    q = f"""
    query($id: ID!) {{
        {order_type}(id: $id) {{ id visualId total }}
    }}
    """
    result = query_printavo(q, {"id": internal_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    inv = result.get(order_type, {})
    total = inv.get("total", "?")
    if not total or float(total or 0) == 0:
        return (
            f"Order #{inv.get('visualId')} total is $0 — pricing may not be set.\n"
            f"Verify imprint matrix columns are assigned via set_imprint_pricing."
        )
    return f"Order #{inv.get('visualId')} current total: ${total} ✓"


@mcp.tool()
def set_order_status(visual_id: str, status_name: str) -> str:
    """
    Set a Printavo order's status by name.
    visual_id: order number shown in Printavo UI
    status_name: exact status name — use get_statuses() to see all available names.
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    status_id, err = _get_status_id_by_name(status_name)
    if err:
        return err
    mutation = """
    mutation($parentId: ID!, $statusId: ID!) {
        statusUpdate(parentId: $parentId, statusId: $statusId) {
            ... on Quote   { id visualId status { name } }
            ... on Invoice { id visualId status { name } }
        }
    }
    """
    result = query_printavo(mutation, {"parentId": internal_id, "statusId": status_id})
    if "error" in result:
        return f"API Error: {result['error']}"
    inv = result.get("statusUpdate", {})
    new_status = (inv.get("status") or {}).get("name", "?")
    return f"Order #{inv.get('visualId')} status → {new_status}"


@mcp.tool()
def upload_production_file(visual_id: str, file_path: str) -> str:
    """
    Upload a file to a Printavo order as a production file.
    visual_id: order number shown in Printavo UI
    file_path: https:// URL to the file
    """
    internal_id, err = _find_invoice_internal_id(visual_id)
    if err:
        return err
    if not file_path.startswith("http"):
        return "file_path must be an https:// URL."
    mutation = """
    mutation($parentId: ID!, $url: String!) {
        productionFileCreate(parentId: $parentId, publicFileUrl: $url) {
            id name
        }
    }
    """
    result = query_printavo(mutation, {"parentId": internal_id, "url": file_path})
    if "error" in result:
        return f"API Error: {result['error']}"
    pf = result.get("productionFileCreate", {})
    filename = file_path.split("/")[-1].split("?")[0]
    return f"Production file uploaded: {pf.get('name', filename)} (ID: {pf.get('id')}) → Order #{visual_id}"


@mcp.tool()
def attach_mockup_to_order(visual_id: str, file_path: str, line_item_id: str = "") -> str:
    """
    Attach an image (or PDF) mockup to a line item by public URL — Printavo fetches it server-side.
    visual_id: order number shown in Printavo UI
    file_path: https:// URL to the image/PDF (e.g. a SAGE product image)
    line_item_id: optional — attach to THIS specific line item; if omitted, uses the first line item.
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    if not file_path.startswith("http"):
        return "file_path must be an https:// URL."
    if not line_item_id:
        q = f"""
        query($id: ID!) {{
            {order_type}(id: $id) {{
                lineItemGroups {{ nodes {{ lineItems {{ nodes {{ id }} }} }} }}
            }}
        }}
        """
        result = query_printavo(q, {"id": internal_id})
        if "error" in result:
            return f"API Error: {result['error']}"
        groups = (result.get(order_type) or {}).get("lineItemGroups", {}).get("nodes", [])
        if not groups:
            return f"No line item groups found on order #{visual_id}."
        items = (groups[0].get("lineItems") or {}).get("nodes", [])
        if not items:
            return f"No line items found in first group of order #{visual_id}."
        line_item_id = items[0]["id"]
    mutation = """
    mutation($lineItemId: ID!, $url: String!) {
        lineItemMockupCreate(lineItemId: $lineItemId, publicImageUrl: $url) {
            id fullImageUrl
        }
    }
    """
    upload_result = query_printavo(mutation, {"lineItemId": line_item_id, "url": file_path})
    if "error" in upload_result:
        return f"Mockup upload failed: {upload_result['error']}"
    mockup = upload_result.get("lineItemMockupCreate", {})
    filename = file_path.split("/")[-1].split("?")[0]
    return f"Mockup attached: {filename} → Order #{visual_id} (Mockup ID: {mockup.get('id')})"


@mcp.tool()
def list_available_mutations() -> str:
    """List all GraphQL mutations available in the Printavo API."""
    q = """
    query {
        __schema {
            mutationType { fields { name } }
        }
    }
    """
    result = query_printavo(q)
    if "error" in result:
        return f"API Error: {result['error']}"
    mt = result.get("__schema", {}).get("mutationType", {})
    if not mt:
        return "Could not retrieve mutation list."
    fields = sorted(f.get("name") for f in mt.get("fields", []))
    return "AVAILABLE MUTATIONS:\n" + "\n".join(f"  {n}" for n in fields)


# ── DAILY CX DIGEST ───────────────────────────────────────────────────────────
# Weekday 8:00 AM America/Chicago Slack digest with five sections:
#   Z1 WELLNESS CALLS   — entered "Quote Approved" the previous business day
#                         (contract orders excluded — no wellness call)
#   Z2 LINGERING PICKUPS — in "Ready for Pickup" > 2 days
#   Z3 QUOTE FOLLOW-UPS  — QUOTE APPROVAL SENT > 3 days (idle >45d auto-flipped
#                          back to Quote so dead quotes stop triggering)
#   Z5 ART FOLLOW-UPS    — art/proof/mock-up/sew-out/digitizing statuses > 3 days
#   Z4 BLOCKED           — waiting-on-goods/artwork/promo statuses > 5 days
# The >14d backlog for pickups (Z2) and quotes (Z3) is pulled OUT of #cx-daily
# into Luke's private backlog block (CX_BACKLOG_SLACK_WEBHOOK_URL).
#
# STATUS-AGE SOURCE: the schema has NO status-change timestamp on Invoice
# (Invoice.timestamps is only createdAt/updatedAt, and updatedAt bumps on ANY
# edit) — so days-in-status comes from a snapshot store persisted to JSON:
#   {order_id: {"status": name, "first_seen": iso, "z1_reported": bool}}
# Every run (scheduled or manual) does a full paginated scan of tracked
# statuses and updates the snapshot. Day counts start from the first run that
# saw the order in its current status — Z2–Z4 will fill in as the snapshot
# ages after first deploy.

# (zid, header, intro line, STATUS_NAMES keys, min days-in-status or None for Z1)
# Copy approved by Luke 2026-07-19 — edit wording here only.
# Z2–Z4 line-item age cap (days). Orders older than this are NOT listed
# individually — they're rolled into a one-line count per section, keeping the
# digest actionable and under Slack's message length limit. Z1 is unaffected
# (it's window-based). Override with env CX_MAX_AGE_DAYS.
CX_MAX_AGE_DAYS = float(os.environ.get("CX_MAX_AGE_DAYS", "14"))

# QUOTE APPROVAL SENT quotes older than this (days) are auto-flipped back to
# "Quote" so dead quotes stop triggering follow-ups. 0 disables the flip.
# Age is a conservative LOWER bound (see _update_snapshot seeding), so a quote
# is only flipped when it is genuinely older than this. Override with env.
CX_QUOTE_STALE_FLIP_DAYS = float(os.environ.get("CX_QUOTE_STALE_FLIP_DAYS", "45"))
# Safety cap on how many quotes a single run will auto-flip.
CX_MAX_FLIPS_PER_RUN = int(os.environ.get("CX_MAX_FLIPS_PER_RUN", "100"))
# RECEIVABLES section: orders in INVOICED with an unpaid balance, pinged once
# they are overdue by more than the grace period. Due date is derived per
# invoice from its own payment terms (paymentTerm.days) — Net30 accounts pop at
# ~day 33, Due-on-Receipt / Pre-Pay (0 or blank terms) at ~day 3 — so no
# customer names are hardcoded and it stays correct as terms change.
CX_RECEIVABLES_GRACE_DAYS = float(os.environ.get("CX_RECEIVABLES_GRACE_DAYS", "3"))
# Most-overdue-first; show this many individually, roll the rest into a count.
CX_RECEIVABLES_MAX_LINES = int(os.environ.get("CX_RECEIVABLES_MAX_LINES", "12"))

# Master on-switch for the actual flip mutation. Luke reviewed the candidate
# list (all 700-1,800-day-old dead quotes) and approved the flip 2026-07-29,
# so this defaults ON. Set CX_FLIP_ENABLED=false to pause it.
CX_FLIP_ENABLED = os.environ.get("CX_FLIP_ENABLED", "true").strip().lower() in (
    "1", "true", "yes")

# Each section is (zid, header, intro, STATUS_NAMES keys, min days-in-status
# or None for Z1, opts). opts controls how the >CX_MAX_AGE_DAYS backlog is
# handled and Z1 filtering:
#   "stale": "slack"  → roll older items into a one-line count in the CX post
#            "backlog" → pull older items OUT of the CX post into Luke's
#                        itemized backlog block (not the CX channel)
#   "drop_contract": True → omit orders whose line-item category contains
#                           "contract" (Z1 only)
#   "flip_stale_quotes": True → auto-flip QUOTE APPROVAL SENT older than
#                               CX_QUOTE_STALE_FLIP_DAYS back to "Quote"
# Copy approved by Luke 2026-07-19; section split approved 2026-07-29.
DIGEST_SECTIONS = [
    ("Z1", "📞 Wellness Calls",
     "these customers approved their quote yesterday. A 2-minute \"got it, "
     "here's what happens next\" call today buys a lot of goodwill later.",
     ["QUOTE_APPROVED"], None, {"drop_contract": True}),
    ("Z2", "📦 Lingering Pickups",
     "ready and waiting on the shelf more than 2 days. Nudge them — done work "
     "sitting here is cash we haven't collected and space we don't have.",
     ["READY_FOR_PICKUP"], 2, {"stale": "backlog"}),
    ("Z3", "✏️ Quote Follow-Ups",
     "quote approval sent, waiting on the customer to say yes since at least "
     "yesterday. One friendly bump usually shakes these loose.",
     ["QUOTE_APPROVAL_SENT"], 1,
     {"stale": "keep"}),
    ("Z5", "🎨 Art Follow-Ups",
     "waiting on the customer to approve art, a proof, a mock-up, or a sew-out "
     "since at least yesterday — plus jobs sitting in digitizing/transfer. "
     "Nudge or move them.",
     ["ART_APPROVAL_SENT", "MOCKUP_REQUESTED", "MOCKUP_READY",
      "PROOF_REQUESTED", "EMB_SEW_OUT_APPROVAL_SENT", "PROMO_ART_APPROVAL_SENT",
      "EMB_ORDER_DIGITIZING", "DTF_ORDER_TRANSFER"], 1, {"stale": "keep"}),
    ("Z4", "🚧 Blocked",
     "stuck 5+ days waiting on artwork, goods, or promo stock. "
     "These don't fix themselves — each one needs a chase or a decision today.",
     ["CONTRACT_WAITING_ARTWORK", "CONTRACT_WAITING_GOODS",
      "PROMO_ON_ORDER"], 5, {"stale": "slack"}),
]


def _central_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        # Fallback if tzdata is unavailable: fixed CST (no DST)
        return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-6)))


def _snapshot_path() -> str:
    p = os.environ.get("STATUS_SNAPSHOT_PATH", "")
    if p:
        return p
    if os.path.isdir("/data"):  # Railway volume default mount
        return "/data/status_snapshot.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "status_snapshot.json")


def _load_snapshot() -> dict:
    try:
        with open(_snapshot_path(), "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_snapshot(snap: dict):
    path = _snapshot_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, path)
    except Exception:
        pass  # snapshot persistence is best-effort; never kill a digest run


def _tracked_status_keys() -> list:
    keys = []
    for _z, _header, _intro, section_keys, _days, _opts in DIGEST_SECTIONS:
        keys.extend(section_keys)
    return keys


def _fetch_tracked_orders() -> dict:
    """
    Full paginated scan of every order (invoices AND quotes) currently in a
    digest-tracked status. Tries the server-side statusIds filter first;
    falls back to an unfiltered walk with client-side filtering.
    Returns {"orders": [...], "missing_statuses": [...]} or {"error": ...}.
    """
    sres = fetch_all_statuses()
    if "error" in sres:
        return {"error": f"Could not fetch statuses: {sres['error']}"}
    live_by_norm = {_norm_status(s["name"]): s["id"] for s in sres["nodes"]}

    tracked_norms, status_ids, missing = set(), [], []
    for key in _tracked_status_keys():
        norm = _norm_status(STATUS_NAMES[key])
        tracked_norms.add(norm)
        if norm in live_by_norm:
            status_ids.append(live_by_norm[norm])
        else:
            missing.append(STATUS_NAMES[key])

    frag = """
            totalNodes
            pageInfo { hasNextPage endCursor }
            nodes {
                id visualId nickname total
                status { name }
                timestamps { updatedAt }
                contact { fullName customer { companyName } }
            }
    """
    orders, seen = [], set()
    for field in ("invoices", "quotes"):
        q_filtered = f"""
        query($first: Int, $after: String, $statusIds: [ID!]) {{
            {field}(first: $first, after: $after, statusIds: $statusIds) {{ {frag} }}
        }}
        """
        r = paginate(q_filtered, {"statusIds": status_ids},
                     connection_path=(field,))
        if "error" in r:
            # statusIds arg rejected or filter failed → unfiltered walk
            q_all = f"""
            query($first: Int, $after: String) {{
                {field}(first: $first, after: $after) {{ {frag} }}
            }}
            """
            r = paginate(q_all, connection_path=(field,))
            if "error" in r:
                return {"error": f"{field} scan failed: {r['error']}"}
        for n in r["nodes"]:
            status_name = (n.get("status") or {}).get("name", "")
            if _norm_status(status_name) not in tracked_norms:
                continue
            oid = str(n.get("id"))
            if oid in seen:
                continue
            seen.add(oid)
            contact = n.get("contact") or {}
            company = ((contact.get("customer") or {}).get("companyName")
                       or contact.get("fullName") or "—")
            orders.append({
                "id": oid,
                "visualId": n.get("visualId"),
                "nickname": n.get("nickname") or "",
                "total": float(n.get("total") or 0),
                "status": status_name,
                "company": company,
                "updatedAt": (n.get("timestamps") or {}).get("updatedAt"),
                "kind": field,  # "invoices" | "quotes"
            })
    return {"orders": orders, "missing_statuses": missing}


def _update_snapshot(orders: list, now_utc: datetime):
    """Persist {id: {status, first_seen}}; reset first_seen on any status
    change; prune orders no longer in a tracked status. The reserved "_z1"
    key (reported quote approvals) is preserved across prunes.

    AGE SEEDING: when an order is seen for the FIRST time, first_seen is
    seeded from the order's timestamps.updatedAt instead of "now" — orders
    that were already stuck in a status before the snapshot store existed get
    a realistic age immediately instead of starting at 0. (updatedAt bumps on
    any edit, so this is a conservative LOWER bound on true status age.)"""
    snap = _load_snapshot()
    new_snap = {}
    if isinstance(snap.get("_z1"), dict):
        # Keep only recent Z1-reported markers (14 days)
        cutoff = (now_utc - timedelta(days=14)).isoformat()
        new_snap["_z1"] = {k: v for k, v in snap["_z1"].items()
                           if str(v) >= cutoff}
    for o in orders:
        prev = snap.get(o["id"])
        upd = _parse_iso(o.get("updatedAt"))
        if prev and _norm_status(prev.get("status")) == _norm_status(o["status"]):
            entry = dict(prev)
            # One-time backdate for entries created during the cold start
            # (before seeding existed): pull first_seen back to updatedAt.
            if not entry.get("seeded"):
                fs = _parse_iso(entry.get("first_seen"))
                if upd and fs and upd < fs:
                    entry["first_seen"] = upd.isoformat()
                entry["seeded"] = True
        else:
            if prev is None and upd and upd < now_utc:
                first_seen = upd.isoformat()   # first sighting → seed from updatedAt
            else:
                first_seen = now_utc.isoformat()  # observed status CHANGE → now
            entry = {"status": o["status"], "first_seen": first_seen,
                     "seeded": True}
        new_snap[o["id"]] = entry
    _save_snapshot(new_snap)
    return new_snap


def _digest_line(o: dict, days: float) -> str:
    path = "invoices" if o["kind"] == "invoices" else "quotes"
    url = f"https://www.printavo.com/{path}/{o['id']}"
    nickname = o["nickname"] or "(no nickname)"
    return (f"• <{url}|#{o['visualId']}> — {o['company']} — {nickname} — "
            f"{int(days)}d — ${o['total']:,.2f}")


# Z1 QUOTE-APPROVAL DETECTION
# "Quote Approved" is a transient status — the team moves orders into a lane
# minutes after approval, so a status snapshot at 8 AM misses nearly all of
# them. Instead, Z1 reads the DURABLE record: ApprovalRequest.response.
# respondedAt survives any status change.
Z1_APPROVAL_NAME_MATCH = "quote"  # case-insensitive filter on ApprovalRequest.name
                                  # (excludes art approvals); unnamed requests are
                                  # included. Calibrate with inspect_approvals().


def _z1_window_start(now_ct):
    """Start of the previous business day, 00:00 America/Chicago, as UTC.
    Monday's window starts Friday 00:00 → Fri/Sat/Sun roll into Monday."""
    prev = now_ct.date() - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return datetime(prev.year, prev.month, prev.day,
                    tzinfo=now_ct.tzinfo).astimezone(timezone.utc)


_APPROVAL_FRAG = """
        totalNodes
        pageInfo { hasNextPage endCursor }
        nodes {
            id visualId nickname total
            status { name }
            contact { fullName customer { companyName } }
            approvalRequests(first: 5) {
                nodes { name status response { name respondedAt } }
            }
        }
"""


def _scan_recent_approvals(window_start_utc, now_utc, max_scan=150):
    """
    Find orders whose QUOTE approval response landed inside the window,
    regardless of what status they've been moved to since. Scans two
    complementary slices per connection and unions them:
      A) newest-first (sortDescending) — catches brand-new orders
      B) production date >= 14 days ago — catches anything scheduled
    Returns {"hits": [...], "errors": [...]}.
    """
    slices = []
    for field in ("invoices", "quotes"):
        slices.append((field, f"""
        query($first: Int, $after: String) {{
            {field}(first: $first, after: $after, sortDescending: true) {{ {_APPROVAL_FRAG} }}
        }}
        """, None))
    prod_after = (now_utc - timedelta(days=14)).strftime("%Y-%m-%dT00:00:00Z")
    for field in ("invoices", "quotes"):
        slices.append((field, f"""
        query($first: Int, $after: String, $prodAfter: ISO8601DateTime) {{
            {field}(first: $first, after: $after, inProductionAfter: $prodAfter) {{ {_APPROVAL_FRAG} }}
        }}
        """, {"prodAfter": prod_after}))

    hits, errors = {}, []
    for field, q, extra_vars in slices:
        r = paginate(q, extra_vars, connection_path=(field,), max_records=max_scan)
        if "error" in r:
            errors.append(f"{field}: {str(r['error'])[:150]}")
            continue
        for n in r["nodes"]:
            best = None
            for ar in ((n.get("approvalRequests") or {}).get("nodes") or []):
                name = (ar.get("name") or "").lower()
                st = str(ar.get("status") or "").lower()
                resp = ar.get("response") or {}
                r_at = _parse_iso(resp.get("respondedAt"))
                if r_at is None:
                    continue                      # never responded
                if name and Z1_APPROVAL_NAME_MATCH not in name:
                    continue                      # e.g. art approval
                if "declin" in st or "retract" in st or "cancel" in st:
                    continue
                if window_start_utc <= r_at <= now_utc:
                    if best is None or r_at > best[0]:
                        best = (r_at, resp.get("name") or "")
            if best is None:
                continue
            oid = str(n.get("id"))
            if oid in hits:
                continue
            contact = n.get("contact") or {}
            company = ((contact.get("customer") or {}).get("companyName")
                       or contact.get("fullName") or "—")
            hits[oid] = {
                "id": oid,
                "visualId": n.get("visualId"),
                "nickname": n.get("nickname") or "",
                "total": float(n.get("total") or 0),
                "status": (n.get("status") or {}).get("name", "?"),
                "company": company,
                "kind": field,
                "approved_by": best[1],
                "responded_at": best[0].isoformat(),
            }
    return {"hits": list(hits.values()), "errors": errors}


def _fetch_receivables() -> dict:
    """Full paginated scan of invoices in INVOICED, with balance + terms.
    Returns {"orders": [...]} or {"error": ...}. Only invoices carry a
    balance/terms, so quotes are not scanned."""
    sres = fetch_all_statuses()
    if "error" in sres:
        return {"error": f"Could not fetch statuses: {sres['error']}"}
    live_by_norm = {_norm_status(s["name"]): s["id"] for s in sres["nodes"]}
    inv_norm = _norm_status(STATUS_NAMES["INVOICED"])
    status_id = live_by_norm.get(inv_norm)

    frag = """
            totalNodes
            pageInfo { hasNextPage endCursor }
            nodes {
                id visualId nickname total amountOutstanding paidInFull
                invoiceAt createdAt
                paymentTerm { days name }
                status { name }
                contact { fullName customer { companyName } }
            }
    """
    q_filtered = f"""
    query($first: Int, $after: String, $statusIds: [ID!]) {{
        invoices(first: $first, after: $after, statusIds: $statusIds) {{ {frag} }}
    }}
    """
    r = paginate(q_filtered, {"statusIds": [status_id] if status_id else []},
                 connection_path=("invoices",))
    if "error" in r:
        q_all = f"""
        query($first: Int, $after: String) {{
            invoices(first: $first, after: $after) {{ {frag} }}
        }}
        """
        r = paginate(q_all, connection_path=("invoices",))
        if "error" in r:
            return {"error": f"receivables scan failed: {r['error']}"}

    orders = []
    for n in r["nodes"]:
        if _norm_status((n.get("status") or {}).get("name", "")) != inv_norm:
            continue
        outstanding = float(n.get("amountOutstanding") or 0)
        if n.get("paidInFull") or outstanding <= 0.005:
            continue
        contact = n.get("contact") or {}
        company = ((contact.get("customer") or {}).get("companyName")
                   or contact.get("fullName") or "—")
        term = n.get("paymentTerm") or {}
        orders.append({
            "id": str(n.get("id")),
            "visualId": n.get("visualId"),
            "nickname": n.get("nickname") or "",
            "outstanding": outstanding,
            "invoiceAt": n.get("invoiceAt") or n.get("createdAt"),
            "term_days": int(term.get("days") or 0),
            "term_name": term.get("name") or "Pre-Pay",
            "company": company,
        })
    return {"orders": orders}


def _receivables_lines(now_utc: datetime) -> list:
    """Build the Receivables section: INVOICED + unpaid, past due by more than
    the grace period, most-overdue-first, capped with an overflow roll-up."""
    header = "💵 Receivables"
    intro = ("invoiced and still unpaid, past due on the customer's own terms "
             "(Net 30 accounts show at ~day 33; Pre-Pay / Due-on-Receipt at "
             "~day 3). Chase the money.")
    out = [f"\n*{header}* — _{intro}_"]

    fetched = _fetch_receivables()
    if "error" in fetched:
        out.append(f"⚠️ receivables scan failed: {fetched['error']}")
        return out

    def overdue_days(o):
        inv = _parse_iso(o.get("invoiceAt"))
        if inv is None:
            return 0.0
        due = inv + timedelta(days=o["term_days"])
        return (now_utc - due).total_seconds() / 86400.0

    due = [o for o in fetched["orders"]
           if overdue_days(o) > CX_RECEIVABLES_GRACE_DAYS]
    due.sort(key=overdue_days, reverse=True)

    if not due:
        out.append("— none —")
        return out

    shown = due[:CX_RECEIVABLES_MAX_LINES]
    hidden = due[CX_RECEIVABLES_MAX_LINES:]
    for o in shown:
        url = f"https://www.printavo.com/invoices/{o['id']}"
        nickname = o["nickname"] or "(no nickname)"
        out.append(
            f"• <{url}|#{o['visualId']}> — {o['company']} — {nickname} — "
            f"{int(overdue_days(o))}d overdue — ${o['outstanding']:,.2f} owed "
            f"({o['term_name']})")
    total_all = sum(o["outstanding"] for o in due)
    if hidden:
        hidden_val = sum(o["outstanding"] for o in hidden)
        out.append(f"…plus *{len(hidden)}* more unpaid (${hidden_val:,.0f}) — "
                   f"${total_all:,.0f} owed across all {len(due)}.")
    else:
        out.append(f"_Total outstanding: ${total_all:,.2f} across "
                   f"{len(due)} invoice(s)._")
    return out


def _flip_order_to_quote(internal_id: str, quote_status_id: str) -> bool:
    """Flip one order back to 'Quote'. Returns True on success."""
    mutation = """
    mutation($parentId: ID!, $statusId: ID!) {
        statusUpdate(parentId: $parentId, statusId: $statusId) {
            ... on Quote   { id visualId status { name } }
            ... on Invoice { id visualId status { name } }
        }
    }
    """
    result = query_printavo(mutation,
                            {"parentId": internal_id, "statusId": quote_status_id})
    if "error" in result:
        return False
    new_status = ((result.get("statusUpdate") or {}).get("status") or {}).get("name", "")
    return _norm_status(new_status) == _norm_status(STATUS_NAMES.get("QUOTE", "Quote"))


def _build_cx_digest(dry_run: bool = False):
    """Build the CX digest.
    Returns (slack_text, z1_marks_dict, backlog_text) or (error_text, None, "").

    slack_text  → the #cx-daily post.
    backlog_text→ Luke's private block (old pickups, old/flipped quotes); goes
                  to CX_BACKLOG_SLACK_WEBHOOK_URL, never #cx-daily.
    When dry_run is True, stale quotes are LISTED as flip candidates but NOT
    actually flipped."""
    now_utc = datetime.now(timezone.utc)
    now_ct = _central_now()

    fetched = _fetch_tracked_orders()
    if "error" in fetched:
        return f"CX DIGEST ERROR: {fetched['error']}", None, ""
    orders = fetched["orders"]
    snap = _update_snapshot(orders, now_utc)
    approvals = _scan_recent_approvals(_z1_window_start(now_ct), now_utc)

    def days_in_status(o):
        e = snap.get(o["id"]) or {}
        first_seen = _parse_iso(e.get("first_seen"))
        if first_seen is None:
            return 0.0
        return (now_utc - first_seen).total_seconds() / 86400.0

    try:
        date_str = now_ct.strftime("%A, %B %-d")   # "Monday, July 20" (Linux)
    except ValueError:
        date_str = now_ct.strftime("%A, %B %d")
    lines = [
        f"☕ *CX Daily — {date_str}*",
        "Six lists, one pass, before the phones start. "
        "Links go straight to the invoice.",
    ]
    backlog = []          # Luke's private block, assembled per-section
    z1_marks = {}
    for z, header, intro, section_keys, min_days, opts in DIGEST_SECTIONS:
        lines.append(f"\n*{header}* — _{intro}_")
        if min_days is None:
            # Z1: quote approvals RESPONDED in the window (durable signal),
            # deduped against previously reported approvals in snap["_z1"].
            reported = snap.get("_z1") if isinstance(snap.get("_z1"), dict) else {}
            hits = [h for h in approvals["hits"]
                    if str(reported.get(h["id"], "")) < h["responded_at"]]
            # Drop contract orders (Contract SP/EMB/DTF) — they don't get a
            # wellness call. Only the few Z1 hits are category-checked.
            if opts.get("drop_contract") and hits:
                cats = _fetch_categories_batch([h["id"] for h in hits])
                hits = [h for h in hits
                        if not _order_is_contract(cats.get(h["id"]))]
            z1_marks = {h["id"]: h["responded_at"] for h in hits}
            hits.sort(key=lambda x: x["responded_at"], reverse=True)
            if hits:
                for h in hits:
                    days = (now_utc - _parse_iso(h["responded_at"])).total_seconds() / 86400.0
                    line = _digest_line(h, days) + f" — now {h['status']}"
                    if h["approved_by"]:
                        line += f" (approved by {h['approved_by']})"
                    lines.append(line)
            else:
                lines.append("— none —")
            if approvals["errors"]:
                lines.append(f"⚠️ approval scan partial: {approvals['errors']}")
            continue

        norms = {_norm_status(STATUS_NAMES[k]) for k in section_keys}
        hits = [o for o in orders
                if _norm_status(o["status"]) in norms
                and days_in_status(o) > min_days]

        # Auto-flip stale QUOTE APPROVAL SENT quotes back to "Quote" so dead
        # quotes stop triggering. Age is a conservative lower bound, so a quote
        # is only flipped when it is genuinely older than the cutoff. Candidates
        # are pulled out of the section entirely (flipped or not) so they don't
        # double-list in the quote worklist below.
        flipped, flip_pending = [], []
        if opts.get("flip_stale_quotes") and CX_QUOTE_STALE_FLIP_DAYS > 0:
            candidates = sorted(
                (o for o in hits if days_in_status(o) > CX_QUOTE_STALE_FLIP_DAYS),
                key=days_in_status, reverse=True)[:CX_MAX_FLIPS_PER_RUN]
            cand_ids = {o["id"] for o in candidates}
            hits = [o for o in hits if o["id"] not in cand_ids]
            if candidates and not dry_run and CX_FLIP_ENABLED:
                qid, _qerr = _get_status_id_by_name(STATUS_NAMES.get("QUOTE", "Quote"))
                for o in candidates:
                    if qid and _flip_order_to_quote(o["id"], qid):
                        flipped.append(o)
                fids = {o["id"] for o in flipped}
                flip_pending = [o for o in candidates if o["id"] not in fids]
            else:
                # dry run OR flip switch off: list candidates, mutate nothing.
                flip_pending = candidates

        # Age cap: list only orders <= CX_MAX_AGE_DAYS individually — unless the
        # section is "keep", in which case every hit is listed in full for as
        # long as it stays in the status (no roll-off, no backlog, no flip).
        stale_mode = opts.get("stale", "slack")
        if stale_mode == "keep":
            fresh, stale = hits, []
        else:
            fresh = [o for o in hits if days_in_status(o) <= CX_MAX_AGE_DAYS]
            stale = [o for o in hits if days_in_status(o) > CX_MAX_AGE_DAYS]
        fresh.sort(key=days_in_status, reverse=True)
        if fresh:
            for o in fresh:
                lines.append(_digest_line(o, days_in_status(o)))
        elif not stale:
            lines.append("— none —")
        if stale and stale_mode == "slack":
            # Roll older items into a one-line count in the #cx-daily post.
            oldest = max(days_in_status(o) for o in stale)
            total_val = sum(float(o.get("total") or 0) for o in stale)
            lines.append(
                f"…plus *{len(stale)}* older than {int(CX_MAX_AGE_DAYS)}d "
                f"(oldest {int(oldest)}d, ${total_val:,.0f} quoted) — "
                f"backlog to work in Printavo, not today's list.")
        elif stale and stale_mode == "backlog":
            # Pull older items OUT of #cx-daily into Luke's private block,
            # itemized so he can restatus them and they fall off the pull.
            if not fresh:
                lines.append(f"…plus *{len(stale)}* older than "
                             f"{int(CX_MAX_AGE_DAYS)}d — on Luke's backlog list.")
            else:
                lines.append(f"…plus *{len(stale)}* older than "
                             f"{int(CX_MAX_AGE_DAYS)}d on Luke's backlog list.")
            stale.sort(key=days_in_status, reverse=True)
            total_val = sum(float(o.get("total") or 0) for o in stale)
            backlog.append(
                f"\n*{header} — backlog ({len(stale)}, ${total_val:,.0f})* "
                f"— older than {int(CX_MAX_AGE_DAYS)}d; restatus these so they "
                f"drop off tomorrow's pull:")
            for o in stale:
                backlog.append(_digest_line(o, days_in_status(o)))

        if flipped:
            flipped.sort(key=days_in_status, reverse=True)
            fval = sum(float(o.get("total") or 0) for o in flipped)
            backlog.append(
                f"\n*{header} — auto-flipped back to Quote "
                f"({len(flipped)}, ${fval:,.0f})* — were idle "
                f">{int(CX_QUOTE_STALE_FLIP_DAYS)}d in QUOTE APPROVAL SENT; "
                f"no longer triggering follow-ups:")
            for o in flipped:
                backlog.append(_digest_line(o, days_in_status(o)))
        if flip_pending:
            flip_pending.sort(key=days_in_status, reverse=True)
            pval = sum(float(o.get("total") or 0) for o in flip_pending)
            note = ("would flip (dry run)" if dry_run
                    else "flip PENDING — set CX_FLIP_ENABLED=true to auto-flip")
            backlog.append(
                f"\n*{header} — stale ≥{int(CX_QUOTE_STALE_FLIP_DAYS)}d, "
                f"{note} ({len(flip_pending)}, ${pval:,.0f})* — idle in QUOTE "
                f"APPROVAL SENT; flip to Quote so they stop triggering:")
            for o in flip_pending:
                backlog.append(_digest_line(o, days_in_status(o)))


    if fetched["missing_statuses"]:
        lines.append("\n⚠️ Status names in STATUS_NAMES not found in Printavo "
                     f"(renamed?): {fetched['missing_statuses']}")

    backlog_text = ""
    if backlog:
        head = (f"📋 *CX Backlog — {date_str}* (just for you — not in #cx-daily)")
        backlog_text = head + "\n" + "\n".join(backlog)
    return "\n".join(lines), z1_marks, backlog_text


def _post_to_slack(text: str, webhook_url: str):
    """Post to whichever channel's webhook it is handed. The CX digest hands
    this CX_SLACK_WEBHOOK_URL and NEVER SLACK_WEBHOOK_URL (production channel)."""
    if not webhook_url:
        return "Error: target Slack webhook env var not set."
    resp = httpx.post(webhook_url, json={"text": text}, timeout=15)
    if resp.status_code == 200:
        return None
    return f"Slack error: HTTP {resp.status_code} — {resp.text[:200]}"


def _mark_z1_reported(marks: dict):
    """Record which quote approvals have been reported: {order_id: responded_at}."""
    if not marks:
        return
    snap = _load_snapshot()
    z1 = snap.get("_z1")
    if not isinstance(z1, dict):
        z1 = {}
    z1.update(marks)
    snap["_z1"] = z1
    _save_snapshot(snap)


def _run_cx_digest_impl(force_dry_run: bool = False) -> str:
    is_dry = force_dry_run or _dry_run()
    text, z1_ids, backlog = _build_cx_digest(dry_run=is_dry)
    if z1_ids is None:
        return text  # error
    backlog_block = f"\n\n{backlog}" if backlog else ""
    if is_dry:
        print(f"[CX DIGEST — DRY RUN]\n{text}\n{backlog}", flush=True)
        return f"[DRY RUN — not posted to Slack]\n\n{text}{backlog_block}"
    if not CX_SLACK_WEBHOOK_URL:
        # Hard rule: CX content NEVER goes to SLACK_WEBHOOK_URL. No fallback.
        print(f"[CX DIGEST] CX_SLACK_WEBHOOK_URL not set — NOT posting.\n{text}",
              flush=True)
        return ("Error: CX_SLACK_WEBHOOK_URL not set. CX digest does not fall "
                f"back to the production webhook.\n\n{text}{backlog_block}")
    err = _post_to_slack(text, CX_SLACK_WEBHOOK_URL)
    if err:
        return f"{err}\n\nDigest that failed to post:\n{text}{backlog_block}"
    _mark_z1_reported(z1_ids)
    # Luke's backlog block → private webhook only. Never #cx-daily. If the
    # webhook isn't configured it still comes back in this return text.
    backlog_status = ""
    if backlog:
        if CX_BACKLOG_SLACK_WEBHOOK_URL:
            berr = _post_to_slack(backlog, CX_BACKLOG_SLACK_WEBHOOK_URL)
            backlog_status = ("\n(backlog block sent to your private channel ✓)"
                              if not berr else f"\n(backlog post failed: {berr})")
        else:
            backlog_status = ("\n(CX_BACKLOG_SLACK_WEBHOOK_URL not set — backlog "
                              "block shown below only, not sent anywhere)")
    return f"Posted to #cx-daily ✓{backlog_status}\n\n{text}{backlog_block}"


@mcp.tool()
def run_cx_digest(dry_run: bool = False) -> str:
    """
    Build and post the daily CX digest right now, without waiting for the 8 AM
    schedule. Five sections: Wellness Calls (contract orders excluded),
    Lingering Pickups, Quote Follow-Ups, Art Follow-Ups, Blocked. Old pickups
    and old quotes go to Luke's private backlog block (not #cx-daily); quotes
    idle >45d in QUOTE APPROVAL SENT are auto-flipped back to Quote.
    dry_run=True (or env DRY_RUN=true) returns the text and only LISTS the
    flip candidates instead of posting to Slack or flipping anything.
    """
    return _run_cx_digest_impl(force_dry_run=dry_run)


@mcp.tool()
def inspect_approvals(visual_id: str) -> str:
    """
    Show the raw approval requests + responses on an order — used to verify
    and calibrate the CX digest's Z1 quote-approval detection
    (Z1_APPROVAL_NAME_MATCH filter).
    """
    internal_id, order_type, err = _find_order(visual_id)
    if err:
        return err
    q = f"""
    query($id: ID!) {{
        {order_type}(id: $id) {{
            visualId
            status {{ name }}
            approvalRequests(first: 20) {{
                nodes {{
                    id name status
                    timestamps {{ createdAt updatedAt }}
                    response {{ name email respondedAt reason }}
                }}
            }}
        }}
    }}
    """
    r = query_printavo(q, {"id": internal_id})
    if "error" in r:
        return f"API Error: {r['error']}"
    obj = r.get(order_type) or {}
    ars = (obj.get("approvalRequests") or {}).get("nodes", [])
    lines = [f"APPROVAL REQUESTS — Order #{obj.get('visualId')} "
             f"(status: {(obj.get('status') or {}).get('name', '?')}): {len(ars)}"]
    for ar in ars:
        resp = ar.get("response") or {}
        ts = ar.get("timestamps") or {}
        lines.append(
            f"  • name={ar.get('name')!r} | status={ar.get('status')!r} | "
            f"created={ts.get('createdAt')} | responded={resp.get('respondedAt')} | "
            f"by={resp.get('name')} <{resp.get('email', '')}> | "
            f"reason={resp.get('reason')!r}")
    if not ars:
        lines.append("  (none)")
    return "\n".join(lines)


@mcp.tool()
def printavo_health_check(full_invoice_walk: bool = False) -> str:
    """
    Prove the 25-record cap is beaten: walks the complete status list and
    reports true status + invoice counts (API totalNodes vs records actually
    retrieved). full_invoice_walk=True walks every invoice page (slow).
    """
    s = fetch_all_statuses()
    if "error" in s:
        return f"Health check FAILED on statuses: {s['error']}"
    inv_q = """
    query($first: Int, $after: String) {
        invoices(first: $first, after: $after) {
            totalNodes
            pageInfo { hasNextPage endCursor }
            nodes { id }
        }
    }
    """
    inv = paginate(inv_q, max_records=None if full_invoice_walk else 60)
    if "error" in inv:
        return f"Health check FAILED on invoices: {inv['error']}"
    cap_beaten = len(s["nodes"]) > 25 or len(inv["nodes"]) > 25
    return "\n".join([
        "PRINTAVO PAGINATOR HEALTH CHECK",
        f"  Statuses: {len(s['nodes'])} retrieved / {s.get('totalNodes')} total "
        f"(pages: {s['pages']}, throttle retries: {s['throttled']})",
        f"  Invoices: {len(inv['nodes'])} retrieved / {inv.get('totalNodes')} total "
        f"(pages: {inv['pages']}, throttle retries: {inv['throttled']}"
        f"{'' if full_invoice_walk else ', capped at 60 for speed — pass full_invoice_walk=true for the full walk'})",
        f"  Snapshot store: {_snapshot_path()} "
        f"({len(_load_snapshot())} orders tracked)",
        f"  DRY_RUN: {_dry_run()}",
        f"  25-cap beaten: {'YES ✓' if cap_beaten else 'NOT PROVEN — fewer than 26 records exist or walk failed'}",
    ])


_last_digest_date = None

def run_cx_digest_scheduler():
    """Background thread: post the CX digest at 8:00 AM America/Chicago,
    weekdays, skipping federal holidays. DST-correct via zoneinfo."""
    global _last_digest_date
    while True:
        try:
            now_ct = _central_now()
            if (now_ct.weekday() < 5
                    and not _is_us_federal_holiday(now_ct)
                    and now_ct.hour == 7 and 40 <= now_ct.minute < 50
                    and _last_digest_date != now_ct.date().isoformat()):
                _last_digest_date = now_ct.date().isoformat()
                _run_cx_digest_impl()
        except Exception as e:
            print(f"[CX DIGEST] scheduler error: {e}", flush=True)
        time.sleep(120)


# ── ART QUEUE DIGEST (Richie) ──────────────────────────────────────────────────
# A Richie-facing twin of the CX digest, scoped to the five art-queue statuses.
# Reports HOURS in status (not days) against a 24h SLA. Uses its OWN snapshot
# store so its clocks are independent of the CX digest's daily prune.
#
# STATUS-AGE SOURCE: same constraint as the CX digest — Printavo exposes no
# status-change timestamp, so hours-in-status come from a snapshot seeded from
# updatedAt on first sighting (a conservative lower bound) and reset to "now" the
# moment an order changes status. Exact from the run after first deploy.

# Fixed section order. Each is (header_label, [STATUS_NAMES keys]).
ART_SECTIONS = [
    ("PROOF REQUESTED",        ["PROOF_REQUESTED"]),
    ("MOCK-UP REQUESTED",      ["MOCKUP_REQUESTED"]),
    ("ARTWORK DECLINED",       ["ARTWORK_DECLINED"]),
    ("EMB – ORDER DIGITIZING", ["EMB_ORDER_DIGITIZING"]),
    ("DTF – ORDER TRANSFER",   ["DTF_ORDER_TRANSFER"]),
]


def _art_status_keys() -> list:
    keys = []
    for _label, section_keys in ART_SECTIONS:
        keys.extend(section_keys)
    return keys


def _art_snapshot_path() -> str:
    if ART_SNAPSHOT_PATH:
        return ART_SNAPSHOT_PATH
    if os.path.isdir("/data"):  # Railway volume default mount
        return "/data/art_status_snapshot.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "art_status_snapshot.json")


def _load_art_snapshot() -> dict:
    try:
        with open(_art_snapshot_path(), "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_art_snapshot(snap: dict):
    path = _art_snapshot_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, path)
    except Exception:
        pass  # best-effort; never kill a digest run


def _fetch_art_orders() -> dict:
    """Full paginated scan of every invoice AND quote currently in one of the
    five art-queue statuses. Tries the server-side statusIds filter first, falls
    back to an unfiltered walk with client-side filtering. Mirrors
    _fetch_tracked_orders. Returns {"orders": [...], "missing_statuses": [...]}
    or {"error": ...}."""
    sres = fetch_all_statuses()
    if "error" in sres:
        return {"error": f"Could not fetch statuses: {sres['error']}"}
    live_by_norm = {_norm_status(s["name"]): s["id"] for s in sres["nodes"]}

    tracked_norms, status_ids, missing = set(), [], []
    for key in _art_status_keys():
        norm = _norm_status(STATUS_NAMES[key])
        tracked_norms.add(norm)
        if norm in live_by_norm:
            status_ids.append(live_by_norm[norm])
        else:
            missing.append(STATUS_NAMES[key])

    frag = """
            totalNodes
            pageInfo { hasNextPage endCursor }
            nodes {
                id visualId nickname
                status { name }
                timestamps { updatedAt }
                contact { fullName customer { companyName } }
            }
    """
    orders, seen = [], set()
    for field in ("invoices", "quotes"):
        q_filtered = f"""
        query($first: Int, $after: String, $statusIds: [ID!]) {{
            {field}(first: $first, after: $after, statusIds: $statusIds) {{ {frag} }}
        }}
        """
        r = paginate(q_filtered, {"statusIds": status_ids},
                     connection_path=(field,))
        if "error" in r:
            q_all = f"""
            query($first: Int, $after: String) {{
                {field}(first: $first, after: $after) {{ {frag} }}
            }}
            """
            r = paginate(q_all, connection_path=(field,))
            if "error" in r:
                return {"error": f"{field} scan failed: {r['error']}"}
        for n in r["nodes"]:
            status_name = (n.get("status") or {}).get("name", "")
            if _norm_status(status_name) not in tracked_norms:
                continue
            oid = str(n.get("id"))
            if oid in seen:
                continue
            seen.add(oid)
            contact = n.get("contact") or {}
            company = ((contact.get("customer") or {}).get("companyName")
                       or contact.get("fullName") or "—")
            orders.append({
                "id": oid,
                "visualId": n.get("visualId"),
                "nickname": n.get("nickname") or "",
                "status": status_name,
                "company": company,
                "updatedAt": (n.get("timestamps") or {}).get("updatedAt"),
                "kind": field,
            })
    return {"orders": orders, "missing_statuses": missing}


def _update_art_snapshot(orders: list, now_utc: datetime) -> dict:
    """Persist {id: {status, first_seen}} for the art queue; reset first_seen on
    a status change; prune orders no longer in an art status. first_seen is
    seeded from updatedAt on first sighting (lower bound) so existing orders show
    a realistic age on day one instead of 0h."""
    snap = _load_art_snapshot()
    new_snap = {}
    for o in orders:
        prev = snap.get(o["id"])
        upd = _parse_iso(o.get("updatedAt"))
        if prev and _norm_status(prev.get("status")) == _norm_status(o["status"]):
            entry = dict(prev)
        else:
            if prev is None and upd and upd < now_utc:
                first_seen = upd.isoformat()      # first sighting → seed from updatedAt
            else:
                first_seen = now_utc.isoformat()  # observed status CHANGE → now
            entry = {"status": o["status"], "first_seen": first_seen}
        new_snap[o["id"]] = entry
    _save_art_snapshot(new_snap)
    return new_snap


def _fmt_hours(hours: float) -> str:
    if hours < 1:
        return "<1h"
    return f"{int(hours)}h"


def _build_art_digest():
    """Build the art-queue digest text. Returns (text, ok) — ok False on error.
    Always updates the art snapshot (starts/continues the hour clocks)."""
    now_utc = datetime.now(timezone.utc)
    now_ct = _central_now()
    fetched = _fetch_art_orders()
    if "error" in fetched:
        return f"ART QUEUE ERROR: {fetched['error']}", False
    orders = fetched["orders"]
    snap = _update_art_snapshot(orders, now_utc)

    def hours_in_status(o):
        e = snap.get(o["id"]) or {}
        fs = _parse_iso(e.get("first_seen"))
        if fs is None:
            return 0.0
        return (now_utc - fs).total_seconds() / 3600.0

    try:
        date_str = now_ct.strftime("%A, %B %-d")
    except ValueError:
        date_str = now_ct.strftime("%A, %B %d")

    lines = [
        f"\U0001F3A8 *Art Queue — {date_str}*",
        f"Everything here targets a {int(ART_SLA_HOURS)}h turnaround. "
        f"\U0001F534 = past {int(ART_SLA_HOURS)}h.",
    ]
    total, past_sla = 0, 0
    for label, section_keys in ART_SECTIONS:
        norms = {_norm_status(STATUS_NAMES[k]) for k in section_keys}
        hits = [o for o in orders if _norm_status(o["status"]) in norms]
        hits.sort(key=hours_in_status, reverse=True)
        lines.append(f"\n*{label}* ({len(hits)})")
        for o in hits:
            hrs = hours_in_status(o)
            total += 1
            flag = ""
            if hrs > ART_SLA_HOURS:
                past_sla += 1
                flag = "\U0001F534 "
            path = "invoices" if o["kind"] == "invoices" else "quotes"
            url = f"https://www.printavo.com/{path}/{o['id']}"
            nickname = o["nickname"] or "(no nickname)"
            lines.append(f"{flag}<{url}|#{o['visualId']}> — {nickname} — "
                         f"{o['company']} — {_fmt_hours(hrs)}")

    lines.append(f"\n_Total in queue: {total}   ·   Past {int(ART_SLA_HOURS)}h "
                 f"SLA: {past_sla}_")
    if fetched["missing_statuses"]:
        lines.append("\n⚠️ Art status names not found in Printavo "
                     f"(renamed?): {fetched['missing_statuses']}")
    return "\n".join(lines), True


def _post_art_digest(text: str):
    """Deliver the art digest. Prefers a true DM via bot token; falls back to a
    channel webhook. Returns None on success, or an error string."""
    if SLACK_BOT_TOKEN:
        recipients = [r.strip() for r in ART_DM_USER_ID.split(",") if r.strip()]
        errors = []
        for uid in recipients:
            resp = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                         "Content-Type": "application/json; charset=utf-8"},
                json={"channel": uid, "text": text,
                      "unfurl_links": False, "unfurl_media": False},
                timeout=15)
            if resp.status_code != 200:
                errors.append(f"{uid}: HTTP {resp.status_code} — {resp.text[:150]}")
                continue
            body = resp.json()
            if not body.get("ok"):
                errors.append(f"{uid}: {body.get('error')}")
        return None if not errors else "; ".join(errors)
    if ART_SLACK_WEBHOOK_URL:
        return _post_to_slack(text, ART_SLACK_WEBHOOK_URL)
    return ("Error: no art-digest delivery configured (set SLACK_BOT_TOKEN for a "
            "DM to Richie, or ART_SLACK_WEBHOOK_URL for a channel).")


def _run_art_digest_impl(force_dry_run: bool = False) -> str:
    is_dry = force_dry_run or _dry_run()
    text, ok = _build_art_digest()
    if not ok:
        return text  # error
    if is_dry:
        print(f"[ART QUEUE — DRY RUN]\n{text}", flush=True)
        return f"[DRY RUN — not posted]\n\n{text}"
    if not ART_DIGEST_ENABLED:
        print("[ART QUEUE] ART_DIGEST_ENABLED not set — built but NOT posting.",
              flush=True)
        return f"[NOT POSTED — ART_DIGEST_ENABLED is off]\n\n{text}"
    err = _post_art_digest(text)
    if err:
        return f"{err}\n\nDigest that failed to post:\n{text}"
    dest = f"DM to {ART_DM_USER_ID}" if SLACK_BOT_TOKEN else "art channel"
    return f"Posted to {dest} ✓\n\n{text}"


@mcp.tool()
def run_art_digest(dry_run: bool = False) -> str:
    """
    Build and (if enabled) DM Richie the daily Art Queue digest right now,
    without waiting for the 8 AM schedule. Five sections in fixed order: PROOF
    REQUESTED, MOCK-UP REQUESTED, ARTWORK DECLINED, EMB - ORDER DIGITIZING, DTF -
    ORDER TRANSFER. Each order shows hours-in-status with a red flag past the 24h
    SLA. dry_run=True (or env DRY_RUN=true) returns the text without posting.
    Even when not dry-run, nothing posts unless ART_DIGEST_ENABLED is true.
    """
    return _run_art_digest_impl(force_dry_run=dry_run)


_last_art_digest_date = None

def run_art_digest_scheduler():
    """Background thread: DM Richie the art queue at 8:00 AM America/Chicago,
    weekdays, skipping federal holidays. No-op until ART_DIGEST_ENABLED is set."""
    global _last_art_digest_date
    while True:
        try:
            now_ct = _central_now()
            if (ART_DIGEST_ENABLED
                    and now_ct.weekday() < 5
                    and not _is_us_federal_holiday(now_ct)
                    and now_ct.hour == 8 and now_ct.minute < 10
                    and _last_art_digest_date != now_ct.date().isoformat()):
                _last_art_digest_date = now_ct.date().isoformat()
                _run_art_digest_impl()
        except Exception as e:
            print(f"[ART QUEUE] scheduler error: {e}", flush=True)
        time.sleep(120)


# ── DAILY SLACK SCHEDULER ─────────────────────────────────────────────────────

def _is_us_federal_holiday(dt: datetime) -> bool:
    month, day, weekday = dt.month, dt.day, dt.weekday()
    if month == 1  and day == 1:                           return True
    if month == 1  and weekday == 0 and 15 <= day <= 21:  return True
    if month == 2  and weekday == 0 and 15 <= day <= 21:  return True
    if month == 5  and weekday == 0 and day >= 25:        return True
    if month == 6  and day == 19:                          return True
    if month == 7  and day == 4:                           return True
    if month == 9  and weekday == 0 and day <= 7:         return True
    if month == 11 and weekday == 3 and 22 <= day <= 28:  return True
    if month == 12 and day == 25:                          return True
    return False


def _build_daily_production_message() -> str:
    """TODAY's production schedule for #all-est-merch, grouped by decoration
    type (Screen Print / Embroidery / DTF). Uses the fetch-all paginator."""
    now_ct = _central_now()
    day_str = now_ct.strftime("%Y-%m-%d")
    q_list = """
    query($prodAfter: ISO8601DateTime, $prodBefore: ISO8601DateTime, $first: Int, $after: String) {
        invoices(inProductionAfter: $prodAfter, inProductionBefore: $prodBefore, first: $first, after: $after) {
            totalNodes
            pageInfo { hasNextPage endCursor }
            nodes {
                id visualId nickname totalQuantity
                status { name }
                contact { fullName customer { companyName } }
            }
        }
    }
    """
    result = paginate(q_list, {
        "prodAfter": f"{day_str}T00:00:00Z",
        "prodBefore": f"{day_str}T23:59:59Z",
    })
    try:
        date_str = now_ct.strftime("%A, %B %-d")
    except ValueError:
        date_str = now_ct.strftime("%A, %B %d")
    header = f"🏭 *Production Today — {date_str}*"
    if "error" in result:
        return f"{header}\n⚠️ Could not pull the schedule: {result['error']}"
    nodes = result.get("nodes", [])
    if not nodes:
        return f"{header}\nNothing on the production schedule today."

    ids = [n["id"] for n in nodes if n.get("id")]
    groups_raw = _fetch_groups_batch(ids)

    sections = {"Screen Print": [], "Embroidery": [], "DTF": []}
    totals = {t: {"orders": 0, "pcs": 0, "imprints": 0, "screens": 0, "minutes": 0}
              for t in sections}

    for n in nodes:
        iid = n.get("id")
        status = (n.get("status") or {}).get("name", "?")
        deco_fallback = _status_deco_fallback(status)
        contact = n.get("contact") or {}
        company = ((contact.get("customer") or {}).get("companyName")
                   or contact.get("fullName") or "—")
        nickname = n.get("nickname") or "(no nickname)"

        obj = groups_raw.get(iid)
        groups = _parse_obj_groups(obj, deco_default=deco_fallback) if obj else []
        order_qty = (int(n.get("totalQuantity") or 0)
                     or sum(g["qty"] for g in groups))

        if not any(g["imprints"] for g in groups):
            sections.setdefault(deco_fallback, [])
            totals.setdefault(deco_fallback, {"orders": 0, "pcs": 0, "imprints": 0,
                                              "screens": 0, "minutes": 0})
            sections[deco_fallback].append(
                f"• #{n.get('visualId')} — {company} — {nickname} — {order_qty} pcs — "
                f"⚠️ no imprints entered — {status}")
            totals[deco_fallback]["orders"] += 1
            totals[deco_fallback]["pcs"] += order_qty
            continue

        # Per-type accumulation: imprints = Σ over groups (group_qty × group locs)
        by_type = defaultdict(lambda: {"locs": 0, "screens": 0,
                                       "imprints": 0, "pcs": 0})
        for g in groups:
            gqty = g["qty"]
            types_in_group = set()
            for imp in g["imprints"]:
                t = by_type[imp["type"]]
                t["locs"] += 1
                t["screens"] += imp["colors"]
                t["imprints"] += gqty
                types_in_group.add(imp["type"])
            for deco in types_in_group:
                by_type[deco]["pcs"] += gqty

        for deco, g in by_type.items():
            sections.setdefault(deco, [])
            totals.setdefault(deco, {"orders": 0, "pcs": 0, "imprints": 0,
                                     "screens": 0, "minutes": 0})
            type_imprints = g["imprints"]
            type_pcs = g["pcs"]
            if deco == "Screen Print":
                rate = _sp_run_rate(type_pcs)
                minutes = g["screens"] * 8 + (int(type_imprints / rate * 60)
                                              if type_imprints else 0)
            else:
                minutes = g["locs"] * 15
            scr = (f" | {g['screens']} screen{'s' if g['screens'] != 1 else ''}"
                   if deco == "Screen Print" and g["screens"] else "")
            if type_pcs * g["locs"] == type_imprints:
                math_str = f"{type_pcs} pcs × {g['locs']} loc = {type_imprints} imprints"
            else:
                math_str = (f"{type_pcs} pcs / {g['locs']} loc "
                            f"= {type_imprints} imprints")
            sections[deco].append(
                f"• #{n.get('visualId')} — {company} — {nickname} — "
                f"{math_str}{scr} — {status}")
            t = totals[deco]
            t["orders"] += 1
            t["pcs"] += type_pcs
            t["imprints"] += type_imprints
            t["screens"] += g["screens"]
            t["minutes"] += minutes

    lines = [header]
    for deco, order_lines in sections.items():
        if not order_lines:
            lines.append(f"\n*{deco}*\n— none —")
            continue
        t = totals[deco]
        hdr = (f"\n*{deco}* — {t['orders']} order{'s' if t['orders'] != 1 else ''} | "
               f"{t['pcs']} pcs | {t['imprints']} imprints")
        if deco == "Screen Print" and t["screens"]:
            hdr += f" | {t['screens']} screens"
        if t["minutes"]:
            hdr += f" | est {_format_est_time(t['minutes'])}"
        lines.append(hdr)
        lines.extend(order_lines)
    return "\n".join(lines)


def _run_production_push_impl(force_dry_run: bool = False) -> str:
    text = _build_daily_production_message()
    if force_dry_run or _dry_run():
        print(f"[PROD PUSH — DRY RUN]\n{text}", flush=True)
        return f"[DRY RUN — not posted to Slack]\n\n{text}"
    err = _post_to_slack(text, SLACK_WEBHOOK_URL)
    if err:
        return f"{err}\n\nMessage that failed to post:\n{text}"
    return f"Posted to #all-est-merch ✓\n\n{text}"


@mcp.tool()
def run_production_push(dry_run: bool = False) -> str:
    """
    Build and post TODAY's production schedule (grouped by decoration type)
    to #all-est-merch right now, without waiting for the 7 AM schedule.
    dry_run=True (or env DRY_RUN=true) returns the text instead of posting.
    """
    return _run_production_push_impl(force_dry_run=dry_run)


_last_prod_push_date = None

def run_daily_scheduler():
    """Background thread: post TODAY's production schedule to #all-est-merch
    at 7:00 AM America/Chicago, weekdays, skipping federal holidays."""
    global _last_prod_push_date
    while True:
        try:
            now_ct = _central_now()
            if (now_ct.weekday() < 5
                    and not _is_us_federal_holiday(now_ct)
                    and now_ct.hour == 7 and now_ct.minute < 10
                    and _last_prod_push_date != now_ct.date().isoformat()):
                _last_prod_push_date = now_ct.date().isoformat()
                _run_production_push_impl()
        except Exception as e:
            print(f"[PROD PUSH] scheduler error: {e}", flush=True)
        time.sleep(120)


# ══════════════════════════════════════════════════════════════════════════════
#  BALLPARK ESTIMATOR  —  SanMar/S&S net cost × pricing matrix → rough quote
#  Data lives in ./pricing/ (sanmar_prices.json + matrices/*.csv).
#  BALLPARK only — Printavo is the final lever.
# ══════════════════════════════════════════════════════════════════════════════
import csv as _csv

_EST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing")
_EST_MARKUP_MODE = "times"  # garment = cost * (markup%/100)  e.g. $6.75 * 1.65 = $11.14

_EST_MATRICES = {
    "direct": "direct.csv", "wholesale": "wholesale.csv", "contract_sp": "contract_sp.csv",
    "brrm": "brrm.csv", "contract_emb": "contract_emb.csv", "emb_direct": "emb_direct.csv",
    "transfers_direct": "transfers_direct.csv", "transfers_contract": "transfers_contract.csv",
}
_EST_SANMAR = None          # lazy-loaded style->cost lookup
_EST_MATRIX_CACHE = {}


def _est_cogs_discount(cost):
    # tiered high-COGs reducer: $8-11 -10% | $11-20 -15% | >$20 -20%
    if cost is None or cost < 8:  return 0.0
    if cost < 11:                 return 0.10
    if cost <= 20:                return 0.15
    return 0.20


def _est_load_sanmar():
    global _EST_SANMAR
    if _EST_SANMAR is None:
        with open(os.path.join(_EST_DIR, "sanmar_prices.json")) as f:
            _EST_SANMAR = json.load(f)
    return _EST_SANMAR


def _est_ss_case_price(style):
    acct = os.environ.get("SSACTIVEWEAR_ACCOUNT"); key = os.environ.get("SSACTIVEWEAR_APIKEY")
    if not acct or not key:
        return None
    try:
        r = httpx.get(f"https://api.ssactivewear.com/v2/products/?style={style}",
                      auth=(acct, key), headers={"Accept": "application/json"}, timeout=20)
        r.raise_for_status()
        items = r.json()
    except Exception:
        return None
    prices = []
    for it in (items or []):
        for fld in ("customerPrice", "casePrice", "salePrice", "piecePrice", "mapPrice"):
            v = it.get(fld)
            if isinstance(v, (int, float)) and v > 0:
                prices.append(float(v)); break
    return min(prices) if prices else None


def _est_cost(style, size="(X)S-(X)L"):
    data = _est_load_sanmar()
    d = data.get(style.strip().upper()) or data.get(style.strip())
    if d:
        sizes = d.get("sizes", {})
        row = sizes.get(size) or sizes.get("(X)S-(X)L") or (next(iter(sizes.values()), {}))
        c = row.get("case")
        if c is not None:
            return c, {"source": "sanmar", "desc": d.get("desc"), "size": size}
    ss = _est_ss_case_price(style)
    if ss is not None:
        return ss, {"source": "ss", "desc": None, "size": size}
    return None, {"source": None, "desc": d.get("desc") if d else None, "size": size}


def _est_load_matrix(name):
    if name not in _EST_MATRIX_CACHE:
        with open(os.path.join(_EST_DIR, "matrices", _EST_MATRICES[name])) as f:
            rows = list(_csv.reader(f))
        header = rows[0]
        markup_idx = len(header) - 1
        cols = {header[i].strip(): i for i in range(1, markup_idx)}
        tiers = [r for r in rows[1:] if r and r[0].strip()]
        _EST_MATRIX_CACHE[name] = (cols, markup_idx, tiers)
    return _EST_MATRIX_CACHE[name]


def _est_pick_tier(tiers, qty):
    chosen = tiers[0]
    for r in tiers:
        if int(float(r[0])) <= qty:
            chosen = r
        else:
            break
    return chosen


def _est_resolve_col(cols, complexity):
    if str(complexity) in cols:
        return str(complexity), cols[str(complexity)]
    try:
        n = int(complexity)
        for label in cols:
            if label.lower().startswith(f"{n} color"):
                return label, cols[label]
        for label, idx in cols.items():
            nums = []
            for p in label.replace("–", "-").split("-"):
                digits = "".join(ch for ch in p if ch.isdigit())
                if digits:
                    nums.append(int(digits))
            if len(nums) == 2 and nums[0] <= n <= nums[1]:
                return label, idx
            if len(nums) == 1 and n <= nums[0]:
                return label, idx
    except (ValueError, TypeError):
        pass
    for label in cols:
        if str(complexity).lower() in label.lower():
            return label, cols[label]
    raise ValueError(f"Could not match '{complexity}' to columns {list(cols)}")


@mcp.tool()
def estimate_price(style: str, qty: int, matrix: str, complexity: str,
                   size: str = "(X)S-(X)L") -> str:
    """Ballpark price estimate for a decorated garment. NOT final — Printavo is the final lever.

    style      : garment style number (e.g. PC55, ST350, K500). SanMar first, S&S fallback.
    qty        : order quantity.
    matrix     : one of direct | wholesale | contract_sp | brrm | contract_emb | emb_direct
                 | transfers_direct | transfers_contract
    complexity : # of ink colors (screen print), stitch count (embroidery),
                 or transfer size label (DTF: LC | A5 | A4 | SQ | A3).
    size       : SanMar size band for upcharges (default (X)S-(X)L; e.g. 2XL, 3XL).

    Method: garment = blank net cost x (1 + matrix markup%); + decoration charge from the
    matrix at (qty tier x complexity); then a tiered high-COGs reducer
    ($8-11 -10% | $11-20 -15% | >$20 -20%).
    """
    if matrix not in _EST_MATRICES:
        return f"Unknown matrix '{matrix}'. Choose one of: {', '.join(_EST_MATRICES)}"
    cost, meta = _est_cost(style, size)
    try:
        cols, markup_idx, tiers = _est_load_matrix(matrix)
        tier = _est_pick_tier(tiers, qty)
        markup = float(tier[markup_idx])
        label, cidx = _est_resolve_col(cols, complexity)
        decoration = float(tier[cidx])
    except Exception as e:
        return f"Estimate error: {e}"
    if cost is None:
        return (f"{style} ({meta.get('desc') or 'unknown'}) — no cost found in SanMar list, "
                f"and S&S lookup unavailable (set SSACTIVEWEAR_ACCOUNT/APIKEY). Can't estimate.")
    garment = cost * (1 + markup / 100) if _EST_MARKUP_MODE == "plus" else cost * (markup / 100)
    per_pc = garment + decoration
    disc = _est_cogs_discount(cost)
    if disc:
        per_pc *= (1 - disc)
    red = f"  (COGs reducer -{int(disc*100)}%)" if disc else ""
    total = per_pc * qty
    return (
        f"{style} ({meta.get('desc') or ''}) x{qty} @ {meta.get('size')}\n"
        f"  matrix: {matrix} | tier {int(float(tier[0]))} | {label} | markup {markup}% "
        f"(cost src: {meta.get('source')})\n"
        f"  blank cost ${cost:.2f} -> garment ${garment:.2f} + decoration ${decoration:.2f}{red}\n"
        f"  = ${per_pc:.2f}/pc  |  TOTAL ${total:.2f}\n"
        f"  (BALLPARK — verify/finalize in Printavo)"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  SAGE CONNECT  —  promotional-product lookups (Product Search, service 103)
#  Auth via env: SAGE_ACCT_ID (default 253658), SAGE_LOGIN_ID, SAGE_AUTH_KEY.
#  POST JSON to the promoplace ConnectAPI endpoint.
# ══════════════════════════════════════════════════════════════════════════════
SAGE_ENDPOINT = "https://www.promoplace.com/ws/ws.dll/ConnectAPI"
SAGE_API_VER  = 130
SAGE_ACCT_ID  = os.environ.get("SAGE_ACCT_ID", "253658")
SAGE_LOGIN_ID = os.environ.get("SAGE_LOGIN_ID", "")
SAGE_AUTH_KEY = os.environ.get("SAGE_AUTH_KEY", "")


def _sage_call(service_id, body):
    if not SAGE_AUTH_KEY:
        return {"ok": False, "errMsg": "SAGE_AUTH_KEY not set — add it in Railway env vars."}
    payload = {
        "serviceId": service_id,
        "apiVer": SAGE_API_VER,
        "auth": {"acctId": SAGE_ACCT_ID, "loginId": SAGE_LOGIN_ID, "key": SAGE_AUTH_KEY},
    }
    payload.update(body)
    try:
        r = httpx.post(SAGE_ENDPOINT, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"ok": False, "errMsg": f"SAGE request failed: {e}"}


@mcp.tool()
def sage_product_search(query: str = "", category: str = "", price_low: float = 0,
                        price_high: float = 0, qty: int = 0, limit: int = 10,
                        sort: str = "BESTMATCH", image_res: int = 300) -> str:
    """Search SAGE for promotional products (tchotchkes: drinkware, bags, pens, awards, etc.).

    query      : quick-search text — a category ("koozies"), keyword, or SPC (smart-matched).
    category   : optional explicit SAGE category name or number (comma-separated ok).
    price_low  : optional minimum price (USD).
    price_high : optional maximum price / budget cap (USD).
    qty        : optional quantity (affects pricing tiers).
    limit      : max products to return (default 10 — the "top X").
    sort       : BESTMATCH (default) | PRICE (low→high, budget) | PRICEHIGHLOW | POPULARITY.
    image_res  : product image resolution in px — 100, 150, 200, 300, or 1800 (default 300).
    Returns name, SAGE Product Code (SPC), price, supplier, production time, and an image URL.
    """
    search = {}
    if query:      search["quickSearch"] = query
    if category:   search["categories"] = category
    if price_low:  search["priceLow"] = price_low
    if price_high: search["priceHigh"] = price_high
    if qty:        search["qty"] = qty
    search["sort"] = sort
    search["maxRecs"] = int(limit)
    search["maxTotalItems"] = max(int(limit), 50)
    search["thumbPicRes"] = int(image_res)
    search["extraReturnFields"] = "SUPPLIER,DESCRIPTION,PRODTIME,ITEMNUM"
    if not search.get("quickSearch") and not search.get("categories"):
        return "Provide a query (e.g. 'koozies') or a category to search SAGE."
    resp = _sage_call(103, {"search": search})
    if not resp.get("ok", False):
        return f"SAGE search error: {resp.get('errMsg') or resp.get('errNum') or resp}"
    prods = resp.get("products", []) or []
    total = resp.get("totalFound", len(prods))
    if not prods:
        return f"No SAGE products found for '{query or category}'."
    budget = f", ${price_low:g}-${price_high:g}" if (price_low or price_high) else ""
    lines = [f"SAGE — {total} match(es) for '{query or category}'{budget} (top {min(limit, len(prods))}):"]
    for p in prods[:limit]:
        name = p.get("name") or p.get("prName") or "(no name)"
        supplier = p.get("supplier") or ""
        prc = p.get("prc") or ""
        spc = p.get("spc") or ""
        itemnum = p.get("itemNum") or ""
        eid = p.get("prodEId") or ""
        pt = p.get("prodTime")
        img = p.get("thumbPic") or ""
        extra = []
        if supplier: extra.append(str(supplier))
        if pt:       extra.append(str(pt))
        tail = f" | {' · '.join(extra)}" if extra else ""
        headline = f"  • {name} — ${prc} | SPC {spc}"
        if itemnum: headline += f" | item# {itemnum}"
        lines.append(headline + tail)
        idbits = []
        if eid: idbits.append(f"id {eid}")
        if img: idbits.append(f"image {img}")
        if idbits: lines.append("      " + " | ".join(idbits))
    return "\n".join(lines)


@mcp.tool()
def sage_product_detail(spc: str = "", prod_eid: str = "", image_res: int = 300) -> str:
    """Full detail for one SAGE product (SAGE Connect service 105).

    Returns supplier item number, name, description, category, colors, imprint area/location,
    price-by-quantity tiers (list price + net cost), and product image URLs.
    Use after sage_product_search to build a Printavo line item and attach a mockup.
    Look up by EITHER:
      prod_eid : the numeric product id (most reliable — it's the P= value in a search image URL), OR
      spc      : the SAGE Product Code (account-specific; may not resolve on the Public/test login).
    """
    if not spc and not prod_eid:
        return "Provide prod_eid (preferred) or spc — from sage_product_search."
    body = {"includeSuppInfo": 1}
    if prod_eid:
        body["prodEId"] = str(prod_eid)
    else:
        body["spc"] = spc
    resp = _sage_call(105, body)
    if resp.get("errNum") or resp.get("ok") is False:
        return f"SAGE detail error: {resp.get('errMsg') or resp.get('errNum') or resp}"
    p = resp.get("product") or {}
    if not p:
        return f"No SAGE detail found for SPC {spc}."
    name = p.get("prName") or p.get("name") or "(no name)"
    lines = [f"{name} — SPC {spc} | item# {p.get('itemNum','')}"]
    if p.get("category"):   lines.append(f"  category: {p['category']}")
    if p.get("colors"):     lines.append(f"  colors: {p['colors']}")
    if p.get("dimensions"): lines.append(f"  dimensions: {p['dimensions']}")
    supp = p.get("supplier") or resp.get("supplier") or {}
    if supp.get("url"):     lines.append(f"  product url: {supp['url']}")
    imp, imploc = p.get("imprintArea", ""), p.get("imprintLoc", "")
    if imp or imploc:     lines.append(f"  imprint: {imp}{(' @ ' + imploc) if imploc else ''}")
    desc = (p.get("description") or "").strip()
    if desc:              lines.append(f"  description: {desc[:500]}")
    qtys = p.get("qty") or []; prc = p.get("prc") or []; net = p.get("net") or []
    tiers = []
    for i, q in enumerate(qtys):
        if not q or str(q) == "0":
            continue
        lp = prc[i] if i < len(prc) else ""
        np = net[i] if i < len(net) else ""
        tiers.append(f"{q}:${lp}" + (f" (net ${np})" if np else ""))
    if tiers:             lines.append("  price tiers — list (net cost): " + " | ".join(tiers))
    pics = p.get("pics") or []
    if pics:
        lines.append("  images:")
        for pic in pics[:6]:
            url = pic.get("url") or ""
            if not url:
                continue
            cap = f"  ({pic['caption']})" if pic.get("caption") else ""
            logo = " [logo-sample]" if pic.get("hasLogo") in (1, "1", True) else ""
            lines.append(f"    - {url}{cap}{logo}")
    return "\n".join(lines)


scheduler_thread = threading.Thread(target=run_daily_scheduler, daemon=True)
scheduler_thread.start()

cx_digest_thread = threading.Thread(target=run_cx_digest_scheduler, daemon=True)
cx_digest_thread.start()

art_digest_thread = threading.Thread(target=run_art_digest_scheduler, daemon=True)
art_digest_thread.start()

if __name__ == "__main__":
    _port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="http", host="0.0.0.0", port=_port)
