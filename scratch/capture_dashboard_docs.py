"""Capture part-wise Taxova dashboard screenshots for docs/DASHBOARD_GUIDE.md."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

KEY = os.getenv("DASHBOARD_API_KEY", "")
OUT = ROOT / "docs" / "images" / "dashboard"
OUT.mkdir(parents=True, exist_ok=True)
BASE = os.getenv("DASH_SHOT_BASE", "http://127.0.0.1:8001").rstrip("/")


def main() -> int:
    if not KEY:
        print("DASHBOARD_API_KEY missing")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        # Escape for JS string
        key_js = KEY.replace("\\", "\\\\").replace("'", "\\'")
        page.add_init_script(
            f"localStorage.setItem('DASHBOARD_API_KEY', '{key_js}');"
            "localStorage.setItem('DASHBOARD_MODULE', 'gst');"
        )
        page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        try:
            page.wait_for_selector("#client-select", timeout=15000)
            opts = page.eval_on_selector_all(
                "#client-select option",
                "els => els.map(e => e.value).filter(Boolean)",
            )
            if opts:
                page.select_option("#client-select", opts[0])
                page.wait_for_timeout(2500)
                print("selected client", opts[0])
            else:
                print("no clients in dropdown")
        except Exception as e:
            print("client select failed:", e)

        page.screenshot(path=str(OUT / "01-full-dashboard.png"))
        print("saved 01-full-dashboard")

        parts = [
            ("02-sidebar", "aside"),
            ("03-header", "header"),
            ("04-month-close", "#month-close-strip"),
            ("05-gst-filing", "#gst-filing-panel"),
            ("06-pilot", "#pilot-panel"),
        ]
        for name, sel in parts:
            try:
                loc = page.locator(sel).first
                loc.scroll_into_view_if_needed(timeout=5000)
                page.wait_for_timeout(300)
                loc.screenshot(path=str(OUT / f"{name}.png"))
                print("saved", name)
            except Exception as e:
                print("fail", name, e)

        # Review queue / KPI band
        try:
            page.locator("#module-gst").first.scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            page.locator("#module-gst").first.screenshot(path=str(OUT / "07-gst-module.png"))
            print("saved 07-gst-module")
        except Exception as e:
            print("fail gst module", e)

        # Invoice table if present
        try:
            table = page.locator(".queue-table, #invoice-table, table").first
            if table.count():
                table.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                table.screenshot(path=str(OUT / "08-invoice-queue.png"))
                print("saved 08-invoice-queue")
        except Exception as e:
            print("fail queue", e)

        # Income Tax module
        try:
            page.click("#module-btn-itr")
            page.wait_for_timeout(1800)
            page.screenshot(path=str(OUT / "09-income-tax-full.png"))
            print("saved 09-income-tax-full")
            itr = page.locator("#module-itr").first
            if itr.count():
                itr.screenshot(path=str(OUT / "10-itr-module.png"))
                print("saved 10-itr-module")
        except Exception as e:
            print("fail itr", e)

        browser.close()

    print("files:", sorted(p.name for p in OUT.glob("*.png")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
