"""Smoke-test firm multi-tenancy isolation via local API."""
from __future__ import annotations

import json
import os
import sys
import uuid

import httpx
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"), override=True)

BASE = os.getenv("TAXOVA_TEST_BASE", "http://127.0.0.1:8001")
API_KEY = (os.getenv("DASHBOARD_API_KEY") or "").strip()
CA_PASS = (os.getenv("CA_BOOTSTRAP_PASSWORD") or "Taxova@ChangeMe").strip()


def fail(msg: str) -> None:
    print("FAIL:", msg)
    sys.exit(1)


def ok(msg: str) -> None:
    print("OK:", msg)


def main() -> None:
    if not API_KEY:
        fail("DASHBOARD_API_KEY missing in .env — needed for admin firm create")

    suffix = uuid.uuid4().hex[:6]
    # 6-digit numeric invites only
    n = int(suffix, 16) % 900000
    invite_a = f"{100000 + n}"[:6]
    invite_b = f"{100000 + ((n + 137) % 900000)}"[:6]
    if invite_a == invite_b:
        invite_b = f"{(int(invite_a) + 1) % 1000000:06d}"
    if invite_a == "123456":
        invite_a = "123457"
    if invite_b == "123456":
        invite_b = "123458"

    # 12-digit WhatsApp-style phones (91 + 10 digits)
    phone_a = f"91{(7000000000 + n) % 10000000000:010d}"
    phone_b = f"91{(8000000000 + n) % 10000000000:010d}"

    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        # Health
        r = client.get("/health")
        if r.status_code != 200:
            fail(f"server not healthy: {r.status_code} {r.text[:200]}")
        ok("server healthy")

        # Create Firm A
        r = client.post(
            "/api/admin/firms",
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            json={
                "firm_name": f"Test Firm A {suffix}",
                "slug": f"test-firm-a-{suffix}",
                "ca_name": "CA Alpha",
                "invite_code": invite_a,
                "password": CA_PASS,
            },
        )
        if r.status_code != 200:
            fail(f"create firm A: {r.status_code} {r.text[:400]}")
        firm_a = r.json()
        ok(f"created firm A id={firm_a['firm']['id']} invite={invite_a}")

        # Create Firm B
        r = client.post(
            "/api/admin/firms",
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            json={
                "firm_name": f"Test Firm B {suffix}",
                "slug": f"test-firm-b-{suffix}",
                "ca_name": "CA Beta",
                "invite_code": invite_b,
                "password": CA_PASS,
            },
        )
        if r.status_code != 200:
            fail(f"create firm B: {r.status_code} {r.text[:400]}")
        firm_b = r.json()
        ok(f"created firm B id={firm_b['firm']['id']} invite={invite_b}")

        # Link client to Firm A CA via assign API
        r = client.post(
            "/api/ca/validate",
            json={"invite_code": invite_a, "client_phone": phone_a},
        )
        # endpoint might be different — try ca-assign style
        if r.status_code == 404:
            r = client.post(
                "/api/ca/link",
                json={"invite_code": invite_a, "client_phone": phone_a},
            )
        if r.status_code == 404:
            # use onboard endpoint used by ca_assign.html
            r = client.post(
                "/api/onboard/link-ca",
                json={"invite_code": invite_a, "client_phone": phone_a},
            )

        # Discover link endpoint from known routes
        if r.status_code >= 400:
            # Direct DB link fallback for smoke test
            import asyncio
            import db

            async def _link():
                await db.init_db()
                return await db.link_client_to_ca(phone_a, invite_a)

            ca = asyncio.run(_link())
            ok(f"linked {phone_a} to CA {invite_a} via db (firm_id={ca.get('firm_id')})")
        else:
            ok(f"linked {phone_a} via API: {r.status_code}")

        # Also create orphan client for firm B path
        import asyncio
        import db

        async def _prep_b():
            await db.get_or_create_client(phone_b, name="Firm B Client")
            return await db.link_client_to_ca(phone_b, invite_b)

        asyncio.run(_prep_b())
        ok(f"linked {phone_b} to CA {invite_b}")

        # Login as CA A
        r = client.post(
            "/api/auth/login",
            json={"invite_code": invite_a, "password": CA_PASS},
        )
        if r.status_code != 200:
            fail(f"login A: {r.status_code} {r.text[:300]}")
        tok_a = r.json()["session_token"]
        firm_name_a = (r.json().get("firm") or {}).get("name")
        ok(f"CA A login firm={firm_name_a}")

        # Login as CA B
        r = client.post(
            "/api/auth/login",
            json={"invite_code": invite_b, "password": CA_PASS},
        )
        if r.status_code != 200:
            fail(f"login B: {r.status_code} {r.text[:300]}")
        tok_b = r.json()["session_token"]
        ok("CA B login")

        # CA A clients
        r = client.get("/api/clients", headers={"X-CA-Session": tok_a})
        if r.status_code != 200:
            fail(f"clients A: {r.status_code} {r.text[:300]}")
        clients_a = r.json()
        phones_a = {c.get("phone_number") for c in clients_a}
        if phone_a not in phones_a:
            fail(f"CA A missing own client {phone_a}; got {phones_a}")
        if phone_b in phones_a:
            fail(f"CA A can see Firm B client {phone_b}")
        ok(f"CA A sees only own client(s) among test phones ({len(clients_a)} total linked)")

        # CA B clients
        r = client.get("/api/clients", headers={"X-CA-Session": tok_b})
        clients_b = r.json()
        phones_b = {c.get("phone_number") for c in clients_b}
        if phone_b not in phones_b:
            fail(f"CA B missing own client {phone_b}")
        if phone_a in phones_b:
            fail(f"CA B can see Firm A client {phone_a} — isolation broken")
        ok("CA B cannot see Firm A client")

        # CA B direct access to Firm A client → 403
        r = client.get(
            f"/api/invoices?client_phone={phone_a}",
            headers={"X-CA-Session": tok_b},
        )
        if r.status_code != 403:
            fail(f"expected 403 for CA B → Firm A invoices, got {r.status_code} {r.text[:200]}")
        ok("CA B blocked (403) from Firm A invoices")

        # Admin sees both
        r = client.get("/api/clients", headers={"X-API-Key": API_KEY})
        if r.status_code != 200:
            fail(f"admin clients: {r.status_code}")
        admin_phones = {c.get("phone_number") for c in r.json()}
        if phone_a not in admin_phones or phone_b not in admin_phones:
            fail("admin key should see both test clients")
        ok("admin API key sees both firms' clients")

        # /api/auth/me for CA A
        r = client.get("/api/auth/me", headers={"X-CA-Session": tok_a})
        me = r.json()
        if not me.get("firm_id"):
            fail(f"auth/me missing firm_id: {me}")
        ok(f"auth/me firm_id={me.get('firm_id')} firm_name={me.get('firm_name')}")

        # ── Logout path regression: login A → logout → token dead → login B → no bleed ──
        r = client.post("/api/auth/logout", headers={"X-CA-Session": tok_a})
        if r.status_code != 200:
            fail(f"logout A: {r.status_code} {r.text[:300]}")
        logout_body = r.json()
        if not logout_body.get("invalidated"):
            fail(f"logout A should invalidate session server-side: {logout_body}")
        if "DASHBOARD_API_KEY" not in (logout_body.get("clear_local") or []):
            fail(f"logout must tell client to clear DASHBOARD_API_KEY: {logout_body}")
        ok("CA A logout invalidated session server-side")

        # Replayed token must not authenticate as CA (401 when API key required)
        r = client.get("/api/clients", headers={"X-CA-Session": tok_a})
        if r.status_code != 401:
            fail(
                f"after logout, tok_a must get 401 (not firm A data); "
                f"got {r.status_code} {r.text[:200]}"
            )
        ok("revoked tok_a rejected (401)")

        # Double-logout is safe (noop, not error)
        r = client.post("/api/auth/logout", headers={"X-CA-Session": tok_a})
        if r.status_code != 200 or r.json().get("invalidated") is not False:
            fail(f"second logout should be noop invalidated=false: {r.text[:200]}")
        ok("second logout is noop")

        # Fresh login B after A logout — still isolated
        r = client.post(
            "/api/auth/login",
            json={"invite_code": invite_b, "password": CA_PASS},
        )
        if r.status_code != 200:
            fail(f"re-login B after A logout: {r.status_code} {r.text[:300]}")
        tok_b2 = r.json()["session_token"]
        r = client.get("/api/clients", headers={"X-CA-Session": tok_b2})
        if r.status_code != 200:
            fail(f"clients after re-login B: {r.status_code}")
        phones_b2 = {c.get("phone_number") for c in r.json()}
        if phone_a in phones_b2:
            fail("after A logout → B login, Firm A client still visible — session bleed")
        if phone_b not in phones_b2:
            fail(f"CA B missing own client after re-login: {phones_b2}")
        ok("login A -> logout -> login B: no client bleed")

        # Old tok_b still valid until its own logout (independent sessions)
        r = client.post("/api/auth/logout", headers={"X-CA-Session": tok_b})
        if not r.json().get("invalidated"):
            fail("logout B (original token) should invalidate")
        r = client.get("/api/clients", headers={"X-CA-Session": tok_b})
        if r.status_code != 401:
            fail(f"revoked tok_b should 401, got {r.status_code}")
        ok("independent session revoke for CA B")

    print("\nALL TENANCY CHECKS PASSED")
    print(json.dumps({
        "invite_a": invite_a,
        "invite_b": invite_b,
        "phone_a": phone_a,
        "phone_b": phone_b,
    }, indent=2))


if __name__ == "__main__":
    main()