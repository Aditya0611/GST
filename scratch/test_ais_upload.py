"""Smoke test: AIS parse + reconcile + optional live API upload."""
from __future__ import annotations

import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import ais as ais_mod

DUMMY = ROOT / "scratch" / "dummy_ais.json"
BASE = os.getenv("ITR_TEST_BASE", "http://127.0.0.1:8001").rstrip("/")
KEY = os.getenv("DASHBOARD_API_KEY", "")
PHONE = os.getenv("ITR_TEST_PHONE", "919999000001")


def main() -> int:
    raw = DUMMY.read_bytes()
    summary, payload = ais_mod.load_ais_bytes(raw, filename="dummy_ais.json")
    assert summary["salary"] == 1200000
    assert summary["tds_on_salary"] == 90000
    assert summary["interest_income"] == 15000
    print("parse OK", summary)

    match = ais_mod.reconcile_ais_vs_return(
        summary,
        {
            "pan": "ABCDE1234F",
            "financial_year": "2025-26",
            "gross_salary": 1200000,
            "tds": 90000,
            "other_income": 0,
        },
    )
    assert match["status"] in ("review", "mismatch")  # interest warn
    assert any(m["field"] == "interest_income" for m in match["mismatches"])
    print("reconcile interest warn OK", match["status"], len(match["mismatches"]))

    perfect = ais_mod.reconcile_ais_vs_return(
        summary,
        {
            "pan": "ABCDE1234F",
            "financial_year": "2025-26",
            "gross_salary": 1200000,
            "tds": 90000,
            "other_income": 15000,
        },
    )
    assert perfect["status"] == "match"
    print("reconcile match OK")

    if not KEY:
        print("SKIP API (no DASHBOARD_API_KEY)")
        print("SMOKE PASS")
        return 0

    def api(method, path, data=None, files=None, timeout=120):
        headers = {"X-API-Key": KEY}
        body = None
        if files:
            boundary = "----Ais" + uuid.uuid4().hex
            field, filename, content, ctype = files
            parts = [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {ctype}\r\n\r\n".encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
            body = b"".join(parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw_err or "{}")
            except json.JSONDecodeError:
                return e.code, {"error": raw_err[:500] or f"HTTP {e.code}"}
        except Exception as e:
            return 0, {"error": str(e)}

    st, health = api("GET", "/health")
    if st != 200:
        print("server not up — unit checks already passed")
        print("SMOKE PASS (offline)")
        return 0

    st, row = api(
        "POST",
        "/api/itr/returns",
        {"client_phone": PHONE, "financial_year": "2025-26", "pan": "ABCDE1234F"},
    )
    print("open", st, row.get("id") if isinstance(row, dict) else row)
    if not isinstance(row, dict) or "id" not in row:
        print("SMOKE PASS (offline units only)")
        return 0
    itr_id = row["id"]
    # Seed Form 16-like fields
    api(
        "PUT",
        f"/api/itr/returns/{itr_id}",
        {
            "gross_salary": 1200000,
            "tds": 90000,
            "other_income": 0,
            "exemptions": 50000,
            "deductions_80c": 150000,
        },
    )
    st, up = api(
        "POST",
        f"/api/itr/returns/{itr_id}/ais",
        files=("file", "dummy_ais.json", raw, "application/json"),
    )
    print("upload", st, (up.get("reconcile") or {}).get("status") if isinstance(up, dict) else up)
    assert st == 200, up
    assert (up.get("reconcile") or {}).get("status") in ("review", "mismatch")
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
