"""Smoke test for GET /api/pilot/stats and pilot status gates."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)


async def main() -> int:
    import db

    await db.init_db()
    stats = await db.get_pilot_stats(days=30, firm_id=None)
    pilot = stats.get("pilot") or {}
    print("approved", stats.get("approved_invoices"))
    print("edit_rate", stats.get("edit_rate"))
    print("status", pilot.get("status"), pilot.get("status_label"))
    print("progress", pilot.get("progress_pct"))
    print("rejected", pilot.get("rejected_invoices"), "skipped", pilot.get("skipped_invoices"))
    print("pending", pilot.get("pending_review"))
    print("filing_critical", len(pilot.get("filing_critical") or []))
    assert "status" in pilot
    assert "min_approvals" in pilot
    assert pilot["min_approvals"] == 25
    assert "show_edit_rate" in pilot
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
