"""Day-zero pilot: reset extraction edit stats, then verify approved_invoices == 0."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

BASE = os.getenv("ITR_TEST_BASE", os.getenv("PROD_BASE", "http://127.0.0.1:8001")).rstrip("/")
KEY = os.getenv("DASHBOARD_API_KEY", "")


def api(method: str, path: str, data=None):
    headers = {"X-API-Key": KEY, "Content-Type": "application/json"}
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"error": raw[:400]}


def main() -> int:
    if not KEY:
        print("Set DASHBOARD_API_KEY")
        return 1
    do_reset = "--reset" in sys.argv
    firm_id = None
    for arg in sys.argv[1:]:
        if arg.startswith("--firm="):
            firm_id = int(arg.split("=", 1)[1])

    if do_reset:
        payload = {"confirm": "RESET_EDIT_STATS"}
        if firm_id is not None:
            payload["firm_id"] = firm_id
        st, body = api("POST", "/api/admin/extraction-edit-stats/reset", payload)
        print("reset", st, body)
        if st != 200:
            return 1

    st, stats = api("GET", "/api/admin/extraction-edit-stats?days=30")
    print("stats", st, {
        "approved_invoices": stats.get("approved_invoices"),
        "edit_rate": stats.get("edit_rate"),
        "days": stats.get("days"),
    })
    if st != 200:
        return 1
    approved = int(stats.get("approved_invoices") or 0)
    if do_reset and approved != 0:
        print("FAIL: expected approved_invoices=0 after reset")
        return 1
    print("OK" if approved == 0 else f"NOTE: {approved} approvals already in window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
