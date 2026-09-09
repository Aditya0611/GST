"""
ais.py — Annual Information Statement (AIS) upload + Form 16 reconcile (Phase 1).

Supports:
  1) Plain JSON (demo / already-decrypted export)
  2) Portal encrypted AIS JSON (AES-CBC) when password = PAN + DOB (ddmmyyyy)

No government API — user downloads AIS from incometax.gov.in and uploads here.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("ais")

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# Money compare tolerance (rupees)
DEFAULT_TOLERANCE = 1.0


def _to_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _looks_encrypted_payload(raw: str) -> bool:
    s = raw.strip()
    if s.startswith("{") or s.startswith("["):
        return False
    if s.startswith('"') and s.endswith('"'):
        try:
            inner = json.loads(s)
            if isinstance(inner, str):
                s = inner.strip()
        except json.JSONDecodeError:
            pass
    if len(s) < 80:
        return False
    # IV(32 hex) + salt(32 hex) + ciphertext
    head = s[:64]
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", head))


def _decrypt_ais_ciphertext(raw: str, password: str) -> dict[str, Any]:
    """Decrypt ITD AIS Utility JSON (PBKDF2-HMAC-SHA256 + AES-256-CBC)."""
    from cryptography.hazmat.primitives import hashes, padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    s = raw.strip()
    if s.startswith('"') and s.endswith('"'):
        s = json.loads(s)
    if not isinstance(s, str):
        raise ValueError("Encrypted AIS payload must be a string.")

    iv = bytes.fromhex(s[:32])
    salt = bytes.fromhex(s[32:64])
    cipher_part = s[64:]
    try:
        ciphertext = base64.b64decode(cipher_part, validate=True)
    except Exception:
        ciphertext = bytes.fromhex(cipher_part)

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=1000,
    )
    key = kdf.derive(password.encode("utf-8"))
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Decrypted AIS is not a JSON object.")
    return data


def decrypt_ais_file(raw_text: str, *, pan: str = "", dob_ddmmyyyy: str = "", password: str = "") -> dict[str, Any]:
    """
    Try password variants until decrypt succeeds.
    password: full password if known; else build from pan + dob.
    """
    if not _looks_encrypted_payload(raw_text):
        raise ValueError("File does not look like an encrypted AIS download.")

    candidates: list[str] = []
    if password.strip():
        candidates.append(password.strip())
    pan_u = (pan or "").strip().upper()
    pan_l = pan_u.lower()
    dob = re.sub(r"\D", "", dob_ddmmyyyy or "")
    if pan_u and len(dob) == 8:
        candidates.extend(
            [
                f"{pan_u}{dob}",
                f"{pan_l}{dob}",
                f"{pan_u}GQ39%*g{dob}",
                f"{pan_l}GQ39%*g{dob}",
            ]
        )
    # de-dupe preserve order
    seen = set()
    uniq = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    if not uniq:
        raise ValueError(
            "Encrypted AIS needs a password. Enter PAN + DOB (ddmmyyyy), "
            "or the full AIS Utility password."
        )

    last_err: Exception | None = None
    for pwd in uniq:
        try:
            return _decrypt_ais_ciphertext(raw_text, pwd)
        except Exception as e:
            last_err = e
            continue
    raise ValueError(
        f"Could not decrypt AIS with provided password ({last_err}). "
        "Check PAN case and DOB as ddmmyyyy."
    )


def _walk_collect_amounts(node: Any, out: list[tuple[str, float]], path: str = "") -> None:
    if isinstance(node, dict):
        # Prefer explicit amount keys with a nearby description
        desc_bits = []
        for k in ("informationDescription", "description", "infoDesc", "label", "type", "informationCode"):
            if node.get(k):
                desc_bits.append(str(node.get(k)))
        amount = None
        for k in (
            "amount",
            "totalAmount",
            "totalValue",
            "value",
            "tdsAmount",
            "taxDeducted",
            "grossAmount",
            "salary",
            "interestAmount",
        ):
            if k in node and node[k] is not None and not isinstance(node[k], (dict, list)):
                amount = _to_float(node[k])
                break
        if amount is not None and amount != 0:
            label = " ".join(desc_bits) or path or "item"
            out.append((label.lower(), amount))
        for k, v in node.items():
            _walk_collect_amounts(v, out, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_collect_amounts(item, out, f"{path}[{i}]")


def _sum_matching(items: list[tuple[str, float]], keywords: tuple[str, ...]) -> float:
    total = 0.0
    for label, amount in items:
        if any(k in label for k in keywords):
            total += amount
    return round(total, 2)


def summarize_ais_payload(data: dict[str, Any], *, filename: str = "") -> dict[str, Any]:
    """
    Normalize AIS / TIS-like JSON into Taxova compare fields.
    Works with our dummy schema and best-effort portal plaintext structures.
    """
    # Direct demo / simplified schema
    if any(k in data for k in ("salary", "tds_on_salary", "gross_salary", "interest_income")):
        pan = (data.get("pan") or "").strip().upper() or None
        fy = (data.get("financial_year") or data.get("fy") or "").strip() or None
        salary = _to_float(data.get("salary") if data.get("salary") is not None else data.get("gross_salary"))
        tds = _to_float(
            data.get("tds_on_salary") if data.get("tds_on_salary") is not None else data.get("tds")
        )
        interest = _to_float(data.get("interest_income") or data.get("interest"))
        other = _to_float(data.get("other_income"))
        return {
            "pan": pan,
            "financial_year": fy,
            "salary": salary,
            "tds_on_salary": tds,
            "interest_income": interest,
            "other_income": other,
            "source": data.get("source") or "plain_json",
            "filename": filename or None,
            "encrypted": False,
        }

    # Filename FY hint: PAN_2025-26_AIS_...
    fy = None
    m = re.search(r"(20\d{2}-\d{2})", filename or "")
    if m:
        fy = m.group(1)
    pan = None
    for key in ("pan", "PAN", "permanentAccountNumber"):
        if data.get(key):
            pan = str(data.get(key)).strip().upper()
            break
    if not pan and filename:
        m2 = re.match(r"^([A-Z]{5}[0-9]{4}[A-Z])_", filename.upper())
        if m2:
            pan = m2.group(1)

    items: list[tuple[str, float]] = []
    _walk_collect_amounts(data, items)

    salary = _sum_matching(
        items,
        ("salary", "salaries", "income from salary", "section 17", "form 16"),
    )
    tds = _sum_matching(
        items,
        ("tds on salary", "tax deducted", "tds-salary", "192", "salary tds"),
    )
    # If TDS keyword hit nothing, try generic tds but avoid double counting salary label
    if tds <= 0:
        tds = _sum_matching(items, ("tds",))
    interest = _sum_matching(
        items,
        ("interest", "savings bank", "term deposit", "fd interest", "194a"),
    )
    # Other = non-salary non-tds non-interest rough residual (cap noise)
    categorized = salary + tds + interest
    other = 0.0
    for label, amount in items:
        if any(k in label for k in ("salary", "tds", "interest", "savings", "deposit")):
            continue
        if amount > 0:
            other += amount
    # Don't let "other" explode from duplicated nested totals
    if other > categorized * 3 and categorized > 0:
        other = 0.0

    return {
        "pan": pan,
        "financial_year": fy or (data.get("financialYear") or data.get("financial_year")),
        "salary": round(salary, 2),
        "tds_on_salary": round(tds, 2),
        "interest_income": round(interest, 2),
        "other_income": round(other, 2),
        "source": "parsed_ais",
        "filename": filename or None,
        "encrypted": False,
        "raw_item_count": len(items),
    }


def load_ais_bytes(
    content: bytes,
    *,
    filename: str = "",
    pan: str = "",
    dob_ddmmyyyy: str = "",
    password: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Returns (summary, raw_payload).
    raw_payload is plaintext JSON object used for storage.
    """
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("Empty AIS file.")

    encrypted = _looks_encrypted_payload(text)
    if encrypted:
        payload = decrypt_ais_file(
            text, pan=pan, dob_ddmmyyyy=dob_ddmmyyyy, password=password
        )
        summary = summarize_ais_payload(payload, filename=filename)
        summary["encrypted"] = True
        summary["source"] = "decrypted_ais"
        return summary, payload

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"AIS file is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise ValueError("AIS JSON must be an object at the top level.")
    summary = summarize_ais_payload(payload, filename=filename)
    return summary, payload


def reconcile_ais_vs_return(
    ais_summary: dict[str, Any],
    itr_row: dict[str, Any],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Compare AIS summary against Form 16 / ITR saved fields."""
    mismatches: list[dict[str, Any]] = []

    def add(field: str, ais_val: Any, return_val: Any, *, severity: str, note: str):
        mismatches.append(
            {
                "field": field,
                "ais": ais_val,
                "return_value": return_val,
                "severity": severity,
                "note": note,
            }
        )

    ais_pan = (ais_summary.get("pan") or "").strip().upper()
    ret_pan = (itr_row.get("pan") or "").strip().upper()
    if ais_pan and ret_pan and ais_pan != ret_pan:
        add("pan", ais_pan, ret_pan, severity="error", note="PAN mismatch between AIS and return")
    elif ais_pan and not ret_pan:
        add("pan", ais_pan, None, severity="info", note="AIS has PAN; return PAN empty — consider copying")

    ais_fy = (ais_summary.get("financial_year") or "").strip()
    ret_fy = (itr_row.get("financial_year") or "").strip()
    if ais_fy and ret_fy and ais_fy != ret_fy:
        add(
            "financial_year",
            ais_fy,
            ret_fy,
            severity="warn",
            note="Financial year differs — confirm you uploaded the correct AIS",
        )

    ais_salary = _to_float(ais_summary.get("salary"))
    ret_salary = _to_float(itr_row.get("gross_salary"))
    if ais_salary > 0 or ret_salary > 0:
        if abs(ais_salary - ret_salary) > tolerance:
            add(
                "gross_salary",
                ais_salary,
                ret_salary,
                severity="error",
                note="Salary in AIS does not match Form 16 / saved gross salary",
            )

    ais_tds = _to_float(ais_summary.get("tds_on_salary"))
    ret_tds = _to_float(itr_row.get("tds"))
    if ais_tds > 0 or ret_tds > 0:
        if abs(ais_tds - ret_tds) > tolerance:
            add(
                "tds",
                ais_tds,
                ret_tds,
                severity="error",
                note="TDS in AIS does not match Form 16 / saved TDS",
            )

    ais_interest = _to_float(ais_summary.get("interest_income"))
    ret_other = _to_float(itr_row.get("other_income"))
    if ais_interest > tolerance and ret_other + tolerance < ais_interest:
        add(
            "interest_income",
            ais_interest,
            ret_other,
            severity="warn",
            note="AIS shows interest income not fully reflected in other income",
        )

    ais_other = _to_float(ais_summary.get("other_income"))
    if ais_other > tolerance and ret_other + tolerance < ais_other:
        add(
            "other_income",
            ais_other,
            ret_other,
            severity="warn",
            note="AIS shows other income above saved other income",
        )

    status = "match"
    if any(m["severity"] == "error" for m in mismatches):
        status = "mismatch"
    elif mismatches:
        status = "review"

    return {
        "status": status,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "ais": ais_summary,
        "tolerance": tolerance,
    }
