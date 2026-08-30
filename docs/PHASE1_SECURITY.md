# Phase 1 Security — Taxova.ai

## What was added

1. **Encryption at rest** — invoice files + extraction JSON encrypted when `ENCRYPTION_KEY` is set (`TXV1ENC1` + Fernet).
2. **Authenticated file access** — public `/storage` mount removed; use `GET /api/files/{path}` with CA session or API key.
3. **CA login** — `POST /api/auth/login` with invite code + password → `X-CA-Session` token (12h).
4. **Client scoping** — logged-in CAs only see clients linked via invite code; API key still sees all.
5. **Security audit log** — login, file view, invoice view recorded in `security_audit_logs`.

## Setup

```bash
pip install cryptography
# Generate key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add to `.env` / Railway:

```
ENCRYPTION_KEY=<fernet-key>
CA_BOOTSTRAP_PASSWORD=Taxova@ChangeMe
DASHBOARD_API_KEY=<strong-secret>
```

Restart the server so `init_db` applies CA passwords + new tables.

## Dashboard login

1. Open `/dashboard` → **Settings**
2. Choose **1 = CA login**
3. Invite code: `123456` (demo) or your CA code
4. Password: value of `CA_BOOTSTRAP_PASSWORD` (default `Taxova@ChangeMe`)

Or keep using the shared **API key** (admin / scripts).

## Dashboard logout

- Sidebar **Sign out** and Settings → **3** call `POST /api/auth/logout` with `X-CA-Session`.
- Server **deletes** the `ca_sessions` row (`invalidated: true`).
- Client clears `CA_SESSION_TOKEN`, `CA_PROFILE`, `CA_FIRM`, and **`DASHBOARD_API_KEY`** so logout cannot fall back to “see all firms”.
- Regression: `scratch/_test_tenancy.py` covers login A → logout → login B (no bleed).

## Notes

- Old plaintext files still open; new uploads encrypt.
- Link clients to a CA (`/ca-assign`) or they won’t appear under CA session (API key still sees all).
- Change `CA_BOOTSTRAP_PASSWORD` in production and rotate keys.
- Phase 2 (optional): hash-anchor approved invoices — not included here.
