"""Smoke: edit extraction fields on 2-3 invoices, approve, print admin edit-rate stats."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import aiosqlite
import httpx
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"), override=True)

BASE = os.getenv("TAXOVA_TEST_BASE", "http://127.0.0.1:8001")
PASS = os.getenv("CA_BOOTSTRAP_PASSWORD", "Taxova@ChangeMe")
KEY = (os.getenv("DASHBOARD_API_KEY") or "").strip()
DB = os.path.join(ROOT, "storage", "gst_autopilot.db")


async def list_invoices(limit: int = 30):
    async with aiosqlite.connect(DB) as c:
        c.row_factory = aiosqlite.Row
        cur = await c.execute(
            """
            SELECT id, client_phone, supplier_name, supplier_gstin, invoice_number,
                   grand_total, is_approved, review_status
            FROM invoices ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def force_needs_review(ids: list[int]) -> None:
    async with aiosqlite.connect(DB) as c:
        for i in ids:
            await c.execute(
                "UPDATE invoices SET is_approved=0, review_status='needs_review' WHERE id=?",
                (i,),
            )
        await c.commit()


def main() -> None:
    if not KEY:
        raise SystemExit("DASHBOARD_API_KEY missing")

    rows = asyncio.run(list_invoices())
    candidates = [
        r
        for r in rows
        if not r["is_approved"] and (r.get("review_status") or "") != "rejected"
    ]
    if len(candidates) < 3:
        extra = [r["id"] for r in rows if r["id"] not in {x["id"] for x in candidates}]
        need = 3 - len(candidates)
        asyncio.run(force_needs_review(extra[:need]))
        rows = asyncio.run(list_invoices())
        candidates = [r for r in rows if not r["is_approved"]][:3]
    else:
        candidates = candidates[:3]

    print("using_invoices", [c["id"] for c in candidates])

    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        login = client.post(
            "/api/auth/login",
            json={"invite_code": "123456", "password": PASS},
        )
        login.raise_for_status()
        tok = login.json()["session_token"]
        h = {"X-CA-Session": tok, "Content-Type": "application/json"}

        phones = {
            c.get("phone_number")
            for c in client.get("/api/clients", headers=h).json()
        }
        usable = [c for c in candidates if c["client_phone"] in phones]
        if len(usable) < 2:
            print("CA cannot reach enough invoices; using admin API key")
            h = {"X-API-Key": KEY, "Content-Type": "application/json"}
            usable = candidates[:3]

        plan = [
            (
                usable[0]["id"],
                {
                    "supplier_name": (usable[0].get("supplier_name") or "Vendor")
                    + " [ER1]"
                },
            ),
            (
                usable[0]["id"],
                {
                    "invoice_number": str(usable[0].get("invoice_number") or "INV")
                    + "-ER"
                },
            ),
            (
                usable[1]["id"],
                {"grand_total": float(usable[1].get("grand_total") or 100) + 0.01},
            ),
        ]
        if len(usable) >= 3:
            plan.append((usable[2]["id"], {"supplier_gstin": "29AAACW3775F1Z1"}))

        edited: set[int] = set()
        for inv_id, fields in plan:
            r = client.put(
                f"/api/invoices/{inv_id}",
                headers=h,
                json={"fields": fields},
            )
            print("edit", inv_id, list(fields.keys()), r.status_code)
            if r.status_code != 200:
                print("  ", r.text[:300])
                raise SystemExit("edit failed — stop")
            edited.add(inv_id)

        for inv_id in sorted(edited):
            r = client.post(f"/api/invoices/{inv_id}/approve", headers=h, json={})
            print("approve", inv_id, r.status_code, r.json() if r.status_code == 200 else r.text[:200])
            if r.status_code != 200:
                raise SystemExit("approve failed — stop")

        stats = client.get(
            "/api/admin/extraction-edit-stats?days=30",
            headers={"X-API-Key": KEY},
        )
        print("stats_status", stats.status_code)
        body = stats.json()
        print(json.dumps(body, indent=2))
        if not body.get("by_field"):
            raise SystemExit("FAIL: by_field empty")
        if body.get("edit_rate") in (None, 0, 0.0) and not body.get(
            "approved_with_extraction_edits"
        ):
            raise SystemExit("FAIL: edit_rate / approved_with_extraction_edits empty")
        print("EDIT_RATE_CHECK_PASS")


if __name__ == "__main__":
    main()
