"""
sandbox.py — Sandbox (Quicko GSP) public GST APIs.

Uses SANDBOX_API_KEY / SANDBOX_API_SECRET from env.
Caches JWT access token until near expiry (tokens are valid ~24h).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date
from typing import Any, Optional

import httpx

from itc_rules import normalize_gstin, validate_gstin

logger = logging.getLogger("sandbox")

BASE_URL = os.getenv("SANDBOX_API_BASE", "https://api.sandbox.co.in").rstrip("/")
API_VERSION = "1.0.0"

_lock = threading.Lock()
_cached_token: Optional[str] = None
_cached_token_expires_at: float = 0.0  # epoch seconds


def configured() -> bool:
    return bool(os.getenv("SANDBOX_API_KEY", "").strip() and os.getenv("SANDBOX_API_SECRET", "").strip())


def classify_sandbox_error(exc_or_msg: Any) -> dict[str, str]:
    """
    Map Sandbox/GSP failures to stable error_code + user-facing message.
    Full technical text stays in logs / optional 'technical' field.
    """
    msg = str(exc_or_msg or "").strip()
    low = msg.lower()
    technical = msg[:400] if msg else ""

    if "not configured" in low or "sandbox_api_key" in low or "sandbox_api_secret" in low:
        return {
            "error_code": "sandbox_not_configured",
            "user_message": (
                "GSTN lookup is not configured. Invoice data is fine — "
                "you can still review, edit, and approve."
            ),
            "technical": technical,
        }
    if "subscription has expired" in low or (
        "401" in low and "subscription" in low
    ):
        return {
            "error_code": "sandbox_subscription_expired",
            "user_message": (
                "GSTN lookup is temporarily unavailable because the GST portal API "
                "subscription needs renewal. Your invoice data is fine — you can still "
                "review, edit, and approve. Recheck after the API is renewed."
            ),
            "technical": technical,
        }
    if any(
        x in low
        for x in ("timed out", "timeout", "connection", "connecterror", "unreachable", "name or service not known")
    ):
        return {
            "error_code": "sandbox_unreachable",
            "user_message": (
                "Could not reach GSTN right now. Try Recheck in a minute. "
                "Your invoice data is fine — you can still review and approve."
            ),
            "technical": technical,
        }
    if "401" in low or "403" in low or "authenticate failed" in low:
        return {
            "error_code": "sandbox_auth_failed",
            "user_message": (
                "GSTN lookup could not authenticate with the portal API. "
                "Invoice data is fine — you can still review and approve."
            ),
            "technical": technical,
        }
    return {
        "error_code": "sandbox_error",
        "user_message": (
            "GSTN lookup is temporarily unavailable. Your invoice data is fine — "
            "you can still review, edit, and approve."
        ),
        "technical": technical,
    }


def is_portal_api_outage_message(msg: Optional[str]) -> bool:
    """True when a cached/live error is an API/config outage, not a bad GSTIN."""
    if not msg:
        return False
    code = classify_sandbox_error(msg).get("error_code") or ""
    if code in {
        "sandbox_subscription_expired",
        "sandbox_auth_failed",
        "sandbox_unreachable",
        "sandbox_not_configured",
    }:
        return True
    # Generic classify falls through to sandbox_error — only treat as outage when
    # the text clearly looks like a Sandbox/GSP transport failure.
    low = str(msg).lower()
    return any(
        x in low
        for x in (
            "sandbox authenticate",
            "sandbox /gst/",
            "subscription has expired",
            "api.sandbox",
            "transaction_id",
            "returned non-json",
        )
    )


def indian_financial_year(as_of: Optional[date] = None) -> str:
    """Return e.g. 'FY 2026-27' for Sandbox track-returns API."""
    d = as_of or date.today()
    start = d.year if d.month >= 4 else d.year - 1
    return f"FY {start}-{str(start + 1)[-2:]}"


def previous_financial_year(as_of: Optional[date] = None) -> str:
    """Prefer previous FY for filing history (usually more complete)."""
    d = as_of or date.today()
    start = d.year if d.month >= 4 else d.year - 1
    prev = start - 1
    return f"FY {prev}-{str(start)[-2:]}"


def _credentials() -> tuple[str, str]:
    key = os.getenv("SANDBOX_API_KEY", "").strip()
    secret = os.getenv("SANDBOX_API_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError(
            "Sandbox credentials missing. Set SANDBOX_API_KEY and SANDBOX_API_SECRET in .env."
        )
    return key, secret


def _authenticate(client: httpx.Client) -> str:
    global _cached_token, _cached_token_expires_at
    key, secret = _credentials()
    resp = client.post(
        f"{BASE_URL}/authenticate",
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "x-api-key": key,
            "x-api-secret": secret,
            "x-api-version": API_VERSION,
        },
    )
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}
    if resp.status_code != 200:
        raise RuntimeError(f"Sandbox authenticate failed ({resp.status_code}): {body}")
    token = (body.get("data") or {}).get("access_token")
    if not token:
        raise RuntimeError(f"Sandbox authenticate returned no access_token: {body}")
    # Docs: JWT valid ~24h; refresh 1h early
    _cached_token = token
    _cached_token_expires_at = time.time() + (23 * 3600)
    return token


def get_access_token(*, force_refresh: bool = False) -> str:
    global _cached_token, _cached_token_expires_at
    with _lock:
        if (
            not force_refresh
            and _cached_token
            and time.time() < _cached_token_expires_at
        ):
            return _cached_token
        with httpx.Client(timeout=30.0) as client:
            return _authenticate(client)


def _public_post(
    path: str,
    *,
    json_body: dict[str, Any],
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """POST to a Sandbox public GST endpoint; returns parsed JSON body."""
    key, _secret = _credentials()
    token = get_access_token()

    def _do(access_token: str) -> httpx.Response:
        with httpx.Client(timeout=45.0) as client:
            return client.post(
                f"{BASE_URL}{path}",
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "x-api-key": key,
                    "authorization": access_token,  # no Bearer prefix
                    "x-api-version": "1.0",
                },
                params=params or None,
                json=json_body,
            )

    resp = _do(token)
    if resp.status_code in (401, 403):
        token = get_access_token(force_refresh=True)
        resp = _do(token)

    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(
            f"Sandbox {path} returned non-JSON ({resp.status_code}): {resp.text[:400]}"
        )

    if resp.status_code == 400:
        msg = body.get("message") or "Bad request"
        raise ValueError(str(msg))
    if resp.status_code != 200:
        raise RuntimeError(f"Sandbox {path} failed ({resp.status_code}): {body}")
    return body


def _unwrap_taxpayer_payload(raw: Any) -> dict[str, Any]:
    """Sandbox nests taxpayer fields under data.data (and sometimes data)."""
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("data")
    if isinstance(inner, dict):
        nested = inner.get("data")
        if isinstance(nested, dict) and (
            "lgnm" in nested
            or "tradeNam" in nested
            or "sts" in nested
            or "EFiledlist" in nested
        ):
            return nested
        if "lgnm" in inner or "tradeNam" in inner or "sts" in inner or "EFiledlist" in inner:
            return inner
        return inner
    return raw


def normalize_gstin_profile(gstin: str, api_body: dict[str, Any]) -> dict[str, Any]:
    """Flatten Sandbox search response into a stable dashboard shape."""
    payload = _unwrap_taxpayer_payload(api_body)
    message = payload.get("message") or api_body.get("message")
    error_cd = payload.get("error_cd")

    legal_name = payload.get("lgnm") or payload.get("legal_name")
    trade_name = payload.get("tradeNam") or payload.get("trade_name")
    status = payload.get("sts") or payload.get("status")
    taxpayer_type = payload.get("dty") or payload.get("taxpayer_type")
    state_jurisdiction = payload.get("stj") or payload.get("stjCd")
    centre_jurisdiction = payload.get("ctj") or payload.get("ctjCd")

    pradr = payload.get("pradr") or {}
    addr = pradr.get("addr") if isinstance(pradr, dict) else {}
    if not isinstance(addr, dict):
        addr = {}
    address_parts = [
        addr.get("bno"),
        addr.get("bnm"),
        addr.get("flno"),
        addr.get("st"),
        addr.get("loc"),
        addr.get("dst"),
        addr.get("stcd"),
        addr.get("pncd"),
    ]
    address = ", ".join(str(p).strip() for p in address_parts if p)

    found = bool(legal_name or trade_name or status)
    return {
        "gstin": normalize_gstin(gstin),
        "found": found,
        "legal_name": legal_name,
        "trade_name": trade_name,
        "status": status,
        "taxpayer_type": taxpayer_type,
        "state_jurisdiction": state_jurisdiction,
        "centre_jurisdiction": centre_jurisdiction,
        "address": address or None,
        "message": None if found else (message or "No records found"),
        "error_code": error_cd,
        "raw": payload if found else payload,
    }


def _format_return_period(ret_prd: Any) -> str:
    """Convert MMYYYY (e.g. 082020) → 2020-08."""
    s = str(ret_prd or "").strip()
    if len(s) == 6 and s.isdigit():
        return f"{s[2:6]}-{s[0:2]}"
    return s or "—"


def normalize_filing_history(api_body: dict[str, Any], financial_year: str) -> dict[str, Any]:
    payload = _unwrap_taxpayer_payload(api_body)
    rows = payload.get("EFiledlist") or payload.get("eFiledlist") or []
    if not isinstance(rows, list):
        rows = []

    filings = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        filings.append(
            {
                "return_type": row.get("rtntype") or row.get("return_type") or "—",
                "period": _format_return_period(row.get("ret_prd")),
                "period_raw": str(row.get("ret_prd") or ""),
                "status": row.get("status") or "—",
                "filed_on": row.get("dof") or row.get("filed_on") or "—",
                "mode": row.get("mof") or row.get("mode") or "—",
                "arn": row.get("arn") or "",
                "valid": row.get("valid"),
            }
        )

    # Newest period first when possible
    filings.sort(key=lambda r: r.get("period_raw") or "", reverse=True)

    message = payload.get("message") or api_body.get("message")
    error_cd = payload.get("error_cd") or payload.get("errorCode")
    return {
        "financial_year": financial_year,
        "filings": filings,
        "count": len(filings),
        "message": message if not filings else None,
        "error_code": error_cd,
    }


def search_gstin(gstin: str) -> dict[str, Any]:
    """
    Public GSTIN search via Sandbox.
    Raises ValueError for bad input; RuntimeError for config/API failures.
    """
    g = normalize_gstin(gstin)
    if not validate_gstin(g):
        raise ValueError("Invalid GSTIN format (expected 15-character GSTIN).")

    body = _public_post(
        "/gst/compliance/public/gstin/search",
        json_body={"gstin": g},
    )
    return normalize_gstin_profile(g, body)


def track_gst_returns(
    gstin: str,
    financial_year: Optional[str] = None,
    *,
    return_type: Optional[str] = None,
) -> dict[str, Any]:
    """
    Public return filing track (filed GSTR-1 / 3B / etc. for a FY).
    No taxpayer OTP required.
    """
    g = normalize_gstin(gstin)
    if not validate_gstin(g):
        raise ValueError("Invalid GSTIN format (expected 15-character GSTIN).")

    fy = (financial_year or "").strip() or indian_financial_year()
    if not fy.upper().startswith("FY"):
        # allow "2025-26" → "FY 2025-26"
        fy = f"FY {fy}"

    params: dict[str, Any] = {"financial_year": fy}
    if return_type:
        params["gstr"] = return_type

    body = _public_post(
        "/gst/compliance/public/gstrs/track",
        json_body={"gstin": g},
        params=params,
    )
    return normalize_filing_history(body, fy)


def lookup_gstin(
    gstin: str,
    *,
    include_history: bool = True,
    financial_year: Optional[str] = None,
) -> dict[str, Any]:
    """Profile + optional public filing history for dashboard."""
    profile = search_gstin(gstin)
    if not include_history:
        profile["filing_history"] = None
        return profile

    fy = (financial_year or "").strip() or previous_financial_year()
    try:
        history = track_gst_returns(gstin, fy)
        # If default FY is empty, also try current FY (for early-year filings)
        if not financial_year and history.get("count", 0) == 0:
            current = indian_financial_year()
            if current != history.get("financial_year"):
                alt = track_gst_returns(gstin, current)
                if alt.get("count", 0) > 0:
                    history = alt
        profile["filing_history"] = history
    except Exception as e:
        logger.warning("Filing history fetch failed for %s: %s", profile.get("gstin"), e)
        profile["filing_history"] = {
            "financial_year": fy if fy.upper().startswith("FY") else f"FY {fy}",
            "filings": [],
            "count": 0,
            "message": str(e),
            "error_code": "fetch_failed",
        }
    return profile


def _normalize_party_name(name: Optional[str]) -> str:
    import re

    s = re.sub(r"[^A-Z0-9\s]", " ", str(name or "").upper())
    s = re.sub(
        r"\b(PRIVATE|LIMITED|PVT|LTD|LLP|OPC|AND|THE|CO|COMPANY|INDIA)\b",
        " ",
        s,
    )
    return re.sub(r"\s+", " ", s).strip()


def party_names_likely_match(
    extracted: Optional[str],
    portal_legal: Optional[str],
    portal_trade: Optional[str] = None,
) -> Optional[bool]:
    """True / False / None (insufficient data)."""
    a = _normalize_party_name(extracted)
    if not a or len(a) < 3:
        return None
    candidates = [
        _normalize_party_name(portal_legal),
        _normalize_party_name(portal_trade),
    ]
    candidates = [c for c in candidates if c and len(c) >= 3]
    if not candidates:
        return None
    for b in candidates:
        if a == b or a in b or b in a:
            return True
        ta = {t for t in a.split(" ") if len(t) > 2}
        tb = {t for t in b.split(" ") if len(t) > 2}
        if not ta or not tb:
            continue
        hit = len(ta & tb)
        if hit / min(len(ta), len(tb)) >= 0.6:
            return True
    return False


def assess_portal_party(
    profile: Optional[dict[str, Any]],
    extracted_name: Optional[str] = None,
    *,
    role: str = "supplier",
    error_message: Optional[str] = None,
) -> dict[str, Any]:
    """
    Derive queue risk flags from a public GSTIN profile vs bill party name.
    role: 'supplier' | 'recipient'
    """
    label = "Supplier" if role == "supplier" else "Recipient"
    if error_message and not profile:
        classified = classify_sandbox_error(error_message)
        if is_portal_api_outage_message(error_message):
            return {
                "role": role,
                "ok": None,
                "found": None,
                "risk_boost": 0,
                "reason": None,
                "issues": [],
                "status": None,
                "legal_name": None,
                "name_match": None,
                "cached": True,
                "portal_outage": True,
                "error_code": classified.get("error_code"),
                "user_message": classified.get("user_message"),
            }
        return {
            "role": role,
            "ok": False,
            "found": False,
            "risk_boost": 15,
            "reason": f"{label} GSTN check failed",
            "issues": [classified.get("user_message") or error_message],
            "status": None,
            "legal_name": None,
            "name_match": None,
            "cached": True,
            "error_code": classified.get("error_code"),
            "user_message": classified.get("user_message"),
        }

    if not profile:
        return {
            "role": role,
            "ok": None,
            "found": None,
            "risk_boost": 0,
            "reason": None,
            "issues": [],
            "status": None,
            "legal_name": None,
            "name_match": None,
            "cached": False,
        }

    if not profile.get("found"):
        msg = profile.get("message") or "No GSTN record"
        # Cached Sandbox auth failures were previously stored as found=False — do not
        # treat those as "GSTIN not found" (misleading for CAs).
        if is_portal_api_outage_message(msg) or profile.get("portal_outage"):
            classified = classify_sandbox_error(msg)
            return {
                "role": role,
                "ok": None,
                "found": None,
                "risk_boost": 0,
                "reason": None,
                "issues": [],
                "status": None,
                "legal_name": None,
                "trade_name": None,
                "name_match": None,
                "gstin": profile.get("gstin"),
                "cached": True,
                "portal_outage": True,
                "error_code": classified.get("error_code"),
                "user_message": classified.get("user_message"),
            }
        return {
            "role": role,
            "ok": False,
            "found": False,
            "risk_boost": 45,
            "reason": f"{label} GSTIN not found on GSTN",
            "issues": ["This GSTIN was not found on GSTN"],
            "status": None,
            "legal_name": None,
            "trade_name": None,
            "name_match": None,
            "gstin": profile.get("gstin"),
            "cached": True,
        }

    status = str(profile.get("status") or "").strip()
    status_l = status.lower()
    inactive = bool(status_l and status_l not in ("active", "provisional"))
    name_match = party_names_likely_match(
        extracted_name, profile.get("legal_name"), profile.get("trade_name")
    )

    issues: list[str] = []
    risk = 0
    if inactive:
        issues.append(f"{label} GSTIN status is \"{status}\" (not Active)")
        risk += 50
    if name_match is False:
        portal_name = profile.get("legal_name") or profile.get("trade_name") or "—"
        bill_name = extracted_name or "—"
        issues.append(f"{label} name mismatch: bill \"{bill_name}\" vs GSTN \"{portal_name}\"")
        risk += 35

    reason = None
    if inactive:
        reason = f"{label} GSTIN inactive on portal"
    elif name_match is False:
        reason = f"{label} name != GSTN legal name"
    elif not issues:
        reason = None

    return {
        "role": role,
        "ok": len(issues) == 0,
        "found": True,
        "risk_boost": risk,
        "reason": reason,
        "issues": issues,
        "status": status or None,
        "legal_name": profile.get("legal_name"),
        "trade_name": profile.get("trade_name"),
        "taxpayer_type": profile.get("taxpayer_type"),
        "name_match": name_match,
        "gstin": profile.get("gstin"),
        "cached": True,
    }


PORTAL_CACHE_TTL_SECONDS = int(os.getenv("SANDBOX_PORTAL_CACHE_TTL_HOURS", "24")) * 3600


def portal_cache_is_fresh(checked_at: Optional[str], *, ttl_seconds: int = PORTAL_CACHE_TTL_SECONDS) -> bool:
    if not checked_at:
        return False
    from datetime import datetime, timezone

    s = str(checked_at).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # SQLite CURRENT_TIMESTAMP often 'YYYY-MM-DD HH:MM:SS'
        try:
            dt = datetime.strptime(str(checked_at)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age <= ttl_seconds


def unwrap_gstr2b_document(payload: Any) -> dict[str, Any]:
    """
    Dig Sandbox/GSTN response envelopes until docdata / b2b / rtnprd is found.
    Returns a dict suitable for gstr2b.parse_gstr2b_json.
    """
    cur: Any = payload
    for _ in range(8):
        if not isinstance(cur, dict):
            break
        if cur.get("docdata") or cur.get("b2b") or cur.get("rtnprd") or cur.get("fp"):
            nested = cur.get("data")
            if isinstance(nested, dict) and (
                nested.get("docdata") or nested.get("b2b") or nested.get("rtnprd")
            ):
                cur = nested
                continue
            out = dict(cur)
            if out.get("rtnprd") and not out.get("return_period") and not out.get("fp"):
                out["fp"] = out["rtnprd"]
            return out
        if isinstance(cur.get("data"), dict):
            cur = cur["data"]
            continue
        break
    return payload if isinstance(payload, dict) else {}


def return_period_to_year_month(return_period: str) -> tuple[str, str]:
    """YYYY-MM → (year, month) path segments for Sandbox GSTR-2B API."""
    import re as _re

    s = str(return_period or "").strip()
    if not _re.match(r"^\d{4}-\d{2}$", s):
        raise ValueError("return_period must be YYYY-MM")
    return s[:4], s[5:7]


def request_taxpayer_otp(username: str, gstin: str, *, source: str = "primary") -> dict[str, Any]:
    """
    Request GST portal OTP for taxpayer session.
    Prerequisite: Enable API Access on gst.gov.in for this user.
    """
    g = normalize_gstin(gstin)
    user = (username or "").strip()
    if not user:
        raise ValueError("GST portal username is required")
    if not validate_gstin(g):
        raise ValueError("Invalid GSTIN format (expected 15-character GSTIN).")

    key, _secret = _credentials()
    token = get_access_token()
    with httpx.Client(timeout=45.0) as client:
        resp = client.post(
            f"{BASE_URL}/gst/compliance/tax-payer/otp",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "x-api-key": key,
                "authorization": token,
                "x-api-version": API_VERSION,
                "x-source": source or "primary",
            },
            json={"username": user, "gstin": g},
        )
    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(f"OTP request returned non-JSON ({resp.status_code}): {resp.text[:400]}")

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"OTP request failed ({resp.status_code}): {body}")

    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    status_cd = str(data.get("status_cd") or "")
    err = data.get("error") if isinstance(data.get("error"), dict) else None
    if status_cd == "0" or err:
        msg = (err or {}).get("message") or body.get("message") or "OTP generation failed"
        code = (err or {}).get("error_cd") or ""
        raise RuntimeError(f"{code + ': ' if code else ''}{msg}")

    return {
        "ok": True,
        "gstin": g,
        "username": user,
        "status_cd": status_cd or "1",
        "transaction_id": body.get("transaction_id"),
        "message": "OTP sent to the GST-registered mobile/email for this username.",
    }


def verify_taxpayer_otp(
    username: str,
    gstin: str,
    otp: str,
    *,
    source: str = "primary",
) -> dict[str, Any]:
    """Verify OTP and return taxpayer access_token (+ expiry). Valid ~6 hours."""
    g = normalize_gstin(gstin)
    user = (username or "").strip()
    otp_s = str(otp or "").strip()
    if not user:
        raise ValueError("GST portal username is required")
    if not validate_gstin(g):
        raise ValueError("Invalid GSTIN format (expected 15-character GSTIN).")
    if not otp_s or not otp_s.isdigit():
        raise ValueError("OTP must be a numeric code")

    key, _secret = _credentials()
    token = get_access_token()
    with httpx.Client(timeout=45.0) as client:
        resp = client.post(
            f"{BASE_URL}/gst/compliance/tax-payer/otp/verify",
            params={"otp": otp_s},
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "x-api-key": key,
                "authorization": token,
                "x-api-version": API_VERSION,
                "x-source": source or "primary",
            },
            json={"username": user, "gstin": g},
        )
    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(f"OTP verify returned non-JSON ({resp.status_code}): {resp.text[:400]}")

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"OTP verify failed ({resp.status_code}): {body}")

    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    status_cd = str(data.get("status_cd") or "")
    err = data.get("error") if isinstance(data.get("error"), dict) else None
    access = data.get("access_token")
    if status_cd == "0" or err or not access:
        msg = (err or {}).get("message") or body.get("message") or "OTP verification failed"
        code = (err or {}).get("error_cd") or ""
        raise RuntimeError(f"{code + ': ' if code else ''}{msg}")

    return {
        "ok": True,
        "gstin": g,
        "username": user,
        "access_token": access,
        "token_expiry": data.get("token_expiry"),
        "session_expiry": data.get("session_expiry"),
        "transaction_id": body.get("transaction_id"),
    }


def fetch_gstr2b_document(
    return_period: str,
    *,
    taxpayer_access_token: str,
    file_number: Optional[int] = None,
) -> dict[str, Any]:
    """
    GET live GSTR-2B for YYYY-MM using taxpayer session token.
    Returns unwrapped document dict (ready for parse_gstr2b_json).
    """
    year, month = return_period_to_year_month(return_period)
    key, _secret = _credentials()
    params: dict[str, Any] = {}
    if file_number is not None:
        params["file_number"] = int(file_number)

    with httpx.Client(timeout=90.0) as client:
        resp = client.get(
            f"{BASE_URL}/gst/compliance/tax-payer/gstrs/gstr-2b/{year}/{month}",
            headers={
                "accept": "application/json",
                "x-api-key": key,
                "authorization": taxpayer_access_token,
                "x-api-version": API_VERSION,
            },
            params=params or None,
        )
    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(f"GSTR-2B fetch returned non-JSON ({resp.status_code}): {resp.text[:400]}")

    if resp.status_code == 401:
        raise RuntimeError("Taxpayer session expired — request a new OTP.")
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"GSTR-2B fetch failed ({resp.status_code}): {body}")

    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if isinstance(data, dict):
        err = data.get("error") if isinstance(data.get("error"), dict) else None
        status_cd = str(data.get("status_cd") or "")
        if err or status_cd == "0":
            msg = (err or {}).get("message") or data.get("message") or "GSTR-2B not available"
            code = (err or {}).get("error_cd") or ""
            raise RuntimeError(f"{code + ': ' if code else ''}{msg}")

    doc = unwrap_gstr2b_document(body)
    if not doc.get("return_period") and not doc.get("fp") and not doc.get("rtnprd"):
        doc = dict(doc)
        doc["return_period"] = return_period
    return doc
