"""
gstr2b.py — GSTR-2B import + books reconciliation (Sec 16(2)(aa)).

Supports:
  - Simplified Autopilot JSON
  - Common portal-like GSTR-2B JSON (b2b / docdata.b2b)
  - CSV (supplier_gstin, invoice_number, invoice_date, taxable_value, cgst, sgst, igst)

Matching is offline (no GST portal login). CA uploads 2B export; we match purchase invoices.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from itc_rules import normalize_gstin, validate_gstin

AMOUNT_TOLERANCE = 1.0  # ₹
DATE_TOLERANCE_DAYS = 3

MATCH_MATCHED = "matched"
MATCH_MISMATCH = "mismatch"
MATCH_UNMATCHED = "unmatched"
MATCH_NONE = "none"  # no 2B imported for period / not a purchase

_INUM_CLEAN = re.compile(r"[^A-Z0-9]")


def normalize_invoice_number(inum: Optional[str]) -> str:
    raw = (inum or "").strip().upper()
    return _INUM_CLEAN.sub("", raw)


def parse_any_date(value: Optional[str]) -> Optional[str]:
    """Return YYYY-MM-DD or None."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # epoch-ish or already close
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return None


def period_from_date(date_yyyy_mm_dd: Optional[str]) -> Optional[str]:
    if not date_yyyy_mm_dd or len(date_yyyy_mm_dd) < 7:
        return None
    return date_yyyy_mm_dd[:7]


def _f(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _entry(
    *,
    supplier_gstin: str,
    supplier_name: str = "",
    invoice_number: str,
    invoice_date: str,
    taxable_value: float = 0.0,
    igst: float = 0.0,
    cgst: float = 0.0,
    sgst: float = 0.0,
    invoice_value: float = 0.0,
    place_of_supply: str = "",
    invoice_type: str = "R",
) -> Optional[dict]:
    gstin = normalize_gstin(supplier_gstin)
    inum = (invoice_number or "").strip()
    idt = parse_any_date(invoice_date)
    if not gstin or not inum or not idt:
        return None
    gst_total = igst + cgst + sgst
    if invoice_value <= 0 and taxable_value > 0:
        invoice_value = taxable_value + gst_total
    return {
        "supplier_gstin": gstin,
        "supplier_name": (supplier_name or "").strip() or None,
        "invoice_number": inum,
        "invoice_number_norm": normalize_invoice_number(inum),
        "invoice_date": idt,
        "taxable_value": round(taxable_value, 2),
        "igst": round(igst, 2),
        "cgst": round(cgst, 2),
        "sgst": round(sgst, 2),
        "invoice_value": round(invoice_value, 2),
        "place_of_supply": (place_of_supply or "")[:2] or None,
        "invoice_type": invoice_type or "R",
    }


def _parse_b2b_blocks(b2b_list: list) -> list[dict]:
    out: list[dict] = []
    for party in b2b_list or []:
        ctin = party.get("ctin") or party.get("supplier_gstin") or ""
        trdnm = party.get("trdnm") or party.get("supplier_name") or party.get("trade_name") or ""
        for inv in party.get("inv") or party.get("invoices") or []:
            # Portal often nests tax in itms[].itm_det
            txval = _f(inv.get("txval") or inv.get("taxable_value"))
            iamt = _f(inv.get("iamt") or inv.get("igst"))
            camt = _f(inv.get("camt") or inv.get("cgst"))
            samt = _f(inv.get("samt") or inv.get("sgst"))
            if not txval and inv.get("itms"):
                for itm in inv["itms"]:
                    det = itm.get("itm_det") or itm
                    txval += _f(det.get("txval"))
                    iamt += _f(det.get("iamt"))
                    camt += _f(det.get("camt"))
                    samt += _f(det.get("samt"))
            entry = _entry(
                supplier_gstin=ctin,
                supplier_name=trdnm,
                invoice_number=inv.get("inum") or inv.get("invoice_number") or "",
                invoice_date=inv.get("idt") or inv.get("invoice_date") or "",
                taxable_value=txval,
                igst=iamt,
                cgst=camt,
                sgst=samt,
                invoice_value=_f(inv.get("val") or inv.get("invoice_value") or inv.get("grand_total")),
                place_of_supply=str(inv.get("pos") or inv.get("place_of_supply") or ""),
                invoice_type=inv.get("inv_typ") or inv.get("invoice_type") or "R",
            )
            if entry:
                out.append(entry)
    return out


def parse_gstr2b_json(payload: dict | list) -> tuple[str | None, list[dict]]:
    """
    Parse JSON into (return_period YYYY-MM | None, entries).
    Period may be inferred from invoice dates if missing.
    """
    if isinstance(payload, list):
        entries = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            e = _entry(
                supplier_gstin=row.get("supplier_gstin") or row.get("ctin") or "",
                supplier_name=row.get("supplier_name") or row.get("trdnm") or "",
                invoice_number=row.get("invoice_number") or row.get("inum") or "",
                invoice_date=row.get("invoice_date") or row.get("idt") or "",
                taxable_value=_f(row.get("taxable_value") or row.get("txval")),
                igst=_f(row.get("igst") or row.get("iamt")),
                cgst=_f(row.get("cgst") or row.get("camt")),
                sgst=_f(row.get("sgst") or row.get("samt")),
                invoice_value=_f(row.get("invoice_value") or row.get("val") or row.get("grand_total")),
                place_of_supply=str(row.get("place_of_supply") or row.get("pos") or ""),
            )
            if e:
                entries.append(e)
        return _infer_period(entries), entries

    if not isinstance(payload, dict):
        return None, []

    # Unwrap common envelopes (incl. Sandbox nested data.data…)
    data = payload
    for _ in range(5):
        if not isinstance(data, dict):
            break
        if data.get("docdata") or data.get("b2b") or data.get("rtnprd") or data.get("fp") or data.get("return_period"):
            break
        nxt = data.get("data")
        if isinstance(nxt, dict):
            data = nxt
        else:
            break
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}

    docdata = data.get("docdata") if isinstance(data.get("docdata"), dict) else data

    period = (
        payload.get("return_period")
        or payload.get("fp")
        or payload.get("rtnprd")
        or data.get("return_period")
        or data.get("fp")
        or data.get("rtnprd")
        or docdata.get("rtnprd")
    )
    period = _normalize_period(period)

    b2b = (
        payload.get("b2b")
        or data.get("b2b")
        or docdata.get("b2b")
        or []
    )
    entries = _parse_b2b_blocks(b2b)

    # Flat entries array (Autopilot format)
    if not entries and isinstance(payload.get("entries"), list):
        return parse_gstr2b_json(payload["entries"])[0] or period, parse_gstr2b_json(payload["entries"])[1]

    if not period:
        period = _infer_period(entries)
    return period, entries


def parse_gstr2b_csv(text: str) -> tuple[str | None, list[dict]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        # normalize keys
        mapped = { (k or "").strip().lower().replace(" ", "_"): v for k, v in row.items() }
        e = _entry(
            supplier_gstin=mapped.get("supplier_gstin") or mapped.get("ctin") or "",
            supplier_name=mapped.get("supplier_name") or mapped.get("trdnm") or "",
            invoice_number=mapped.get("invoice_number") or mapped.get("inum") or "",
            invoice_date=mapped.get("invoice_date") or mapped.get("idt") or "",
            taxable_value=_f(mapped.get("taxable_value") or mapped.get("txval")),
            igst=_f(mapped.get("igst") or mapped.get("iamt")),
            cgst=_f(mapped.get("cgst") or mapped.get("camt")),
            sgst=_f(mapped.get("sgst") or mapped.get("samt")),
            invoice_value=_f(mapped.get("invoice_value") or mapped.get("val") or mapped.get("grand_total")),
            place_of_supply=str(mapped.get("place_of_supply") or mapped.get("pos") or ""),
        )
        if e:
            rows.append(e)
    return _infer_period(rows), rows


def _normalize_period(fp: Any) -> Optional[str]:
    if not fp:
        return None
    s = str(fp).strip()
    # YYYY-MM
    if re.match(r"^\d{4}-\d{2}$", s):
        return s
    # MMYYYY portal style
    if re.match(r"^\d{6}$", s):
        return f"{s[2:6]}-{s[0:2]}"
    # YYYYMM
    if re.match(r"^\d{4}\d{2}$", s) and len(s) == 6:
        return f"{s[0:4]}-{s[4:6]}"
    return None


def _infer_period(entries: list[dict]) -> Optional[str]:
    periods = [period_from_date(e.get("invoice_date")) for e in entries if e.get("invoice_date")]
    periods = [p for p in periods if p]
    if not periods:
        return None
    # majority period
    counts: dict[str, int] = {}
    for p in periods:
        counts[p] = counts.get(p, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def amounts_close(a: float, b: float, tol: float = AMOUNT_TOLERANCE) -> bool:
    return abs(float(a or 0) - float(b or 0)) <= tol


def dates_close(a: Optional[str], b: Optional[str], days: int = DATE_TOLERANCE_DAYS) -> bool:
    if not a or not b:
        return False
    try:
        da = datetime.strptime(a[:10], "%Y-%m-%d")
        db = datetime.strptime(b[:10], "%Y-%m-%d")
    except ValueError:
        return a[:10] == b[:10]
    return abs((da - db).days) <= days


def compare_books_to_2b(invoice: dict, entry: dict) -> tuple[str, Optional[str]]:
    """
    Return (match_status, mismatch_reason).
    Assumes supplier_gstin + invoice_number already matched.
    """
    reasons: list[str] = []
    inv_date = parse_any_date(invoice.get("invoice_date"))
    ent_date = entry.get("invoice_date")
    if not dates_close(inv_date, ent_date):
        reasons.append(f"Date books {inv_date} vs 2B {ent_date}")

    inv_taxable = _f(invoice.get("total_taxable_value"))
    ent_taxable = _f(entry.get("taxable_value"))
    if not amounts_close(inv_taxable, ent_taxable):
        reasons.append(f"Taxable ₹{inv_taxable:.2f} vs ₹{ent_taxable:.2f}")

    inv_gst = (
        _f(invoice.get("total_cgst"))
        + _f(invoice.get("total_sgst"))
        + _f(invoice.get("total_igst"))
    )
    ent_gst = _f(entry.get("cgst")) + _f(entry.get("sgst")) + _f(entry.get("igst"))
    if not amounts_close(inv_gst, ent_gst):
        reasons.append(f"GST ₹{inv_gst:.2f} vs ₹{ent_gst:.2f}")

    if reasons:
        return MATCH_MISMATCH, "; ".join(reasons)
    return MATCH_MATCHED, None


def build_2b_index(entries: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Index by (supplier_gstin, invoice_number_norm)."""
    idx: dict[tuple[str, str], list[dict]] = {}
    for e in entries:
        key = (normalize_gstin(e.get("supplier_gstin")), e.get("invoice_number_norm") or normalize_invoice_number(e.get("invoice_number")))
        if not key[0] or not key[1]:
            continue
        idx.setdefault(key, []).append(e)
    return idx


def reconcile_purchase_invoices(
    invoices: list[dict],
    entries: list[dict],
    *,
    client_gstin: str,
) -> dict[str, Any]:
    """
    Match books purchase invoices (recipient == client) to 2B entries.

    Returns summary + per-invoice results + unmatched 2B orphans.
    """
    gstin = normalize_gstin(client_gstin)
    idx = build_2b_index(entries)
    used_entry_ids: set[int] = set()

    invoice_results: list[dict] = []
    counts = {
        MATCH_MATCHED: 0,
        MATCH_MISMATCH: 0,
        MATCH_UNMATCHED: 0,
        MATCH_NONE: 0,
    }

    for inv in invoices:
        rec = normalize_gstin(inv.get("recipient_gstin"))
        if not gstin or rec != gstin:
            invoice_results.append({
                "invoice_id": inv["id"],
                "gstr2b_match_status": MATCH_NONE,
                "gstr2b_entry_id": None,
                "gstr2b_mismatch_reason": None,
            })
            counts[MATCH_NONE] += 1
            continue

        key = (
            normalize_gstin(inv.get("supplier_gstin")),
            normalize_invoice_number(inv.get("invoice_number")),
        )
        candidates = idx.get(key) or []
        # Prefer unused entries; if multiple, pick closest taxable
        candidate = None
        for c in candidates:
            cid = c.get("id")
            if cid is not None and cid in used_entry_ids:
                continue
            candidate = c
            break
        if candidate is None and candidates:
            candidate = candidates[0]

        if candidate is None:
            invoice_results.append({
                "invoice_id": inv["id"],
                "gstr2b_match_status": MATCH_UNMATCHED,
                "gstr2b_entry_id": None,
                "gstr2b_mismatch_reason": "Not found in GSTR-2B for this period",
            })
            counts[MATCH_UNMATCHED] += 1
            continue

        status, reason = compare_books_to_2b(inv, candidate)
        eid = candidate.get("id")
        if eid is not None:
            used_entry_ids.add(eid)
        invoice_results.append({
            "invoice_id": inv["id"],
            "gstr2b_match_status": status,
            "gstr2b_entry_id": eid,
            "gstr2b_mismatch_reason": reason,
        })
        counts[status] = counts.get(status, 0) + 1

    orphans = []
    for e in entries:
        eid = e.get("id")
        if eid is not None and eid not in used_entry_ids:
            orphans.append(e)
        elif eid is None:
            # no id yet — check if any invoice claimed this key
            key = (normalize_gstin(e.get("supplier_gstin")), e.get("invoice_number_norm") or normalize_invoice_number(e.get("invoice_number")))
            claimed = any(
                r["gstr2b_match_status"] in (MATCH_MATCHED, MATCH_MISMATCH)
                and normalize_gstin(
                    next((i for i in invoices if i["id"] == r["invoice_id"]), {}).get("supplier_gstin")
                ) == key[0]
                and normalize_invoice_number(
                    next((i for i in invoices if i["id"] == r["invoice_id"]), {}).get("invoice_number")
                ) == key[1]
                for r in invoice_results
            )
            if not claimed:
                orphans.append(e)

    return {
        "counts": counts,
        "invoice_results": invoice_results,
        "orphan_2b_count": len(orphans),
        "orphans": orphans,
    }
