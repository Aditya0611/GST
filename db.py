"""
db.py — Database management layer for Taxova.ai

Supports local development with SQLite (aiosqlite) out-of-the-box,
and seamlessly switches to PostgreSQL (asyncpg) if DATABASE_URL is set in environment.
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime
import sqlite3

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("db")

DATABASE_URL = os.getenv("DATABASE_URL", "")
IS_POSTGRES = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

# Always resolve to an absolute path so cwd does not change which DB is used.
_PROJECT_ROOT = Path(__file__).resolve().parent
_STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(_PROJECT_ROOT / "storage")))
if not _STORAGE_DIR.is_absolute():
    _STORAGE_DIR = (_PROJECT_ROOT / _STORAGE_DIR).resolve()
else:
    _STORAGE_DIR = _STORAGE_DIR.resolve()
SQLITE_DB_PATH = _STORAGE_DIR / "gst_autopilot.db"


async def get_connection():
    """Returns an active database connection context or manager."""
    if IS_POSTGRES:
        import asyncpg
        return await asyncpg.connect(DATABASE_URL)
    else:
        # Ensure directory exists for local SQLite
        SQLITE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


async def init_db():
    """Create schema tables if they do not exist."""
    conn = await get_connection()
    logger.info("Initializing database schema... (Postgres=%s)", IS_POSTGRES)

    try:
        if IS_POSTGRES:
            # PostgreSQL Schema
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    phone_number VARCHAR(20) PRIMARY KEY,
                    name VARCHAR(100),
                    gstin VARCHAR(15),
                    registered BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS invoices (
                    id SERIAL PRIMARY KEY,
                    client_phone VARCHAR(20) REFERENCES clients(phone_number),
                    file_path VARCHAR(255),
                    supplier_name VARCHAR(150),
                    supplier_gstin VARCHAR(15),
                    recipient_name VARCHAR(150),
                    recipient_gstin VARCHAR(15),
                    invoice_number VARCHAR(50),
                    invoice_date VARCHAR(20),
                    place_of_supply VARCHAR(100),
                    total_taxable_value DOUBLE PRECISION,
                    total_cgst DOUBLE PRECISION,
                    total_sgst DOUBLE PRECISION,
                    total_igst DOUBLE PRECISION,
                    grand_total DOUBLE PRECISION,
                    business_category VARCHAR(100),
                    is_calculation_correct BOOLEAN,
                    is_itc_eligible BOOLEAN,
                    itc_ineligibility_reason TEXT,
                    supply_type VARCHAR(20),
                    is_approved BOOLEAN DEFAULT FALSE,
                    review_status VARCHAR(32) DEFAULT 'needs_review',
                    hitl_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS line_items (
                    id SERIAL PRIMARY KEY,
                    invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
                    description TEXT,
                    hsn_or_sac VARCHAR(20),
                    quantity DOUBLE PRECISION,
                    unit_price DOUBLE PRECISION,
                    taxable_value DOUBLE PRECISION,
                    gst_rate DOUBLE PRECISION,
                    cgst DOUBLE PRECISION,
                    sgst DOUBLE PRECISION,
                    igst DOUBLE PRECISION,
                    line_total DOUBLE PRECISION
                );

                CREATE TABLE IF NOT EXISTS audit_errors (
                    id SERIAL PRIMARY KEY,
                    invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS ca_action_logs (
                    id SERIAL PRIMARY KEY,
                    invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
                    ca_user VARCHAR(100),
                    action VARCHAR(50),
                    field_name VARCHAR(50),
                    old_value TEXT,
                    new_value TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS cas (
                    invite_code VARCHAR(6) PRIMARY KEY,
                    name VARCHAR(150) NOT NULL,
                    firm_name VARCHAR(150),
                    phone VARCHAR(20),
                    email VARCHAR(150),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS client_ca_links (
                    client_phone VARCHAR(20) PRIMARY KEY REFERENCES clients(phone_number),
                    ca_invite_code VARCHAR(6) REFERENCES cas(invite_code),
                    linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id VARCHAR(128) PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Seed a demo CA invite code for local/testing if table is empty
            ca_count = await conn.fetchval("SELECT COUNT(*) FROM cas")
            if ca_count == 0:
                await conn.execute(
                    """
                    INSERT INTO cas (invite_code, name, firm_name, phone, email)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    "123456", "Demo CA", "Taxova.ai Demo Firm", "919876543210", "demo.ca@taxova.ai"
                )
        else:
            # SQLite Schema (uses AUTOINCREMENT instead of SERIAL)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    phone_number TEXT PRIMARY KEY,
                    name TEXT,
                    gstin TEXT,
                    registered BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_phone TEXT REFERENCES clients(phone_number),
                    file_path TEXT,
                    supplier_name TEXT,
                    supplier_gstin TEXT,
                    recipient_name TEXT,
                    recipient_gstin TEXT,
                    invoice_number TEXT,
                    invoice_date TEXT,
                    place_of_supply TEXT,
                    total_taxable_value REAL,
                    total_cgst REAL,
                    total_sgst REAL,
                    total_igst REAL,
                    grand_total REAL,
                    business_category TEXT,
                    is_calculation_correct BOOLEAN,
                    is_itc_eligible BOOLEAN,
                    itc_ineligibility_reason TEXT,
                    supply_type TEXT,
                    is_approved BOOLEAN DEFAULT 0,
                    review_status TEXT DEFAULT 'needs_review',
                    hitl_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS line_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
                    description TEXT,
                    hsn_or_sac TEXT,
                    quantity REAL,
                    unit_price REAL,
                    taxable_value REAL,
                    gst_rate REAL,
                    cgst REAL,
                    sgst REAL,
                    igst REAL,
                    line_total REAL
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
                    error_message TEXT
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS ca_action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
                    ca_user TEXT,
                    action TEXT,
                    field_name TEXT,
                    old_value TEXT,
                    new_value TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS cas (
                    invite_code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    firm_name TEXT,
                    phone TEXT,
                    email TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS client_ca_links (
                    client_phone TEXT PRIMARY KEY REFERENCES clients(phone_number),
                    ca_invite_code TEXT REFERENCES cas(invite_code),
                    linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Seed a demo CA invite code for local/testing if table is empty
            cursor = await conn.execute("SELECT COUNT(*) FROM cas")
            ca_count = (await cursor.fetchone())[0]
            if ca_count == 0:
                await conn.execute(
                    """
                    INSERT INTO cas (invite_code, name, firm_name, phone, email)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("123456", "Demo CA", "Taxova.ai Demo Firm", "919876543210", "demo.ca@taxova.ai")
                )
            await conn.commit()
        await _ensure_hitl_columns(conn)
        await _ensure_itc_line_columns(conn)
        await _ensure_gstr2b_schema(conn)
        await _ensure_gstin_portal_cache_schema(conn)
        await _ensure_gst_taxpayer_session_schema(conn)
        await _ensure_security_schema(conn)
        await _ensure_itr_schema(conn)
        await _ensure_tenancy_schema(conn)
        await _ensure_pricing_schema(conn)
        await _ensure_extraction_edit_schema(conn)
        await _bootstrap_ca_passwords(conn)
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.exception("Error creating tables:")
        raise e
    finally:
        await conn.close()


async def _ensure_itc_line_columns(conn) -> None:
    """Add line-level ITC columns + invoice rollups (idempotent)."""
    invoice_cols = [
        ("itc_eligible_cgst", "REAL DEFAULT 0"),
        ("itc_eligible_sgst", "REAL DEFAULT 0"),
        ("itc_eligible_igst", "REAL DEFAULT 0"),
        ("itc_blocked_gst", "REAL DEFAULT 0"),
        ("itc_partial", "BOOLEAN DEFAULT 0" if not IS_POSTGRES else "BOOLEAN DEFAULT FALSE"),
        ("itc_line_evaluated", "BOOLEAN DEFAULT 0" if not IS_POSTGRES else "BOOLEAN DEFAULT FALSE"),
    ]
    line_cols = [
        ("is_itc_eligible", "BOOLEAN"),
        ("itc_ineligibility_reason", "TEXT"),
        ("itc_rule_code", "TEXT" if not IS_POSTGRES else "VARCHAR(64)"),
        ("inferred_category", "TEXT" if not IS_POSTGRES else "VARCHAR(80)"),
    ]
    for col, col_type in invoice_cols:
        try:
            if IS_POSTGRES:
                await conn.execute(
                    f"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS {col} {col_type}"
                )
            else:
                await conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {col_type}")
                await conn.commit()
        except Exception:
            pass
    for col, col_type in line_cols:
        try:
            if IS_POSTGRES:
                await conn.execute(
                    f"ALTER TABLE line_items ADD COLUMN IF NOT EXISTS {col} {col_type}"
                )
            else:
                await conn.execute(f"ALTER TABLE line_items ADD COLUMN {col} {col_type}")
                await conn.commit()
        except Exception:
            pass


async def _ensure_gstr2b_schema(conn) -> None:
    """GSTR-2B entries table + invoice match columns (idempotent)."""
    if IS_POSTGRES:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gstr2b_entries (
                id SERIAL PRIMARY KEY,
                client_phone VARCHAR(20) NOT NULL,
                return_period VARCHAR(7) NOT NULL,
                supplier_gstin VARCHAR(15),
                supplier_name TEXT,
                invoice_number TEXT,
                invoice_number_norm TEXT,
                invoice_date VARCHAR(10),
                taxable_value REAL DEFAULT 0,
                igst REAL DEFAULT 0,
                cgst REAL DEFAULT 0,
                sgst REAL DEFAULT 0,
                invoice_value REAL DEFAULT 0,
                place_of_supply VARCHAR(2),
                invoice_type VARCHAR(8) DEFAULT 'R',
                matched_invoice_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    else:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gstr2b_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_phone TEXT NOT NULL,
                return_period TEXT NOT NULL,
                supplier_gstin TEXT,
                supplier_name TEXT,
                invoice_number TEXT,
                invoice_number_norm TEXT,
                invoice_date TEXT,
                taxable_value REAL DEFAULT 0,
                igst REAL DEFAULT 0,
                cgst REAL DEFAULT 0,
                sgst REAL DEFAULT 0,
                invoice_value REAL DEFAULT 0,
                place_of_supply TEXT,
                invoice_type TEXT DEFAULT 'R',
                matched_invoice_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()

    invoice_cols = [
        ("gstr2b_match_status", "TEXT DEFAULT 'none'" if not IS_POSTGRES else "VARCHAR(16) DEFAULT 'none'"),
        ("gstr2b_entry_id", "INTEGER"),
        ("gstr2b_mismatch_reason", "TEXT"),
        ("gstr2b_matched_at", "TEXT" if not IS_POSTGRES else "TIMESTAMP"),
    ]
    for col, col_type in invoice_cols:
        try:
            if IS_POSTGRES:
                await conn.execute(
                    f"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS {col} {col_type}"
                )
            else:
                await conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {col_type}")
                await conn.commit()
        except Exception:
            pass


async def _ensure_gstin_portal_cache_schema(conn) -> None:
    """Public GSTIN search cache (24h TTL enforced in app code)."""
    if IS_POSTGRES:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gstin_portal_cache (
                gstin VARCHAR(15) PRIMARY KEY,
                found BOOLEAN,
                status VARCHAR(64),
                legal_name TEXT,
                trade_name TEXT,
                taxpayer_type VARCHAR(64),
                profile_json TEXT,
                error_message TEXT,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    else:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gstin_portal_cache (
                gstin TEXT PRIMARY KEY,
                found INTEGER,
                status TEXT,
                legal_name TEXT,
                trade_name TEXT,
                taxpayer_type TEXT,
                profile_json TEXT,
                error_message TEXT,
                checked_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()


def _portal_cache_row_to_dict(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    raw = d.get("profile_json")
    profile = None
    if raw:
        try:
            profile = json.loads(raw)
        except Exception:
            profile = None
    d["profile"] = profile
    return d


async def get_gstin_portal_cache(gstin: str) -> dict | None:
    g = (gstin or "").strip().upper()
    if not g:
        return None
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow("SELECT * FROM gstin_portal_cache WHERE gstin = $1", g)
        else:
            cur = await conn.execute("SELECT * FROM gstin_portal_cache WHERE gstin = ?", (g,))
            row = await cur.fetchone()
        return _portal_cache_row_to_dict(row)
    finally:
        await conn.close()


async def get_gstin_portal_cache_many(gstins: list[str]) -> dict[str, dict]:
    """Return map gstin -> cache row for requested GSTINs."""
    cleaned = sorted({(g or "").strip().upper() for g in gstins if (g or "").strip()})
    if not cleaned:
        return {}
    conn = await get_connection()
    out: dict[str, dict] = {}
    try:
        if IS_POSTGRES:
            rows = await conn.fetch(
                "SELECT * FROM gstin_portal_cache WHERE gstin = ANY($1::text[])",
                cleaned,
            )
            for row in rows:
                d = _portal_cache_row_to_dict(row)
                if d:
                    out[d["gstin"]] = d
        else:
            placeholders = ",".join("?" * len(cleaned))
            cur = await conn.execute(
                f"SELECT * FROM gstin_portal_cache WHERE gstin IN ({placeholders})",
                cleaned,
            )
            rows = await cur.fetchall()
            for row in rows:
                d = _portal_cache_row_to_dict(row)
                if d:
                    out[d["gstin"]] = d
        return out
    finally:
        await conn.close()


async def upsert_gstin_portal_cache(
    gstin: str,
    *,
    profile: dict | None = None,
    error_message: str | None = None,
) -> dict:
    """Insert/update portal cache row. Returns stored row dict."""
    from datetime import datetime, timezone

    g = (gstin or "").strip().upper()
    found = bool(profile and profile.get("found"))
    status = (profile or {}).get("status")
    legal_name = (profile or {}).get("legal_name")
    trade_name = (profile or {}).get("trade_name")
    taxpayer_type = (profile or {}).get("taxpayer_type")
    profile_json = json.dumps(profile) if profile else None
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                INSERT INTO gstin_portal_cache (
                    gstin, found, status, legal_name, trade_name, taxpayer_type,
                    profile_json, error_message, checked_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (gstin) DO UPDATE SET
                    found = EXCLUDED.found,
                    status = EXCLUDED.status,
                    legal_name = EXCLUDED.legal_name,
                    trade_name = EXCLUDED.trade_name,
                    taxpayer_type = EXCLUDED.taxpayer_type,
                    profile_json = EXCLUDED.profile_json,
                    error_message = EXCLUDED.error_message,
                    checked_at = EXCLUDED.checked_at
                """,
                g, found, status, legal_name, trade_name, taxpayer_type,
                profile_json, error_message, checked_at,
            )
        else:
            await conn.execute(
                """
                INSERT INTO gstin_portal_cache (
                    gstin, found, status, legal_name, trade_name, taxpayer_type,
                    profile_json, error_message, checked_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(gstin) DO UPDATE SET
                    found = excluded.found,
                    status = excluded.status,
                    legal_name = excluded.legal_name,
                    trade_name = excluded.trade_name,
                    taxpayer_type = excluded.taxpayer_type,
                    profile_json = excluded.profile_json,
                    error_message = excluded.error_message,
                    checked_at = excluded.checked_at
                """,
                (
                    g, 1 if found else 0, status, legal_name, trade_name, taxpayer_type,
                    profile_json, error_message, checked_at,
                ),
            )
            await conn.commit()
        return {
            "gstin": g,
            "found": found,
            "status": status,
            "legal_name": legal_name,
            "trade_name": trade_name,
            "taxpayer_type": taxpayer_type,
            "profile": profile,
            "error_message": error_message,
            "checked_at": checked_at,
        }
    finally:
        await conn.close()


async def _ensure_gst_taxpayer_session_schema(conn) -> None:
    """OTP taxpayer session + optional GST portal username on clients."""
    if IS_POSTGRES:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gst_taxpayer_sessions (
                client_phone VARCHAR(20) PRIMARY KEY,
                gstin VARCHAR(15) NOT NULL,
                username TEXT NOT NULL,
                access_token TEXT NOT NULL,
                token_expiry BIGINT,
                session_expiry BIGINT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            await conn.execute(
                "ALTER TABLE clients ADD COLUMN IF NOT EXISTS gst_portal_username TEXT"
            )
        except Exception:
            pass
    else:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gst_taxpayer_sessions (
                client_phone TEXT PRIMARY KEY,
                gstin TEXT NOT NULL,
                username TEXT NOT NULL,
                access_token TEXT NOT NULL,
                token_expiry INTEGER,
                session_expiry INTEGER,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()
        try:
            await conn.execute("ALTER TABLE clients ADD COLUMN gst_portal_username TEXT")
            await conn.commit()
        except Exception:
            pass


async def _ensure_security_schema(conn) -> None:
    """CA passwords, sessions, and security audit log (idempotent)."""
    # password columns on cas
    for col, col_type in (
        ("password_salt", "TEXT"),
        ("password_hash", "TEXT"),
    ):
        try:
            if IS_POSTGRES:
                await conn.execute(
                    f"ALTER TABLE cas ADD COLUMN IF NOT EXISTS {col} {col_type}"
                )
            else:
                await conn.execute(f"ALTER TABLE cas ADD COLUMN {col} {col_type}")
                await conn.commit()
        except Exception:
            pass

    if IS_POSTGRES:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ca_sessions (
                token_hash VARCHAR(64) PRIMARY KEY,
                ca_invite_code VARCHAR(6) NOT NULL REFERENCES cas(invite_code),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_audit_logs (
                id SERIAL PRIMARY KEY,
                actor VARCHAR(150),
                action VARCHAR(64) NOT NULL,
                resource_type VARCHAR(64),
                resource_id TEXT,
                detail TEXT,
                ip VARCHAR(64),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    else:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ca_sessions (
                token_hash TEXT PRIMARY KEY,
                ca_invite_code TEXT NOT NULL REFERENCES cas(invite_code),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT,
                action TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                detail TEXT,
                ip TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()


async def _bootstrap_ca_passwords(conn) -> None:
    """Set bootstrap password for CAs that have none (from CA_BOOTSTRAP_PASSWORD)."""
    import security as security_mod

    pwd = os.getenv("CA_BOOTSTRAP_PASSWORD", "Taxova@ChangeMe").strip()
    if not pwd:
        return
    salt_b64, hash_b64 = security_mod.hash_password(pwd)
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                UPDATE cas
                SET password_salt = $1, password_hash = $2
                WHERE password_hash IS NULL OR password_hash = ''
                """,
                salt_b64,
                hash_b64,
            )
        else:
            await conn.execute(
                """
                UPDATE cas
                SET password_salt = ?, password_hash = ?
                WHERE password_hash IS NULL OR password_hash = ''
                """,
                (salt_b64, hash_b64),
            )
            await conn.commit()
        logger.info("CA bootstrap passwords applied where missing (change CA_BOOTSTRAP_PASSWORD in prod).")
    except Exception:
        logger.exception("Could not bootstrap CA passwords")


def _slugify_firm_name(name: str) -> str:
    import re

    base = re.sub(r"[^a-z0-9]+", "-", (name or "firm").lower()).strip("-") or "firm"
    return base[:48]


async def _ensure_tenancy_schema(conn) -> None:
    """
    Multi-tenant firms layer: firms table + firm_id on cas/clients/invoices/etc.
    Backfills existing rows into a default demo firm.
    """
    if IS_POSTGRES:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS firms (
                id SERIAL PRIMARY KEY,
                name VARCHAR(150) NOT NULL,
                slug VARCHAR(64) UNIQUE NOT NULL,
                status VARCHAR(32) DEFAULT 'active',
                billing_model VARCHAR(32) DEFAULT 'per_firm',
                plan_tier VARCHAR(32),
                seat_limit INTEGER,
                monthly_invoice_cap INTEGER,
                client_cap INTEGER,
                billing_status VARCHAR(32) DEFAULT 'trial',
                trial_ends_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    else:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS firms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'active',
                billing_model TEXT DEFAULT 'per_firm',
                plan_tier TEXT,
                seat_limit INTEGER,
                monthly_invoice_cap INTEGER,
                client_cap INTEGER,
                billing_status TEXT DEFAULT 'trial',
                trial_ends_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()

    for table in (
        "cas",
        "clients",
        "invoices",
        "gstr2b_entries",
        "gst_taxpayer_sessions",
        "itr_returns",
    ):
        try:
            if IS_POSTGRES:
                await conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS firm_id INTEGER"
                )
            else:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN firm_id INTEGER")
                await conn.commit()
        except Exception:
            pass

    if IS_POSTGRES:
        firm_count = await conn.fetchval("SELECT COUNT(*) FROM firms")
    else:
        cur = await conn.execute("SELECT COUNT(*) FROM firms")
        firm_count = (await cur.fetchone())[0]

    default_firm_id = None
    if not firm_count:
        demo_name = "Taxova Demo Firm"
        try:
            if IS_POSTGRES:
                row = await conn.fetchrow(
                    "SELECT firm_name FROM cas WHERE invite_code = $1", "123456"
                )
                if row and row.get("firm_name"):
                    demo_name = str(row["firm_name"])
            else:
                cur = await conn.execute(
                    "SELECT firm_name FROM cas WHERE invite_code = ?", ("123456",)
                )
                row = await cur.fetchone()
                if row and row[0]:
                    demo_name = str(row[0])
        except Exception:
            pass
        slug = _slugify_firm_name(demo_name)
        if IS_POSTGRES:
            default_firm_id = await conn.fetchval(
                """
                INSERT INTO firms (name, slug, status)
                VALUES ($1, $2, 'active')
                ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                demo_name,
                slug,
            )
        else:
            try:
                await conn.execute(
                    "INSERT INTO firms (name, slug, status) VALUES (?, ?, 'active')",
                    (demo_name, slug),
                )
                await conn.commit()
            except Exception:
                pass
            cur = await conn.execute("SELECT id FROM firms WHERE slug = ?", (slug,))
            r = await cur.fetchone()
            default_firm_id = r[0] if r else None
        logger.info("Seeded default firm id=%s name=%s", default_firm_id, demo_name)
    else:
        if IS_POSTGRES:
            default_firm_id = await conn.fetchval(
                "SELECT id FROM firms ORDER BY id ASC LIMIT 1"
            )
        else:
            cur = await conn.execute("SELECT id FROM firms ORDER BY id ASC LIMIT 1")
            r = await cur.fetchone()
            default_firm_id = r[0] if r else None

    if default_firm_id is None:
        return

    if IS_POSTGRES:
        await conn.execute(
            "UPDATE cas SET firm_id = $1 WHERE firm_id IS NULL",
            default_firm_id,
        )
        await conn.execute(
            """
            UPDATE clients c
            SET firm_id = ca.firm_id
            FROM client_ca_links l
            JOIN cas ca ON ca.invite_code = l.ca_invite_code
            WHERE c.phone_number = l.client_phone
              AND c.firm_id IS NULL
              AND ca.firm_id IS NOT NULL
            """
        )
        await conn.execute(
            "UPDATE clients SET firm_id = $1 WHERE firm_id IS NULL",
            default_firm_id,
        )
        await conn.execute(
            """
            UPDATE invoices i
            SET firm_id = c.firm_id
            FROM clients c
            WHERE i.client_phone = c.phone_number
              AND i.firm_id IS NULL
              AND c.firm_id IS NOT NULL
            """
        )
        await conn.execute(
            "UPDATE invoices SET firm_id = $1 WHERE firm_id IS NULL",
            default_firm_id,
        )
        await conn.execute(
            """
            UPDATE gstr2b_entries g
            SET firm_id = c.firm_id
            FROM clients c
            WHERE g.client_phone = c.phone_number
              AND g.firm_id IS NULL
              AND c.firm_id IS NOT NULL
            """
        )
        await conn.execute(
            """
            UPDATE gst_taxpayer_sessions s
            SET firm_id = c.firm_id
            FROM clients c
            WHERE s.client_phone = c.phone_number
              AND s.firm_id IS NULL
              AND c.firm_id IS NOT NULL
            """
        )
        await conn.execute(
            """
            UPDATE itr_returns r
            SET firm_id = c.firm_id
            FROM clients c
            WHERE r.client_phone = c.phone_number
              AND r.firm_id IS NULL
              AND c.firm_id IS NOT NULL
            """
        )
    else:
        await conn.execute(
            "UPDATE cas SET firm_id = ? WHERE firm_id IS NULL",
            (default_firm_id,),
        )
        await conn.execute(
            """
            UPDATE clients
            SET firm_id = (
                SELECT ca.firm_id FROM client_ca_links l
                JOIN cas ca ON ca.invite_code = l.ca_invite_code
                WHERE l.client_phone = clients.phone_number
                LIMIT 1
            )
            WHERE firm_id IS NULL
              AND phone_number IN (SELECT client_phone FROM client_ca_links)
            """
        )
        await conn.execute(
            "UPDATE clients SET firm_id = ? WHERE firm_id IS NULL",
            (default_firm_id,),
        )
        await conn.execute(
            """
            UPDATE invoices
            SET firm_id = (
                SELECT c.firm_id FROM clients c
                WHERE c.phone_number = invoices.client_phone
            )
            WHERE firm_id IS NULL
            """
        )
        await conn.execute(
            "UPDATE invoices SET firm_id = ? WHERE firm_id IS NULL",
            (default_firm_id,),
        )
        for table in ("gstr2b_entries", "gst_taxpayer_sessions", "itr_returns"):
            try:
                await conn.execute(
                    f"""
                    UPDATE {table}
                    SET firm_id = (
                        SELECT c.firm_id FROM clients c
                        WHERE c.phone_number = {table}.client_phone
                    )
                    WHERE firm_id IS NULL
                    """
                )
            except Exception:
                pass
        await conn.commit()


async def _ensure_pricing_schema(conn) -> None:
    """
    Nullable billing columns on firms — shape decided as per-firm flat by default,
    with optional seat / invoice / client caps for later enforcement.
    See docs/PRICING_MODEL.md.
    """
    cols = [
        ("billing_model", "VARCHAR(32)" if IS_POSTGRES else "TEXT", "'per_firm'"),
        ("plan_tier", "VARCHAR(32)" if IS_POSTGRES else "TEXT", "NULL"),
        ("seat_limit", "INTEGER", "NULL"),
        ("monthly_invoice_cap", "INTEGER", "NULL"),
        ("client_cap", "INTEGER", "NULL"),
        ("billing_status", "VARCHAR(32)" if IS_POSTGRES else "TEXT", "'trial'"),
        ("trial_ends_at", "TIMESTAMP" if IS_POSTGRES else "TEXT", "NULL"),
    ]
    for name, typ, _default in cols:
        try:
            if IS_POSTGRES:
                await conn.execute(
                    f"ALTER TABLE firms ADD COLUMN IF NOT EXISTS {name} {typ}"
                )
            else:
                await conn.execute(f"ALTER TABLE firms ADD COLUMN {name} {typ}")
                await conn.commit()
        except Exception:
            pass
    # Backfill billing_model / billing_status for existing firms
    try:
        if IS_POSTGRES:
            await conn.execute(
                "UPDATE firms SET billing_model = 'per_firm' WHERE billing_model IS NULL"
            )
            await conn.execute(
                "UPDATE firms SET billing_status = 'trial' WHERE billing_status IS NULL"
            )
        else:
            await conn.execute(
                "UPDATE firms SET billing_model = 'per_firm' WHERE billing_model IS NULL"
            )
            await conn.execute(
                "UPDATE firms SET billing_status = 'trial' WHERE billing_status IS NULL"
            )
            await conn.commit()
    except Exception:
        pass


# Fields that come from AI extraction — CA edits here = extraction error signal
EXTRACTION_TRACKED_FIELDS = frozenset(
    {
        "supplier_name",
        "supplier_gstin",
        "recipient_name",
        "recipient_gstin",
        "invoice_number",
        "invoice_date",
        "place_of_supply",
        "total_taxable_value",
        "total_cgst",
        "total_sgst",
        "total_igst",
        "grand_total",
        "business_category",
        "supply_type",
    }
)

# Filing-critical fields for pilot go/hold (docs/PILOT_CRITERIA.md)
PILOT_FILING_CRITICAL_FIELDS = frozenset(
    {
        "supplier_gstin",
        "recipient_gstin",
        "invoice_number",
        "invoice_date",
        "total_taxable_value",
        "total_cgst",
        "total_sgst",
        "total_igst",
        "grand_total",
    }
)
# Defaults match docs/PILOT_CRITERIA.md. Override locally for demos, e.g. PILOT_MIN_APPROVALS=5
PILOT_MIN_APPROVALS = max(1, int(os.getenv("PILOT_MIN_APPROVALS", "25") or 25))
PILOT_CHECK_IN_AT = max(
    1, min(PILOT_MIN_APPROVALS, int(os.getenv("PILOT_CHECK_IN_AT", "10") or 10))
)
PILOT_EDIT_RATE_HOLD = float(os.getenv("PILOT_EDIT_RATE_HOLD", "0.25") or 0.25)
PILOT_FIELD_RATE_HOLD = float(os.getenv("PILOT_FIELD_RATE_HOLD", "0.15") or 0.15)


async def _ensure_extraction_edit_schema(conn) -> None:
    """Production signal: every CA edit of an extraction field before approve."""
    if IS_POSTGRES:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS extraction_field_edits (
                id SERIAL PRIMARY KEY,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                firm_id INTEGER,
                client_phone VARCHAR(20),
                field_name VARCHAR(64) NOT NULL,
                old_value TEXT,
                new_value TEXT,
                ca_user VARCHAR(100),
                ca_invite_code VARCHAR(6),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_extraction_outcomes (
                invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE,
                firm_id INTEGER,
                client_phone VARCHAR(20),
                fields_edited_count INTEGER DEFAULT 0,
                distinct_fields TEXT,
                had_any_edit BOOLEAN DEFAULT FALSE,
                ca_user VARCHAR(100),
                ca_invite_code VARCHAR(6),
                approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for col, typ in (
            ("extraction_edit_count", "INTEGER DEFAULT 0"),
            ("extraction_edited_fields", "TEXT"),
        ):
            try:
                await conn.execute(
                    f"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS {col} {typ}"
                )
            except Exception:
                pass
    else:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS extraction_field_edits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                firm_id INTEGER,
                client_phone TEXT,
                field_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                ca_user TEXT,
                ca_invite_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_extraction_outcomes (
                invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE,
                firm_id INTEGER,
                client_phone TEXT,
                fields_edited_count INTEGER DEFAULT 0,
                distinct_fields TEXT,
                had_any_edit INTEGER DEFAULT 0,
                ca_user TEXT,
                ca_invite_code TEXT,
                approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()
        for col, typ in (
            ("extraction_edit_count", "INTEGER DEFAULT 0"),
            ("extraction_edited_fields", "TEXT"),
        ):
            try:
                await conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {typ}")
                await conn.commit()
            except Exception:
                pass


async def get_firm(firm_id: int) -> dict | None:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow("SELECT * FROM firms WHERE id = $1", firm_id)
            return dict(row) if row else None
        conn.row_factory = sqlite3.Row
        cur = await conn.execute("SELECT * FROM firms WHERE id = ?", (firm_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def create_firm(name: str, slug: str | None = None) -> dict:
    """Create a firm tenant. Raises ValueError on slug conflict."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Firm name is required")
    clean_slug = _slugify_firm_name(slug or clean_name)
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO firms (
                        name, slug, status, billing_model, billing_status
                    )
                    VALUES ($1, $2, 'active', 'per_firm', 'trial')
                    RETURNING *
                    """,
                    clean_name,
                    clean_slug,
                )
                return dict(row)
            except Exception as e:
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    raise ValueError(f"Firm slug already exists: {clean_slug}") from e
                raise
        try:
            await conn.execute(
                """
                INSERT INTO firms (name, slug, status, billing_model, billing_status)
                VALUES (?, ?, 'active', 'per_firm', 'trial')
                """,
                (clean_name, clean_slug),
            )
            await conn.commit()
        except Exception as e:
            if "unique" in str(e).lower():
                raise ValueError(f"Firm slug already exists: {clean_slug}") from e
            raise
        conn.row_factory = sqlite3.Row
        cur = await conn.execute("SELECT * FROM firms WHERE slug = ?", (clean_slug,))
        return dict(await cur.fetchone())
    finally:
        await conn.close()


async def create_ca(
    *,
    invite_code: str,
    name: str,
    firm_id: int,
    firm_name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    password: str | None = None,
) -> dict:
    """Create a CA under a firm. Sets password if provided."""
    import security as security_mod

    code = str(invite_code or "").strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError("invite_code must be 6 digits")
    ca_name = (name or "").strip()
    if not ca_name:
        raise ValueError("CA name is required")
    firm = await get_firm(firm_id)
    if not firm:
        raise ValueError("Firm not found")
    display_firm = (firm_name or firm.get("name") or "").strip()

    salt_b64 = hash_b64 = None
    if password:
        salt_b64, hash_b64 = security_mod.hash_password(password)

    conn = await get_connection()
    try:
        if IS_POSTGRES:
            existing = await conn.fetchrow(
                "SELECT invite_code FROM cas WHERE invite_code = $1", code
            )
            if existing:
                raise ValueError("Invite code already exists")
            await conn.execute(
                """
                INSERT INTO cas (
                    invite_code, name, firm_name, phone, email, firm_id,
                    password_salt, password_hash
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                code,
                ca_name,
                display_firm,
                phone,
                email,
                firm_id,
                salt_b64,
                hash_b64,
            )
        else:
            cur = await conn.execute(
                "SELECT invite_code FROM cas WHERE invite_code = ?", (code,)
            )
            if await cur.fetchone():
                raise ValueError("Invite code already exists")
            await conn.execute(
                """
                INSERT INTO cas (
                    invite_code, name, firm_name, phone, email, firm_id,
                    password_salt, password_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    ca_name,
                    display_firm,
                    phone,
                    email,
                    firm_id,
                    salt_b64,
                    hash_b64,
                ),
            )
            await conn.commit()
        return await get_ca_by_invite_code(code)
    finally:
        await conn.close()


async def set_client_firm(client_phone: str, firm_id: int | None) -> None:
    """Set client firm and denormalize onto that client's related rows."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                "UPDATE clients SET firm_id = $1 WHERE phone_number = $2",
                firm_id,
                client_phone,
            )
            await conn.execute(
                "UPDATE invoices SET firm_id = $1 WHERE client_phone = $2",
                firm_id,
                client_phone,
            )
            await conn.execute(
                "UPDATE gstr2b_entries SET firm_id = $1 WHERE client_phone = $2",
                firm_id,
                client_phone,
            )
            await conn.execute(
                "UPDATE gst_taxpayer_sessions SET firm_id = $1 WHERE client_phone = $2",
                firm_id,
                client_phone,
            )
            await conn.execute(
                "UPDATE itr_returns SET firm_id = $1 WHERE client_phone = $2",
                firm_id,
                client_phone,
            )
        else:
            await conn.execute(
                "UPDATE clients SET firm_id = ? WHERE phone_number = ?",
                (firm_id, client_phone),
            )
            await conn.execute(
                "UPDATE invoices SET firm_id = ? WHERE client_phone = ?",
                (firm_id, client_phone),
            )
            for table in ("gstr2b_entries", "gst_taxpayer_sessions", "itr_returns"):
                try:
                    await conn.execute(
                        f"UPDATE {table} SET firm_id = ? WHERE client_phone = ?",
                        (firm_id, client_phone),
                    )
                except Exception:
                    pass
            await conn.commit()
    finally:
        await conn.close()


async def client_in_firm(client_phone: str, firm_id: int) -> bool:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                "SELECT 1 FROM clients WHERE phone_number = $1 AND firm_id = $2",
                client_phone,
                firm_id,
            )
            return row is not None
        cur = await conn.execute(
            "SELECT 1 FROM clients WHERE phone_number = ? AND firm_id = ?",
            (client_phone, firm_id),
        )
        return (await cur.fetchone()) is not None
    finally:
        await conn.close()


def _session_row_public(row: dict) -> dict:
    """Session status without exposing the access token."""
    from datetime import datetime, timezone

    token_expiry = row.get("token_expiry")
    session_expiry = row.get("session_expiry")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    active = False
    try:
        if token_expiry and int(token_expiry) > now_ms:
            active = True
    except (TypeError, ValueError):
        active = bool(row.get("access_token"))
    return {
        "client_phone": row.get("client_phone"),
        "gstin": row.get("gstin"),
        "username": row.get("username"),
        "token_expiry": token_expiry,
        "session_expiry": session_expiry,
        "updated_at": row.get("updated_at"),
        "active": active,
    }


async def get_gst_taxpayer_session(client_phone: str, *, include_token: bool = False) -> dict | None:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                "SELECT * FROM gst_taxpayer_sessions WHERE client_phone = $1",
                client_phone,
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM gst_taxpayer_sessions WHERE client_phone = ?",
                (client_phone,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        if include_token:
            pub = _session_row_public(d)
            pub["access_token"] = d.get("access_token")
            return pub
        return _session_row_public(d)
    finally:
        await conn.close()


async def upsert_gst_taxpayer_session(
    client_phone: str,
    *,
    gstin: str,
    username: str,
    access_token: str,
    token_expiry: int | None = None,
    session_expiry: int | None = None,
) -> dict:
    from datetime import datetime, timezone

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    g = (gstin or "").strip().upper()
    user = (username or "").strip()
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                INSERT INTO gst_taxpayer_sessions (
                    client_phone, gstin, username, access_token,
                    token_expiry, session_expiry, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (client_phone) DO UPDATE SET
                    gstin = EXCLUDED.gstin,
                    username = EXCLUDED.username,
                    access_token = EXCLUDED.access_token,
                    token_expiry = EXCLUDED.token_expiry,
                    session_expiry = EXCLUDED.session_expiry,
                    updated_at = EXCLUDED.updated_at
                """,
                client_phone, g, user, access_token,
                token_expiry, session_expiry, updated_at,
            )
            # Remember username on client for next OTP
            await conn.execute(
                "UPDATE clients SET gst_portal_username = $1 WHERE phone_number = $2",
                user, client_phone,
            )
        else:
            await conn.execute(
                """
                INSERT INTO gst_taxpayer_sessions (
                    client_phone, gstin, username, access_token,
                    token_expiry, session_expiry, updated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(client_phone) DO UPDATE SET
                    gstin = excluded.gstin,
                    username = excluded.username,
                    access_token = excluded.access_token,
                    token_expiry = excluded.token_expiry,
                    session_expiry = excluded.session_expiry,
                    updated_at = excluded.updated_at
                """,
                (client_phone, g, user, access_token, token_expiry, session_expiry, updated_at),
            )
            try:
                await conn.execute(
                    "UPDATE clients SET gst_portal_username = ? WHERE phone_number = ?",
                    (user, client_phone),
                )
            except Exception:
                pass
            await conn.commit()
        return await get_gst_taxpayer_session(client_phone) or {
            "client_phone": client_phone,
            "gstin": g,
            "username": user,
            "active": True,
            "token_expiry": token_expiry,
            "session_expiry": session_expiry,
        }
    finally:
        await conn.close()


async def clear_gst_taxpayer_session(client_phone: str) -> None:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                "DELETE FROM gst_taxpayer_sessions WHERE client_phone = $1",
                client_phone,
            )
        else:
            await conn.execute(
                "DELETE FROM gst_taxpayer_sessions WHERE client_phone = ?",
                (client_phone,),
            )
            await conn.commit()
    finally:
        await conn.close()


async def replace_gstr2b_entries(
    client_phone: str,
    return_period: str,
    entries: list[dict],
) -> list[dict]:
    """Replace all 2B rows for a client+period; return inserted rows with ids."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                "DELETE FROM gstr2b_entries WHERE client_phone = $1 AND return_period = $2",
                client_phone, return_period,
            )
            # Clear prior match links for this period's invoices (best-effort)
            await conn.execute(
                """
                UPDATE invoices SET gstr2b_match_status = 'none', gstr2b_entry_id = NULL,
                    gstr2b_mismatch_reason = NULL, gstr2b_matched_at = NULL
                WHERE client_phone = $1 AND substr(invoice_date, 1, 7) = $2
                """,
                client_phone, return_period,
            )
            inserted = []
            for e in entries:
                row = await conn.fetchrow(
                    """
                    INSERT INTO gstr2b_entries (
                        client_phone, return_period, supplier_gstin, supplier_name,
                        invoice_number, invoice_number_norm, invoice_date,
                        taxable_value, igst, cgst, sgst, invoice_value,
                        place_of_supply, invoice_type
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    RETURNING *
                    """,
                    client_phone, return_period,
                    e.get("supplier_gstin"), e.get("supplier_name"),
                    e.get("invoice_number"), e.get("invoice_number_norm"),
                    e.get("invoice_date"),
                    float(e.get("taxable_value") or 0),
                    float(e.get("igst") or 0), float(e.get("cgst") or 0),
                    float(e.get("sgst") or 0), float(e.get("invoice_value") or 0),
                    e.get("place_of_supply"), e.get("invoice_type") or "R",
                )
                inserted.append(dict(row))
            return inserted
        else:
            await conn.execute(
                "DELETE FROM gstr2b_entries WHERE client_phone = ? AND return_period = ?",
                (client_phone, return_period),
            )
            await conn.execute(
                """
                UPDATE invoices SET gstr2b_match_status = 'none', gstr2b_entry_id = NULL,
                    gstr2b_mismatch_reason = NULL, gstr2b_matched_at = NULL
                WHERE client_phone = ? AND substr(invoice_date, 1, 7) = ?
                """,
                (client_phone, return_period),
            )
            inserted = []
            for e in entries:
                cur = await conn.execute(
                    """
                    INSERT INTO gstr2b_entries (
                        client_phone, return_period, supplier_gstin, supplier_name,
                        invoice_number, invoice_number_norm, invoice_date,
                        taxable_value, igst, cgst, sgst, invoice_value,
                        place_of_supply, invoice_type
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        client_phone, return_period,
                        e.get("supplier_gstin"), e.get("supplier_name"),
                        e.get("invoice_number"), e.get("invoice_number_norm"),
                        e.get("invoice_date"),
                        float(e.get("taxable_value") or 0),
                        float(e.get("igst") or 0), float(e.get("cgst") or 0),
                        float(e.get("sgst") or 0), float(e.get("invoice_value") or 0),
                        e.get("place_of_supply"), e.get("invoice_type") or "R",
                    ),
                )
                eid = cur.lastrowid
                inserted.append({**e, "id": eid, "client_phone": client_phone, "return_period": return_period})
            await conn.commit()
            return inserted
    finally:
        await conn.close()


async def get_gstr2b_entries(client_phone: str, return_period: str | None = None) -> list[dict]:
    conn = await get_connection()
    try:
        if return_period:
            if IS_POSTGRES:
                rows = await conn.fetch(
                    "SELECT * FROM gstr2b_entries WHERE client_phone = $1 AND return_period = $2 ORDER BY id",
                    client_phone, return_period,
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM gstr2b_entries WHERE client_phone = ? AND return_period = ? ORDER BY id",
                    (client_phone, return_period),
                )
                rows = await cur.fetchall()
        else:
            if IS_POSTGRES:
                rows = await conn.fetch(
                    "SELECT * FROM gstr2b_entries WHERE client_phone = $1 ORDER BY return_period DESC, id",
                    client_phone,
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM gstr2b_entries WHERE client_phone = ? ORDER BY return_period DESC, id",
                    (client_phone,),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def apply_gstr2b_reconcile_results(results: list[dict]) -> int:
    """Persist per-invoice 2B match fields. Returns updated count."""
    from datetime import datetime as _dt

    conn = await get_connection()
    now = _dt.utcnow().isoformat(timespec="seconds") + "Z"
    updated = 0
    try:
        for r in results:
            status = r.get("gstr2b_match_status") or "none"
            eid = r.get("gstr2b_entry_id")
            reason = r.get("gstr2b_mismatch_reason")
            iid = r["invoice_id"]
            if IS_POSTGRES:
                await conn.execute(
                    """
                    UPDATE invoices SET
                        gstr2b_match_status = $1,
                        gstr2b_entry_id = $2,
                        gstr2b_mismatch_reason = $3,
                        gstr2b_matched_at = $4
                    WHERE id = $5
                    """,
                    status, eid, reason, now, iid,
                )
                if eid and status in ("matched", "mismatch"):
                    await conn.execute(
                        "UPDATE gstr2b_entries SET matched_invoice_id = $1 WHERE id = $2",
                        iid, eid,
                    )
            else:
                await conn.execute(
                    """
                    UPDATE invoices SET
                        gstr2b_match_status = ?,
                        gstr2b_entry_id = ?,
                        gstr2b_mismatch_reason = ?,
                        gstr2b_matched_at = ?
                    WHERE id = ?
                    """,
                    (status, eid, reason, now, iid),
                )
                if eid and status in ("matched", "mismatch"):
                    await conn.execute(
                        "UPDATE gstr2b_entries SET matched_invoice_id = ? WHERE id = ?",
                        (iid, eid),
                    )
            updated += 1
        if not IS_POSTGRES:
            await conn.commit()
        return updated
    finally:
        await conn.close()


async def reconcile_gstr2b_for_client(client_phone: str, return_period: str) -> dict:
    """Load 2B + purchase invoices for period, match, persist."""
    from gstr2b import reconcile_purchase_invoices
    from itc_rules import normalize_gstin, validate_gstin

    client = await get_or_create_client(client_phone)
    gstin = normalize_gstin(client.get("gstin"))
    if not validate_gstin(gstin):
        return {
            "ok": False,
            "error": "Client needs a valid GSTIN before GSTR-2B reconciliation.",
            "return_period": return_period,
        }

    entries = await get_gstr2b_entries(client_phone, return_period)
    if not entries:
        return {
            "ok": False,
            "error": f"No GSTR-2B entries imported for {return_period}.",
            "return_period": return_period,
            "imported": 0,
        }

    invoices = await get_invoices(client_phone=client_phone, month=return_period)
    summary = reconcile_purchase_invoices(invoices, entries, client_gstin=gstin)
    updated = await apply_gstr2b_reconcile_results(summary["invoice_results"])

    # Clear matched_invoice_id on orphans
    conn = await get_connection()
    try:
        matched_ids = {
            r["gstr2b_entry_id"]
            for r in summary["invoice_results"]
            if r.get("gstr2b_entry_id") and r.get("gstr2b_match_status") in ("matched", "mismatch")
        }
        if IS_POSTGRES:
            rows = await conn.fetch(
                "SELECT id FROM gstr2b_entries WHERE client_phone = $1 AND return_period = $2",
                client_phone, return_period,
            )
            for row in rows:
                if row["id"] not in matched_ids:
                    await conn.execute(
                        "UPDATE gstr2b_entries SET matched_invoice_id = NULL WHERE id = $1",
                        row["id"],
                    )
        else:
            cur = await conn.execute(
                "SELECT id FROM gstr2b_entries WHERE client_phone = ? AND return_period = ?",
                (client_phone, return_period),
            )
            rows = await cur.fetchall()
            for row in rows:
                rid = dict(row)["id"]
                if rid not in matched_ids:
                    await conn.execute(
                        "UPDATE gstr2b_entries SET matched_invoice_id = NULL WHERE id = ?",
                        (rid,),
                    )
            await conn.commit()
    finally:
        await conn.close()

    metrics = await get_monthly_metrics(client_phone, return_period)
    return {
        "ok": True,
        "return_period": return_period,
        "imported": len(entries),
        "updated_invoices": updated,
        "counts": summary["counts"],
        "orphan_2b_count": summary["orphan_2b_count"],
        "orphans": [
            {
                "id": o.get("id"),
                "supplier_gstin": o.get("supplier_gstin"),
                "invoice_number": o.get("invoice_number"),
                "invoice_date": o.get("invoice_date"),
                "taxable_value": o.get("taxable_value"),
            }
            for o in summary.get("orphans") or []
        ],
        "metrics": metrics,
    }


async def _ensure_hitl_columns(conn) -> None:
    """Add HITL columns to existing databases (idempotent)."""
    alter_stmts = [
        ("review_status", "TEXT DEFAULT 'needs_review'" if not IS_POSTGRES else "VARCHAR(32) DEFAULT 'needs_review'"),
        ("hitl_reason", "TEXT"),
    ]
    for col, col_type in alter_stmts:
        try:
            if IS_POSTGRES:
                await conn.execute(
                    f"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS {col} {col_type}"
                )
            else:
                await conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {col_type}")
                await conn.commit()
        except Exception:
            # Column already exists
            pass

    # Backfill statuses for legacy rows
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                UPDATE invoices SET review_status = 'approved'
                WHERE is_approved = TRUE AND (review_status IS NULL OR review_status = '' OR review_status = 'needs_review')
                """
            )
            await conn.execute(
                """
                UPDATE invoices SET review_status = 'needs_review'
                WHERE is_approved = FALSE AND (review_status IS NULL OR review_status = '')
                """
            )
        else:
            await conn.execute(
                """
                UPDATE invoices SET review_status = 'approved'
                WHERE is_approved = 1 AND (review_status IS NULL OR review_status = '' OR review_status = 'needs_review')
                  AND id IN (SELECT id FROM invoices WHERE is_approved = 1)
                """
            )
            # Simpler backfill
            await conn.execute(
                "UPDATE invoices SET review_status = 'approved' WHERE is_approved = 1 AND review_status = 'needs_review'"
            )
            await conn.execute(
                "UPDATE invoices SET review_status = 'needs_review' WHERE is_approved = 0 AND (review_status IS NULL OR review_status = '')"
            )
            await conn.commit()
    except Exception as e:
        logger.warning("HITL backfill skipped: %s", e)


async def get_or_create_client(phone_number: str, name: str = "Unknown Client") -> dict:
    """Gets client profile, or creates it if not existing."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow("SELECT * FROM clients WHERE phone_number = $1", phone_number)
            if not row:
                await conn.execute(
                    "INSERT INTO clients (phone_number, name) VALUES ($1, $2)",
                    phone_number, name
                )
                row = await conn.fetchrow("SELECT * FROM clients WHERE phone_number = $1", phone_number)
            return dict(row)
        else:
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute("SELECT * FROM clients WHERE phone_number = ?", (phone_number,))
            row = await cursor.fetchone()
            if not row:
                await conn.execute(
                    "INSERT INTO clients (phone_number, name) VALUES (?, ?)",
                    (phone_number, name)
                )
                await conn.commit()
                cursor = await conn.execute("SELECT * FROM clients WHERE phone_number = ?", (phone_number,))
                row = await cursor.fetchone()
            return dict(row)
    finally:
        await conn.close()


async def get_client_wa_routing(client_phone: str) -> dict:
    """WhatsApp document routing state (intent hint + pending classification file)."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                """
                SELECT pending_doc_intent, pending_media_path, pending_media_message_id
                FROM clients WHERE phone_number = $1
                """,
                client_phone,
            )
        else:
            conn.row_factory = sqlite3.Row
            cur = await conn.execute(
                """
                SELECT pending_doc_intent, pending_media_path, pending_media_message_id
                FROM clients WHERE phone_number = ?
                """,
                (client_phone,),
            )
            row = await cur.fetchone()
        if not row:
            return {
                "pending_doc_intent": None,
                "pending_media_path": None,
                "pending_media_message_id": None,
            }
        d = dict(row)
        return {
            "pending_doc_intent": d.get("pending_doc_intent"),
            "pending_media_path": d.get("pending_media_path"),
            "pending_media_message_id": d.get("pending_media_message_id"),
        }
    finally:
        await conn.close()


async def set_client_doc_intent(client_phone: str, intent: str | None) -> None:
    await get_or_create_client(client_phone)
    conn = await get_connection()
    try:
        val = (intent or "").strip().lower() or None
        if IS_POSTGRES:
            await conn.execute(
                "UPDATE clients SET pending_doc_intent = $1 WHERE phone_number = $2",
                val,
                client_phone,
            )
        else:
            await conn.execute(
                "UPDATE clients SET pending_doc_intent = ? WHERE phone_number = ?",
                (val, client_phone),
            )
            await conn.commit()
    finally:
        await conn.close()


async def set_pending_media_route(
    client_phone: str, saved_path: str, message_id: str
) -> None:
    await get_or_create_client(client_phone)
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                UPDATE clients
                SET pending_media_path = $1,
                    pending_media_message_id = $2,
                    pending_doc_intent = NULL
                WHERE phone_number = $3
                """,
                saved_path,
                message_id,
                client_phone,
            )
        else:
            await conn.execute(
                """
                UPDATE clients
                SET pending_media_path = ?,
                    pending_media_message_id = ?,
                    pending_doc_intent = NULL
                WHERE phone_number = ?
                """,
                (saved_path, message_id, client_phone),
            )
            await conn.commit()
    finally:
        await conn.close()


async def clear_client_wa_routing(client_phone: str) -> None:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                UPDATE clients
                SET pending_doc_intent = NULL,
                    pending_media_path = NULL,
                    pending_media_message_id = NULL
                WHERE phone_number = $1
                """,
                client_phone,
            )
        else:
            await conn.execute(
                """
                UPDATE clients
                SET pending_doc_intent = NULL,
                    pending_media_path = NULL,
                    pending_media_message_id = NULL
                WHERE phone_number = ?
                """,
                (client_phone,),
            )
            await conn.commit()
    finally:
        await conn.close()


async def update_client_profile(
    phone_number: str,
    gstin: str,
    registered: bool,
    name: str = None,
    pan: str = None,
):
    """Updates client registration status, GSTIN, and optional PAN."""
    conn = await get_connection()
    try:
        pan_val = (pan or "").strip().upper() or None
        if IS_POSTGRES:
            if name and pan_val is not None:
                await conn.execute(
                    "UPDATE clients SET gstin = $1, registered = $2, name = $3, pan = $4 WHERE phone_number = $5",
                    gstin, registered, name, pan_val, phone_number,
                )
            elif name:
                await conn.execute(
                    "UPDATE clients SET gstin = $1, registered = $2, name = $3 WHERE phone_number = $4",
                    gstin, registered, name, phone_number,
                )
            elif pan_val is not None:
                await conn.execute(
                    "UPDATE clients SET gstin = $1, registered = $2, pan = $3 WHERE phone_number = $4",
                    gstin, registered, pan_val, phone_number,
                )
            else:
                await conn.execute(
                    "UPDATE clients SET gstin = $1, registered = $2 WHERE phone_number = $3",
                    gstin, registered, phone_number,
                )
        else:
            if name and pan_val is not None:
                await conn.execute(
                    "UPDATE clients SET gstin = ?, registered = ?, name = ?, pan = ? WHERE phone_number = ?",
                    (gstin, registered, name, pan_val, phone_number),
                )
            elif name:
                await conn.execute(
                    "UPDATE clients SET gstin = ?, registered = ?, name = ? WHERE phone_number = ?",
                    (gstin, registered, name, phone_number),
                )
            elif pan_val is not None:
                await conn.execute(
                    "UPDATE clients SET gstin = ?, registered = ?, pan = ? WHERE phone_number = ?",
                    (gstin, registered, pan_val, phone_number),
                )
            else:
                await conn.execute(
                    "UPDATE clients SET gstin = ?, registered = ? WHERE phone_number = ?",
                    (gstin, registered, phone_number),
                )
            await conn.commit()
    finally:
        await conn.close()


async def update_client_pan(phone_number: str, pan: str) -> None:
    """Set or clear client PAN (Income Tax)."""
    conn = await get_connection()
    try:
        pan_val = (pan or "").strip().upper() or None
        if IS_POSTGRES:
            await conn.execute(
                "UPDATE clients SET pan = $1 WHERE phone_number = $2",
                pan_val, phone_number,
            )
        else:
            await conn.execute(
                "UPDATE clients SET pan = ? WHERE phone_number = ?",
                (pan_val, phone_number),
            )
            await conn.commit()
    finally:
        await conn.close()


async def update_client_gst_portal_username(phone_number: str, username: str) -> None:
    conn = await get_connection()
    try:
        user = (username or "").strip()
        if IS_POSTGRES:
            await conn.execute(
                "UPDATE clients SET gst_portal_username = $1 WHERE phone_number = $2",
                user, phone_number,
            )
        else:
            await conn.execute(
                "UPDATE clients SET gst_portal_username = ? WHERE phone_number = ?",
                (user, phone_number),
            )
            await conn.commit()
    finally:
        await conn.close()


async def save_invoice(client_phone: str, file_path: str, result: dict) -> int:
    """
    Saves an extracted invoice, line items, and audit errors to database.
    Supports line-level ITC fields when present on result / line items.
    """
    from itc_rules import evaluate_line_items_itc, validate_gstin

    conn = await get_connection()
    ext = result["extraction"]
    review_status = result.get("review_status") or "needs_review"
    hitl_reason = result.get("hitl_reason")

    # Ensure line-level ITC evaluation exists (complex bills)
    line_items = list(ext.get("line_items") or [])
    if result.get("line_itc"):
        line_items = list(result["line_itc"])
    elif not result.get("itc_line_evaluated"):
        line_eval = evaluate_line_items_itc(
            line_items,
            invoice_category=ext.get("business_category") or "Other",
            is_recipient_registered=validate_gstin(ext.get("recipient_gstin")),
        )
        line_items = line_eval["lines"] or line_items
        result["is_itc_eligible"] = line_eval["is_itc_eligible"]
        result["itc_ineligibility_reason"] = line_eval["itc_ineligibility_reason"]
        result["itc_eligible_cgst"] = line_eval["eligible_cgst"]
        result["itc_eligible_sgst"] = line_eval["eligible_sgst"]
        result["itc_eligible_igst"] = line_eval["eligible_igst"]
        result["itc_blocked_gst"] = line_eval["blocked_gst"]
        result["itc_partial"] = line_eval["partial"]

    elig_c = float(result.get("itc_eligible_cgst") or 0)
    elig_s = float(result.get("itc_eligible_sgst") or 0)
    elig_i = float(result.get("itc_eligible_igst") or 0)
    blocked_gst = float(result.get("itc_blocked_gst") or 0)
    # Legacy fallback: no line rollup computed
    if elig_c == elig_s == elig_i == blocked_gst == 0 and not line_items:
        gst_c = float(ext.get("total_cgst") or 0)
        gst_s = float(ext.get("total_sgst") or 0)
        gst_i = float(ext.get("total_igst") or 0)
        if result.get("is_itc_eligible"):
            elig_c, elig_s, elig_i = gst_c, gst_s, gst_i
        else:
            blocked_gst = gst_c + gst_s + gst_i

    itc_partial = 1 if result.get("itc_partial") else 0

    # Inherit firm from client (may be NULL until CA link)
    firm_id = None
    try:
        if IS_POSTGRES:
            firm_id = await conn.fetchval(
                "SELECT firm_id FROM clients WHERE phone_number = $1", client_phone
            )
        else:
            cur = await conn.execute(
                "SELECT firm_id FROM clients WHERE phone_number = ?", (client_phone,)
            )
            row = await cur.fetchone()
            firm_id = row[0] if row else None
    except Exception:
        firm_id = None

    try:
        if IS_POSTGRES:
            invoice_id = await conn.fetchval("""
                INSERT INTO invoices (
                    client_phone, file_path, supplier_name, supplier_gstin, recipient_name, recipient_gstin,
                    invoice_number, invoice_date, place_of_supply, total_taxable_value,
                    total_cgst, total_sgst, total_igst, grand_total, business_category,
                    is_calculation_correct, is_itc_eligible, itc_ineligibility_reason, supply_type,
                    review_status, hitl_reason,
                    itc_eligible_cgst, itc_eligible_sgst, itc_eligible_igst, itc_blocked_gst,
                    itc_partial, itc_line_evaluated, firm_id
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,
                    $22,$23,$24,$25,$26,$27,$28
                )
                RETURNING id
            """,
                client_phone, file_path, ext["supplier_name"], ext["supplier_gstin"],
                ext["recipient_name"], ext["recipient_gstin"], ext["invoice_number"],
                ext["invoice_date"], ext["place_of_supply"], ext["total_taxable_value"],
                ext["total_cgst"], ext["total_sgst"], ext["total_igst"], ext["grand_total"],
                ext["business_category"], result["is_calculation_correct"], result["is_itc_eligible"],
                result["itc_ineligibility_reason"], result["supply_type"],
                review_status, hitl_reason,
                elig_c, elig_s, elig_i, blocked_gst, bool(itc_partial), True, firm_id,
            )

            for item in line_items:
                await conn.execute("""
                    INSERT INTO line_items (
                        invoice_id, description, hsn_or_sac, quantity, unit_price,
                        taxable_value, gst_rate, cgst, sgst, igst, line_total,
                        is_itc_eligible, itc_ineligibility_reason, itc_rule_code, inferred_category
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                """,
                    invoice_id, item.get("description"), item.get("hsn_or_sac"), item.get("quantity"),
                    item.get("unit_price"), item.get("taxable_value"), item.get("gst_rate"),
                    item.get("cgst"), item.get("sgst"), item.get("igst"), item.get("line_total"),
                    item.get("is_itc_eligible"), item.get("itc_ineligibility_reason"),
                    item.get("itc_rule_code"), item.get("inferred_category"),
                )

            for err in result["calculation_errors"]:
                await conn.execute(
                    "INSERT INTO audit_errors (invoice_id, error_message) VALUES ($1, $2)",
                    invoice_id, err
                )
        else:
            cursor = await conn.execute("""
                INSERT INTO invoices (
                    client_phone, file_path, supplier_name, supplier_gstin, recipient_name, recipient_gstin,
                    invoice_number, invoice_date, place_of_supply, total_taxable_value,
                    total_cgst, total_sgst, total_igst, grand_total, business_category,
                    is_calculation_correct, is_itc_eligible, itc_ineligibility_reason, supply_type,
                    review_status, hitl_reason,
                    itc_eligible_cgst, itc_eligible_sgst, itc_eligible_igst, itc_blocked_gst,
                    itc_partial, itc_line_evaluated, firm_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                client_phone, file_path, ext["supplier_name"], ext["supplier_gstin"],
                ext["recipient_name"], ext["recipient_gstin"], ext["invoice_number"],
                ext["invoice_date"], ext["place_of_supply"], ext["total_taxable_value"],
                ext["total_cgst"], ext["total_sgst"], ext["total_igst"], ext["grand_total"],
                ext["business_category"], result["is_calculation_correct"], result["is_itc_eligible"],
                result["itc_ineligibility_reason"], result["supply_type"],
                review_status, hitl_reason,
                elig_c, elig_s, elig_i, blocked_gst, itc_partial, 1, firm_id,
            ))
            invoice_id = cursor.lastrowid

            for item in line_items:
                await conn.execute("""
                    INSERT INTO line_items (
                        invoice_id, description, hsn_or_sac, quantity, unit_price,
                        taxable_value, gst_rate, cgst, sgst, igst, line_total,
                        is_itc_eligible, itc_ineligibility_reason, itc_rule_code, inferred_category
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    invoice_id, item.get("description"), item.get("hsn_or_sac"), item.get("quantity"),
                    item.get("unit_price"), item.get("taxable_value"), item.get("gst_rate"),
                    item.get("cgst"), item.get("sgst"), item.get("igst"), item.get("line_total"),
                    1 if item.get("is_itc_eligible") else 0,
                    item.get("itc_ineligibility_reason"),
                    item.get("itc_rule_code"),
                    item.get("inferred_category"),
                ))

            for err in result["calculation_errors"]:
                await conn.execute(
                    "INSERT INTO audit_errors (invoice_id, error_message) VALUES (?, ?)",
                    (invoice_id, err)
                )
            await conn.commit()

        logger.info(
            "Saved invoice ID %s for %s (HITL=%s, partial_itc=%s)",
            invoice_id, client_phone, review_status, bool(itc_partial),
        )
        return invoice_id
    finally:
        await conn.close()


async def get_clients(firm_id: int | None = None) -> list[dict]:
    """Fetch clients. If firm_id set, only that firm's clients (excludes firm_id NULL)."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            if firm_id is not None:
                rows = await conn.fetch(
                    """
                    SELECT c.*, l.ca_invite_code
                    FROM clients c
                    LEFT JOIN client_ca_links l ON l.client_phone = c.phone_number
                    WHERE c.firm_id = $1
                    ORDER BY c.name ASC
                    """,
                    firm_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT c.*, l.ca_invite_code
                    FROM clients c
                    LEFT JOIN client_ca_links l ON l.client_phone = c.phone_number
                    ORDER BY c.name ASC
                    """
                )
            return [dict(r) for r in rows]
        else:
            conn.row_factory = sqlite3.Row
            if firm_id is not None:
                cursor = await conn.execute(
                    """
                    SELECT c.*, l.ca_invite_code
                    FROM clients c
                    LEFT JOIN client_ca_links l ON l.client_phone = c.phone_number
                    WHERE c.firm_id = ?
                    ORDER BY c.name ASC
                    """,
                    (firm_id,),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT c.*, l.ca_invite_code
                    FROM clients c
                    LEFT JOIN client_ca_links l ON l.client_phone = c.phone_number
                    ORDER BY c.name ASC
                    """
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_clients_for_ca(ca_invite_code: str) -> list[dict]:
    """Clients linked to a specific CA invite code (same firm implied by link)."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            rows = await conn.fetch(
                """
                SELECT c.*, l.ca_invite_code
                FROM clients c
                INNER JOIN client_ca_links l ON l.client_phone = c.phone_number
                INNER JOIN cas ca ON ca.invite_code = l.ca_invite_code
                WHERE l.ca_invite_code = $1
                  AND (c.firm_id IS NULL OR c.firm_id = ca.firm_id)
                ORDER BY c.name ASC
                """,
                ca_invite_code,
            )
            return [dict(r) for r in rows]
        else:
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute(
                """
                SELECT c.*, l.ca_invite_code
                FROM clients c
                INNER JOIN client_ca_links l ON l.client_phone = c.phone_number
                INNER JOIN cas ca ON ca.invite_code = l.ca_invite_code
                WHERE l.ca_invite_code = ?
                  AND (c.firm_id IS NULL OR c.firm_id = ca.firm_id)
                ORDER BY c.name ASC
                """,
                (ca_invite_code,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        await conn.close()


async def ca_has_client(ca_invite_code: str, client_phone: str) -> bool:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                """
                SELECT 1 FROM client_ca_links
                WHERE ca_invite_code = $1 AND client_phone = $2
                """,
                ca_invite_code,
                client_phone,
            )
            return row is not None
        else:
            cursor = await conn.execute(
                """
                SELECT 1 FROM client_ca_links
                WHERE ca_invite_code = ? AND client_phone = ?
                """,
                (ca_invite_code, client_phone),
            )
            return (await cursor.fetchone()) is not None
    finally:
        await conn.close()


async def create_ca_session(ca_invite_code: str, token: str, hours: int = 12) -> None:
    import security as security_mod
    from datetime import datetime, timedelta, timezone

    token_hash = security_mod.fingerprint_token(token)
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                INSERT INTO ca_sessions (token_hash, ca_invite_code, expires_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (token_hash) DO UPDATE
                SET ca_invite_code = EXCLUDED.ca_invite_code,
                    expires_at = EXCLUDED.expires_at,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                token_hash,
                ca_invite_code,
                expires,
            )
        else:
            await conn.execute(
                """
                INSERT OR REPLACE INTO ca_sessions (token_hash, ca_invite_code, expires_at)
                VALUES (?, ?, ?)
                """,
                (token_hash, ca_invite_code, expires.isoformat()),
            )
            await conn.commit()
    finally:
        await conn.close()


async def get_ca_session(token: str) -> dict | None:
    """Return CA row + session if token is valid and not expired."""
    import security as security_mod
    from datetime import datetime, timezone

    token_hash = security_mod.fingerprint_token(token)
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                """
                SELECT s.token_hash, s.ca_invite_code, s.expires_at,
                       c.name, c.email, c.firm_name, c.firm_id,
                       f.name AS firm_display_name
                FROM ca_sessions s
                JOIN cas c ON c.invite_code = s.ca_invite_code
                LEFT JOIN firms f ON f.id = c.firm_id
                WHERE s.token_hash = $1
                """,
                token_hash,
            )
            if not row:
                return None
            data = dict(row)
            exp = data.get("expires_at")
            if exp and getattr(exp, "tzinfo", None) is None:
                # asyncpg may return naive UTC
                from datetime import timezone as tz

                exp = exp.replace(tzinfo=tz.utc)
            if exp and exp < datetime.now(timezone.utc):
                await conn.execute("DELETE FROM ca_sessions WHERE token_hash = $1", token_hash)
                return None
            await conn.execute(
                "UPDATE ca_sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE token_hash = $1",
                token_hash,
            )
            return data
        else:
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute(
                """
                SELECT s.token_hash, s.ca_invite_code, s.expires_at,
                       c.name, c.email, c.firm_name, c.firm_id,
                       f.name AS firm_display_name
                FROM ca_sessions s
                JOIN cas c ON c.invite_code = s.ca_invite_code
                LEFT JOIN firms f ON f.id = c.firm_id
                WHERE s.token_hash = ?
                """,
                (token_hash,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            data = dict(row)
            exp_raw = data.get("expires_at") or ""
            try:
                exp = datetime.fromisoformat(str(exp_raw).replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp < datetime.now(timezone.utc):
                    await conn.execute(
                        "DELETE FROM ca_sessions WHERE token_hash = ?", (token_hash,)
                    )
                    await conn.commit()
                    return None
            except Exception:
                pass
            await conn.execute(
                "UPDATE ca_sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE token_hash = ?",
                (token_hash,),
            )
            await conn.commit()
            return data
    finally:
        await conn.close()


async def delete_ca_session(token: str) -> bool:
    """Delete a CA session by raw token. Returns True if a row was removed."""
    result = await revoke_ca_session(token)
    return result is not None


async def revoke_ca_session(token: str) -> dict | None:
    """
    Invalidate a CA session server-side.
    Returns session metadata (ca_invite_code, name, firm_id) if it existed; else None.
    """
    import security as security_mod

    raw = (token or "").strip()
    if not raw:
        return None
    token_hash = security_mod.fingerprint_token(raw)
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                """
                SELECT s.ca_invite_code, c.name, c.firm_id
                FROM ca_sessions s
                JOIN cas c ON c.invite_code = s.ca_invite_code
                WHERE s.token_hash = $1
                """,
                token_hash,
            )
            if not row:
                return None
            meta = dict(row)
            await conn.execute("DELETE FROM ca_sessions WHERE token_hash = $1", token_hash)
            return meta
        conn.row_factory = sqlite3.Row
        cur = await conn.execute(
            """
            SELECT s.ca_invite_code, c.name, c.firm_id
            FROM ca_sessions s
            JOIN cas c ON c.invite_code = s.ca_invite_code
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        meta = dict(row)
        await conn.execute("DELETE FROM ca_sessions WHERE token_hash = ?", (token_hash,))
        await conn.commit()
        return meta
    finally:
        await conn.close()


async def insert_security_audit_log(
    *,
    actor: str,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
) -> None:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                INSERT INTO security_audit_logs
                    (actor, action, resource_type, resource_id, detail, ip)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                actor,
                action,
                resource_type,
                resource_id,
                detail,
                ip,
            )
        else:
            await conn.execute(
                """
                INSERT INTO security_audit_logs
                    (actor, action, resource_type, resource_id, detail, ip)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (actor, action, resource_type, resource_id, detail, ip),
            )
            await conn.commit()
    except Exception:
        logger.exception("Failed to write security_audit_log")
    finally:
        await conn.close()


async def get_invoices(
    client_phone: str = None,
    status: str = None,
    month: str = None,
    firm_id: int | None = None,
) -> list[dict]:
    """
    Fetch list of invoices with filters.
    Args:
        client_phone: Filter by phone number.
        status: 'approved' | 'pending_review' | 'flagged' | 'hitl' |
                'needs_review' | 'awaiting_client' | 'client_confirmed' | 'rejected'.
        month: YYYY-MM based on invoice_date.
        firm_id: When set, only invoices for that firm.
    """
    conn = await get_connection()
    query = "SELECT * FROM invoices WHERE 1=1"
    params = []

    if firm_id is not None:
        if IS_POSTGRES:
            params.append(firm_id)
            query += f" AND firm_id = ${len(params)}"
        else:
            params.append(firm_id)
            query += " AND firm_id = ?"

    if client_phone:
        if IS_POSTGRES:
            params.append(client_phone)
            query += f" AND client_phone = ${len(params)}"
        else:
            params.append(client_phone)
            query += " AND client_phone = ?"

    if status == "approved":
        query += " AND is_approved = 1" if not IS_POSTGRES else " AND is_approved = TRUE"
    elif status == "pending_review":
        # Exclude rejected — pending = awaiting CA action
        if IS_POSTGRES:
            query += " AND is_approved = FALSE AND COALESCE(review_status, 'needs_review') <> 'rejected'"
        else:
            query += " AND is_approved = 0 AND COALESCE(review_status, 'needs_review') != 'rejected'"
    elif status == "flagged":
        query += " AND is_calculation_correct = 0" if not IS_POSTGRES else " AND is_calculation_correct = FALSE"
    elif status == "hitl":
        query += " AND review_status IN ('needs_review', 'awaiting_client', 'client_confirmed')"
    elif status in ("needs_review", "awaiting_client", "client_confirmed", "rejected"):
        if IS_POSTGRES:
            params.append(status)
            query += f" AND review_status = ${len(params)}"
        else:
            params.append(status)
            query += " AND review_status = ?"

    if month:
        # Match YYYY-MM prefix in invoice_date (format YYYY-MM-DD)
        if IS_POSTGRES:
            params.append(f"{month}%")
            query += f" AND invoice_date LIKE ${len(params)}"
        else:
            params.append(f"{month}%")
            query += " AND invoice_date LIKE ?"

    query += " ORDER BY id DESC"

    try:
        if IS_POSTGRES:
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]
        else:
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_invoice_detail(invoice_id: int) -> dict | None:
    """Fetch single invoice detail, its line items, and audit errors."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            inv_row = await conn.fetchrow("SELECT * FROM invoices WHERE id = $1", invoice_id)
            if not inv_row:
                return None
            
            invoice = dict(inv_row)
            
            # Fetch line items
            li_rows = await conn.fetch("SELECT * FROM line_items WHERE invoice_id = $1", invoice_id)
            invoice["line_items"] = [dict(r) for r in li_rows]
            
            # Fetch audit errors
            err_rows = await conn.fetch("SELECT error_message FROM audit_errors WHERE invoice_id = $1", invoice_id)
            invoice["calculation_errors"] = [r["error_message"] for r in err_rows]
            
            # Fetch audit logs
            log_rows = await conn.fetch("SELECT * FROM ca_action_logs WHERE invoice_id = $1 ORDER BY created_at DESC", invoice_id)
            invoice["ca_action_logs"] = [dict(r) for r in log_rows]
            
            return invoice
        else:
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
            inv_row = await cursor.fetchone()
            if not inv_row:
                return None
            
            invoice = dict(inv_row)
            
            # Fetch line items
            cursor = await conn.execute("SELECT * FROM line_items WHERE invoice_id = ?", (invoice_id,))
            li_rows = await cursor.fetchall()
            invoice["line_items"] = [dict(r) for r in li_rows]
            
            # Fetch audit errors
            cursor = await conn.execute("SELECT error_message FROM audit_errors WHERE invoice_id = ?", (invoice_id,))
            err_rows = await cursor.fetchall()
            invoice["calculation_errors"] = [r["error_message"] for r in err_rows]
            
            # Fetch audit logs
            cursor = await conn.execute("SELECT * FROM ca_action_logs WHERE invoice_id = ? ORDER BY created_at DESC", (invoice_id,))
            log_rows = await cursor.fetchall()
            invoice["ca_action_logs"] = [dict(r) for r in log_rows]
            
            return invoice
    finally:
        await conn.close()


async def update_invoice(
    invoice_id: int,
    updated_fields: dict,
    ca_user: str = "CA Operator",
    *,
    ca_invite_code: str | None = None,
    firm_id: int | None = None,
) -> bool:
    """
    Updates invoice metadata and audits the edits.
    Handles fields like supplier_name, supplier_gstin, grand_total, categories, etc.
    Extraction-tracked field edits are also written to extraction_field_edits.
    """
    import json as _json

    conn = await get_connection()
    try:
        # Get old values for logging
        old_invoice = await get_invoice_detail(invoice_id)
        if not old_invoice:
            return False

        firm_id = firm_id if firm_id is not None else old_invoice.get("firm_id")
        client_phone = old_invoice.get("client_phone")

        # Build dynamic query
        allowed_extra = {
            "itc_eligible_cgst", "itc_eligible_sgst", "itc_eligible_igst",
            "itc_blocked_gst", "itc_partial", "itc_line_evaluated",
        }
        sets = []
        params = []
        extraction_edits: list[tuple[str, str | None, str | None]] = []
        for key, new_val in updated_fields.items():
            # Basic validation check to ensure key exists in table
            if (key in old_invoice or key in allowed_extra) and key not in [
                "id", "client_phone", "file_path", "line_items", "calculation_errors",
                "ca_action_logs", "extraction_edit_count", "extraction_edited_fields",
            ]:
                old_val = old_invoice.get(key)
                if old_val != new_val:
                    if IS_POSTGRES:
                        params.append(new_val)
                        sets.append(f"{key} = ${len(params)}")
                    else:
                        params.append(new_val)
                        sets.append(f"{key} = ?")

                    # Log the change
                    await insert_audit_log(
                        conn, invoice_id, ca_user, "EDIT_FIELD", key, str(old_val), str(new_val)
                    )
                    if key in EXTRACTION_TRACKED_FIELDS:
                        extraction_edits.append((key, str(old_val) if old_val is not None else None, str(new_val) if new_val is not None else None))

        if not sets and not extraction_edits:
            return True  # No changes detected

        if sets:
            if IS_POSTGRES:
                params.append(invoice_id)
                query = f"UPDATE invoices SET {', '.join(sets)} WHERE id = ${len(params)}"
                await conn.execute(query, *params)
            else:
                params.append(invoice_id)
                query = f"UPDATE invoices SET {', '.join(sets)} WHERE id = ?"
                await conn.execute(query, params)

        for field_name, old_s, new_s in extraction_edits:
            if IS_POSTGRES:
                await conn.execute(
                    """
                    INSERT INTO extraction_field_edits
                        (invoice_id, firm_id, client_phone, field_name, old_value, new_value,
                         ca_user, ca_invite_code)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    invoice_id,
                    firm_id,
                    client_phone,
                    field_name,
                    old_s,
                    new_s,
                    ca_user,
                    ca_invite_code,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO extraction_field_edits
                        (invoice_id, firm_id, client_phone, field_name, old_value, new_value,
                         ca_user, ca_invite_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invoice_id,
                        firm_id,
                        client_phone,
                        field_name,
                        old_s,
                        new_s,
                        ca_user,
                        ca_invite_code,
                    ),
                )

        if extraction_edits:
            # Roll up distinct edited extraction fields on the invoice row
            prev_fields: list[str] = []
            raw_prev = old_invoice.get("extraction_edited_fields")
            if raw_prev:
                try:
                    prev_fields = list(_json.loads(raw_prev)) if isinstance(raw_prev, str) else list(raw_prev)
                except Exception:
                    prev_fields = []
            for field_name, _, _ in extraction_edits:
                if field_name not in prev_fields:
                    prev_fields.append(field_name)
            count = len(prev_fields)
            fields_json = _json.dumps(prev_fields)
            if IS_POSTGRES:
                await conn.execute(
                    """
                    UPDATE invoices
                    SET extraction_edit_count = $1, extraction_edited_fields = $2
                    WHERE id = $3
                    """,
                    count,
                    fields_json,
                    invoice_id,
                )
            else:
                await conn.execute(
                    """
                    UPDATE invoices
                    SET extraction_edit_count = ?, extraction_edited_fields = ?
                    WHERE id = ?
                    """,
                    (count, fields_json, invoice_id),
                )

        if not IS_POSTGRES:
            await conn.commit()

        logger.info("Updated invoice ID: %d fields: %s by %s", invoice_id, list(updated_fields.keys()), ca_user)
        return True
    finally:
        await conn.close()


async def approve_invoice(
    invoice_id: int,
    ca_user: str = "CA Operator",
    *,
    ca_invite_code: str | None = None,
    firm_id: int | None = None,
) -> bool:
    """Marks an invoice as verified and approved (HITL final gate)."""
    import json as _json

    conn = await get_connection()
    try:
        detail = await get_invoice_detail(invoice_id)
        if not detail:
            return False

        firm_id = firm_id if firm_id is not None else detail.get("firm_id")
        client_phone = detail.get("client_phone")

        # Distinct extraction fields edited before this approve
        if IS_POSTGRES:
            rows = await conn.fetch(
                """
                SELECT DISTINCT field_name FROM extraction_field_edits
                WHERE invoice_id = $1
                """,
                invoice_id,
            )
            fields = [r["field_name"] for r in rows]
        else:
            cur = await conn.execute(
                "SELECT DISTINCT field_name FROM extraction_field_edits WHERE invoice_id = ?",
                (invoice_id,),
            )
            fields = [r[0] for r in await cur.fetchall()]

        # Also include rolled-up column if present
        raw_fields = detail.get("extraction_edited_fields")
        if raw_fields:
            try:
                extra = list(_json.loads(raw_fields)) if isinstance(raw_fields, str) else list(raw_fields)
                for f in extra:
                    if f not in fields:
                        fields.append(f)
            except Exception:
                pass

        had_edit = len(fields) > 0
        fields_json = _json.dumps(fields)

        if IS_POSTGRES:
            await conn.execute(
                "UPDATE invoices SET is_approved = TRUE, review_status = 'approved' WHERE id = $1",
                invoice_id,
            )
            await insert_audit_log(conn, invoice_id, ca_user, "APPROVE", "review_status", None, "approved")
            await conn.execute(
                """
                INSERT INTO invoice_extraction_outcomes
                    (invoice_id, firm_id, client_phone, fields_edited_count, distinct_fields,
                     had_any_edit, ca_user, ca_invite_code)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (invoice_id) DO UPDATE SET
                    fields_edited_count = EXCLUDED.fields_edited_count,
                    distinct_fields = EXCLUDED.distinct_fields,
                    had_any_edit = EXCLUDED.had_any_edit,
                    ca_user = EXCLUDED.ca_user,
                    ca_invite_code = EXCLUDED.ca_invite_code,
                    approved_at = CURRENT_TIMESTAMP
                """,
                invoice_id,
                firm_id,
                client_phone,
                len(fields),
                fields_json,
                had_edit,
                ca_user,
                ca_invite_code,
            )
        else:
            await conn.execute(
                "UPDATE invoices SET is_approved = 1, review_status = 'approved' WHERE id = ?",
                (invoice_id,),
            )
            await insert_audit_log(conn, invoice_id, ca_user, "APPROVE", "review_status", None, "approved")
            await conn.execute(
                """
                INSERT INTO invoice_extraction_outcomes
                    (invoice_id, firm_id, client_phone, fields_edited_count, distinct_fields,
                     had_any_edit, ca_user, ca_invite_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(invoice_id) DO UPDATE SET
                    fields_edited_count = excluded.fields_edited_count,
                    distinct_fields = excluded.distinct_fields,
                    had_any_edit = excluded.had_any_edit,
                    ca_user = excluded.ca_user,
                    ca_invite_code = excluded.ca_invite_code,
                    approved_at = CURRENT_TIMESTAMP
                """,
                (
                    invoice_id,
                    firm_id,
                    client_phone,
                    len(fields),
                    fields_json,
                    1 if had_edit else 0,
                    ca_user,
                    ca_invite_code,
                ),
            )
            await conn.commit()

        logger.info(
            "Invoice approved: %d by CA %s (extraction_edits=%d fields=%s)",
            invoice_id,
            ca_user,
            len(fields),
            fields,
        )
        return True
    finally:
        await conn.close()


async def get_extraction_edit_stats(*, days: int = 30, firm_id: int | None = None) -> dict:
    """
    Aggregate CA extraction-field edit rate from production traffic.
    edit_rate = share of approved invoices that had ≥1 extraction field edit.
    """
    days = max(1, min(int(days or 30), 365))
    conn = await get_connection()
    try:
        firm_clause_pg = "AND firm_id = $2" if firm_id is not None else ""
        firm_clause_sq = "AND firm_id = ?" if firm_id is not None else ""

        if IS_POSTGRES:
            params: list = [days]
            if firm_id is not None:
                params.append(firm_id)
            outcomes = await conn.fetch(
                f"""
                SELECT had_any_edit, fields_edited_count, distinct_fields
                FROM invoice_extraction_outcomes
                WHERE approved_at >= NOW() - make_interval(days => $1)
                {firm_clause_pg}
                """,
                *params,
            )
            field_rows = await conn.fetch(
                f"""
                SELECT field_name, COUNT(*) AS edit_events,
                       COUNT(DISTINCT invoice_id) AS invoices
                FROM extraction_field_edits
                WHERE created_at >= NOW() - make_interval(days => $1)
                {firm_clause_pg}
                GROUP BY field_name
                ORDER BY edit_events DESC
                """,
                *params,
            )
        else:
            params = [f"-{days} days"]
            if firm_id is not None:
                params.append(firm_id)
            cur = await conn.execute(
                f"""
                SELECT had_any_edit, fields_edited_count, distinct_fields
                FROM invoice_extraction_outcomes
                WHERE approved_at >= datetime('now', ?)
                {firm_clause_sq}
                """,
                tuple(params),
            )
            outcomes = await cur.fetchall()
            cur = await conn.execute(
                f"""
                SELECT field_name, COUNT(*) AS edit_events,
                       COUNT(DISTINCT invoice_id) AS invoices
                FROM extraction_field_edits
                WHERE created_at >= datetime('now', ?)
                {firm_clause_sq}
                GROUP BY field_name
                ORDER BY edit_events DESC
                """,
                tuple(params),
            )
            field_rows = await cur.fetchall()

        approved = len(outcomes)
        with_edits = 0
        total_fields_touched = 0
        for row in outcomes:
            if IS_POSTGRES:
                had = bool(row["had_any_edit"])
                total_fields_touched += int(row["fields_edited_count"] or 0)
            else:
                had = bool(row[0])
                total_fields_touched += int(row[1] or 0)
            if had:
                with_edits += 1

        by_field = []
        for row in field_rows:
            if IS_POSTGRES:
                by_field.append(
                    {
                        "field_name": row["field_name"],
                        "edit_events": int(row["edit_events"]),
                        "invoices": int(row["invoices"]),
                    }
                )
            else:
                by_field.append(
                    {
                        "field_name": row[0],
                        "edit_events": int(row[1]),
                        "invoices": int(row[2]),
                    }
                )

        edit_rate = (with_edits / approved) if approved else None
        return {
            "days": days,
            "firm_id": firm_id,
            "approved_invoices": approved,
            "approved_with_extraction_edits": with_edits,
            "edit_rate": round(edit_rate, 4) if edit_rate is not None else None,
            "avg_fields_edited_when_touched": (
                round(total_fields_touched / with_edits, 2) if with_edits else None
            ),
            "by_field": by_field,
            "tracked_fields": sorted(EXTRACTION_TRACKED_FIELDS),
        }
    finally:
        await conn.close()


async def get_pilot_stats(*, days: int = 30, firm_id: int | None = None) -> dict:
    """
    GST pilot KPIs for the CA dashboard (docs/PILOT_CRITERIA.md).
    Includes approvals progress, edit-rate gate, filing-critical fields,
    and reject/skip volume in the same window.
    """
    base = await get_extraction_edit_stats(days=days, firm_id=firm_id)
    days = int(base.get("days") or days)
    approved = int(base.get("approved_invoices") or 0)
    edit_rate = base.get("edit_rate")
    by_field = list(base.get("by_field") or [])

    filing_critical = []
    critical_hold_fields = []
    for row in by_field:
        name = row.get("field_name")
        if name not in PILOT_FILING_CRITICAL_FIELDS:
            continue
        invs = int(row.get("invoices") or 0)
        rate = (invs / approved) if approved else None
        entry = {
            "field_name": name,
            "edit_events": int(row.get("edit_events") or 0),
            "invoices": invs,
            "rate": round(rate, 4) if rate is not None else None,
            "hold": bool(rate is not None and rate >= PILOT_FIELD_RATE_HOLD),
        }
        filing_critical.append(entry)
        if entry["hold"]:
            critical_hold_fields.append(name)
    filing_critical.sort(key=lambda x: (-(x["rate"] or 0), x["field_name"]))

    # Reject / skip volume (same window) — not in edit_rate denominator
    conn = await get_connection()
    try:
        firm_clause_pg = "AND firm_id = $2" if firm_id is not None else ""
        firm_clause_sq = "AND firm_id = ?" if firm_id is not None else ""
        if IS_POSTGRES:
            params: list = [days]
            if firm_id is not None:
                params.append(firm_id)
            counts = await conn.fetchrow(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE review_status = 'rejected') AS rejected,
                    COUNT(*) FILTER (WHERE review_status = 'skipped') AS skipped,
                    COUNT(*) FILTER (
                        WHERE COALESCE(is_approved, FALSE) = FALSE
                          AND COALESCE(review_status, 'needs_review')
                              NOT IN ('rejected', 'skipped', 'approved')
                    ) AS pending_review
                FROM invoices
                WHERE created_at >= NOW() - make_interval(days => $1)
                {firm_clause_pg}
                """,
                *params,
            )
            rejected = int(counts["rejected"] or 0) if counts else 0
            skipped = int(counts["skipped"] or 0) if counts else 0
            pending = int(counts["pending_review"] or 0) if counts else 0
        else:
            params = [f"-{days} days"]
            if firm_id is not None:
                params.append(firm_id)
            cur = await conn.execute(
                f"""
                SELECT
                    SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                    SUM(CASE WHEN review_status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                    SUM(
                        CASE
                            WHEN COALESCE(is_approved, 0) = 0
                             AND COALESCE(review_status, 'needs_review')
                                 NOT IN ('rejected', 'skipped', 'approved')
                            THEN 1 ELSE 0
                        END
                    ) AS pending_review
                FROM invoices
                WHERE created_at >= datetime('now', ?)
                {firm_clause_sq}
                """,
                tuple(params),
            )
            row = await cur.fetchone()
            rejected = int(row[0] or 0) if row else 0
            skipped = int(row[1] or 0) if row else 0
            pending = int(row[2] or 0) if row else 0
    finally:
        await conn.close()

    volume_met = approved >= PILOT_MIN_APPROVALS
    overall_hold = bool(
        volume_met and edit_rate is not None and edit_rate >= PILOT_EDIT_RATE_HOLD
    )
    field_hold = bool(volume_met and critical_hold_fields)

    if approved < PILOT_CHECK_IN_AT:
        status = "collecting"
        status_label = "Collecting approvals"
        status_hint = (
            f"{approved}/{PILOT_MIN_APPROVALS} approved — keep reviewing WhatsApp bills. "
            f"Edit-rate (% you changed on extract) is counted only after {PILOT_MIN_APPROVALS}."
        )
    elif approved < PILOT_MIN_APPROVALS:
        status = "check_in"
        status_label = "Relationship check-in"
        status_hint = (
            f"At {approved}/{PILOT_MIN_APPROVALS} — ask the CA “how’s this feeling?” "
            "Keep approving; do not decide scale/hold on edit-rate yet."
        )
    elif overall_hold or field_hold:
        status = "hold"
        status_label = "Hold — fix extraction"
        parts = []
        if overall_hold:
            parts.append(f"overall edit-rate {edit_rate:.0%} ≥ {PILOT_EDIT_RATE_HOLD:.0%}")
        if field_hold:
            parts.append(
                "filing-critical: " + ", ".join(critical_hold_fields[:4])
            )
        status_hint = (
            "Volume met, but extraction needs work: "
            + "; ".join(parts)
            + f". Hold scale if any filing-critical field ≥ {PILOT_FIELD_RATE_HOLD:.0%}."
        )
    else:
        status = "go"
        status_label = "Go — scale outreach"
        status_hint = (
            f"≥{PILOT_MIN_APPROVALS} approvals and edit-rate under "
            f"{PILOT_EDIT_RATE_HOLD:.0%} (no filing-critical field ≥ {PILOT_FIELD_RATE_HOLD:.0%})."
        )

    progress_pct = min(100.0, round(100.0 * approved / PILOT_MIN_APPROVALS, 1))

    return {
        **base,
        "pilot": {
            "status": status,
            "status_label": status_label,
            "status_hint": status_hint,
            "volume_met": volume_met,
            "min_approvals": PILOT_MIN_APPROVALS,
            "check_in_at": PILOT_CHECK_IN_AT,
            "progress_pct": progress_pct,
            "approvals_remaining": max(0, PILOT_MIN_APPROVALS - approved),
            "edit_rate_hold_threshold": PILOT_EDIT_RATE_HOLD,
            "field_rate_hold_threshold": PILOT_FIELD_RATE_HOLD,
            "show_edit_rate": volume_met,
            "filing_critical": filing_critical,
            "critical_hold_fields": critical_hold_fields,
            "rejected_invoices": rejected,
            "skipped_invoices": skipped,
            "pending_review": pending,
        },
    }


async def reset_extraction_edit_stats(*, firm_id: int | None = None) -> dict:
    """
    Day-zero pilot wipe: clear extraction edit events + approve outcomes.
    Does NOT delete invoices. Optional firm_id scopes the wipe.
    """
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            if firm_id is not None:
                edits = await conn.fetchval(
                    "SELECT COUNT(*) FROM extraction_field_edits WHERE firm_id = $1",
                    firm_id,
                )
                outcomes = await conn.fetchval(
                    "SELECT COUNT(*) FROM invoice_extraction_outcomes WHERE firm_id = $1",
                    firm_id,
                )
                await conn.execute(
                    "DELETE FROM extraction_field_edits WHERE firm_id = $1", firm_id
                )
                await conn.execute(
                    "DELETE FROM invoice_extraction_outcomes WHERE firm_id = $1", firm_id
                )
            else:
                edits = await conn.fetchval("SELECT COUNT(*) FROM extraction_field_edits")
                outcomes = await conn.fetchval(
                    "SELECT COUNT(*) FROM invoice_extraction_outcomes"
                )
                await conn.execute("DELETE FROM extraction_field_edits")
                await conn.execute("DELETE FROM invoice_extraction_outcomes")
        else:
            if firm_id is not None:
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM extraction_field_edits WHERE firm_id = ?",
                    (firm_id,),
                )
                edits = int((await cur.fetchone())[0] or 0)
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM invoice_extraction_outcomes WHERE firm_id = ?",
                    (firm_id,),
                )
                outcomes = int((await cur.fetchone())[0] or 0)
                await conn.execute(
                    "DELETE FROM extraction_field_edits WHERE firm_id = ?", (firm_id,)
                )
                await conn.execute(
                    "DELETE FROM invoice_extraction_outcomes WHERE firm_id = ?",
                    (firm_id,),
                )
                await conn.commit()
            else:
                cur = await conn.execute("SELECT COUNT(*) FROM extraction_field_edits")
                edits = int((await cur.fetchone())[0] or 0)
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM invoice_extraction_outcomes"
                )
                outcomes = int((await cur.fetchone())[0] or 0)
                await conn.execute("DELETE FROM extraction_field_edits")
                await conn.execute("DELETE FROM invoice_extraction_outcomes")
                await conn.commit()

        return {
            "ok": True,
            "firm_id": firm_id,
            "deleted_edit_events": int(edits or 0),
            "deleted_outcomes": int(outcomes or 0),
            "hint": "Verify with GET /api/admin/extraction-edit-stats — approved_invoices should be 0 before real traffic.",
        }
    finally:
        await conn.close()


async def skip_invoice(
    invoice_id: int,
    reason: str = "Skipped by CA",
    ca_user: str = "CA Operator",
) -> bool:
    """Defer an invoice out of the CA review queue without approving or rejecting."""
    conn = await get_connection()
    reason = (reason or "Skipped by CA").strip()
    try:
        detail = await get_invoice_detail(invoice_id)
        if not detail:
            return False
        old = detail.get("review_status")
        note = reason
        prev = (detail.get("hitl_reason") or "").strip()
        if prev and reason not in prev:
            note = f"{prev}; {reason}"
        if IS_POSTGRES:
            await conn.execute(
                """
                UPDATE invoices
                SET review_status = 'skipped', hitl_reason = $1, is_approved = FALSE
                WHERE id = $2
                """,
                note, invoice_id,
            )
            await insert_audit_log(
                conn, invoice_id, ca_user, "SKIP", "review_status", old, f"skipped:{reason}"
            )
        else:
            await conn.execute(
                """
                UPDATE invoices
                SET review_status = 'skipped', hitl_reason = ?, is_approved = 0
                WHERE id = ?
                """,
                (note, invoice_id),
            )
            await insert_audit_log(
                conn, invoice_id, ca_user, "SKIP", "review_status", old, f"skipped:{reason}"
            )
            await conn.commit()
        logger.info("Invoice skipped: %d by %s", invoice_id, ca_user)
        return True
    finally:
        await conn.close()


async def acknowledge_gstr2b_mismatch(
    invoice_id: int,
    note: str = "CA noted GSTR-2B mismatch",
    ca_user: str = "CA Operator",
) -> bool:
    """Mark a 2B mismatch as reviewed so it no longer drives exception risk."""
    conn = await get_connection()
    note = (note or "CA noted GSTR-2B mismatch").strip()
    try:
        detail = await get_invoice_detail(invoice_id)
        if not detail:
            return False
        old_status = detail.get("gstr2b_match_status")
        prev = (detail.get("hitl_reason") or "").strip()
        hitl = note if not prev else (prev if note in prev else f"{prev}; {note}")
        if IS_POSTGRES:
            await conn.execute(
                """
                UPDATE invoices
                SET gstr2b_match_status = 'mismatch_accepted',
                    gstr2b_mismatch_reason = COALESCE(gstr2b_mismatch_reason, $1),
                    hitl_reason = $2
                WHERE id = $3
                """,
                note, hitl, invoice_id,
            )
            await insert_audit_log(
                conn, invoice_id, ca_user, "2B_NOTE", "gstr2b_match_status",
                old_status, "mismatch_accepted",
            )
        else:
            await conn.execute(
                """
                UPDATE invoices
                SET gstr2b_match_status = 'mismatch_accepted',
                    gstr2b_mismatch_reason = COALESCE(gstr2b_mismatch_reason, ?),
                    hitl_reason = ?
                WHERE id = ?
                """,
                (note, hitl, invoice_id),
            )
            await insert_audit_log(
                conn, invoice_id, ca_user, "2B_NOTE", "gstr2b_match_status",
                old_status, "mismatch_accepted",
            )
            await conn.commit()
        return True
    finally:
        await conn.close()


async def reject_invoice(
    invoice_id: int,
    reason: str,
    actor: str = "CA Operator",
    source: str = "ca",
) -> bool:
    """Reject an invoice in the HITL loop (CA or client)."""
    conn = await get_connection()
    reason = (reason or "Rejected").strip()
    try:
        detail = await get_invoice_detail(invoice_id)
        if not detail:
            return False
        old = detail.get("review_status")
        if IS_POSTGRES:
            await conn.execute(
                """
                UPDATE invoices
                SET is_approved = FALSE, review_status = 'rejected', hitl_reason = $1
                WHERE id = $2
                """,
                reason, invoice_id
            )
            await insert_audit_log(
                conn, invoice_id, actor, "REJECT", "review_status", old, f"rejected:{reason}"
            )
        else:
            await conn.execute(
                """
                UPDATE invoices
                SET is_approved = 0, review_status = 'rejected', hitl_reason = ?
                WHERE id = ?
                """,
                (reason, invoice_id)
            )
            await insert_audit_log(
                conn, invoice_id, actor, "REJECT", "review_status", old, f"rejected:{reason}"
            )
            await conn.commit()
        logger.info("Invoice %d rejected by %s (%s): %s", invoice_id, actor, source, reason)
        return True
    finally:
        await conn.close()


async def client_confirm_invoice(invoice_id: int, client_phone: str) -> tuple[bool, str]:
    """Client WhatsApp CONFIRM. Moves awaiting_client -> client_confirmed."""
    detail = await get_invoice_detail(invoice_id)
    if not detail:
        return False, "Invoice not found."
    if detail.get("client_phone") != client_phone:
        return False, "This invoice does not belong to your account."

    status = detail.get("review_status") or "needs_review"
    if status == "approved":
        return False, "This invoice is already approved by your CA."
    if status == "rejected":
        return False, "This invoice was rejected. Send a new photo or ask your CA."
    if status == "needs_review":
        return False, "This invoice is with your CA for review. They will confirm after fixing issues."
    if status == "client_confirmed":
        return True, "You already confirmed this invoice. Waiting for CA approval."

    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                "UPDATE invoices SET review_status = 'client_confirmed' WHERE id = $1",
                invoice_id
            )
            await insert_audit_log(
                conn, invoice_id, client_phone, "CLIENT_CONFIRM", "review_status", status, "client_confirmed"
            )
        else:
            await conn.execute(
                "UPDATE invoices SET review_status = 'client_confirmed' WHERE id = ?",
                (invoice_id,)
            )
            await insert_audit_log(
                conn, invoice_id, client_phone, "CLIENT_CONFIRM", "review_status", status, "client_confirmed"
            )
            await conn.commit()
        return True, "Thanks! Confirmed. Your CA will do the final approval for GSTR."
    finally:
        await conn.close()


async def insert_audit_log(conn, invoice_id: int, ca_user: str, action: str, field_name: str | None, old_val: str | None, new_val: str | None):
    """Internal helper to insert changes into audit trail."""
    if IS_POSTGRES:
        await conn.execute("""
            INSERT INTO ca_action_logs (invoice_id, ca_user, action, field_name, old_value, new_value)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, invoice_id, ca_user, action, field_name, old_val, new_val)
    else:
        await conn.execute("""
            INSERT INTO ca_action_logs (invoice_id, ca_user, action, field_name, old_value, new_value)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (invoice_id, ca_user, action, field_name, old_val, new_val))


async def get_monthly_metrics(client_phone: str, year_month: str | None = None) -> dict:
    """
    Computes KPIs for a client.

    year_month:
      - 'YYYY-MM' filters invoice_date to that month
      - None / '' / 'all' means all invoices (no date filter)

    ITC / sales classification requires a valid client GSTIN:
      - Sales: supplier_gstin == client GSTIN AND approved
      - ITC claimed/blocked: recipient_gstin == client GSTIN AND approved
      - ITC provisional: same recipient match, eligible, not approved, not rejected
    Without a valid GSTIN, sales/ITC stay 0 (total_taxable still shows all invoices).
    """
    from itc_rules import normalize_gstin, validate_gstin

    scope_all = (not year_month) or str(year_month).lower() in ("all", "*")
    month = None if scope_all else year_month

    client = await get_or_create_client(client_phone)
    raw_gstin = client.get("gstin")
    client_gstin = normalize_gstin(raw_gstin)
    has_gstin = validate_gstin(client_gstin)

    invoices = await get_invoices(client_phone=client_phone, month=month)
    metrics = _compute_metrics_from_invoices(
        invoices,
        client_gstin=client_gstin if has_gstin else "",
        has_gstin=has_gstin,
        period="all" if scope_all else year_month,
    )

    # Month-close extras: was GSTR-2B imported for this period?
    if scope_all:
        metrics["gstr2b_imported"] = False
        metrics["gstr2b_entry_count"] = 0
        metrics["month_scope"] = "all"
    else:
        entries = await get_gstr2b_entries(client_phone, year_month)
        metrics["gstr2b_imported"] = len(entries) > 0
        metrics["gstr2b_entry_count"] = len(entries)
        metrics["month_scope"] = year_month

    needs_ca = 0
    for inv in invoices:
        if _invoice_needs_ca(inv):
            needs_ca += 1
    metrics["needs_ca_count"] = needs_ca

    blockers = []
    if not has_gstin:
        blockers.append("Set a valid client GSTIN")
    if scope_all:
        blockers.append("Pick a single month to close")
    elif metrics["invoice_count"] == 0:
        blockers.append("No bills in this month")
    if needs_ca > 0:
        blockers.append(f"{needs_ca} still need CA")
    if not scope_all and not metrics["gstr2b_imported"]:
        blockers.append("Import GSTR-2B for this month")
    elif not scope_all and metrics["gstr2b_imported"]:
        if metrics.get("gstr2b_unmatched_count", 0) > 0:
            blockers.append(f"{metrics['gstr2b_unmatched_count']} missing in 2B")
        if metrics.get("gstr2b_mismatch_count", 0) > 0:
            blockers.append(f"{metrics['gstr2b_mismatch_count']} 2B mismatch")

    metrics["month_close_ready"] = len(blockers) == 0
    metrics["month_close_blockers"] = blockers
    return metrics


def _invoice_needs_ca(inv: dict) -> bool:
    """Mirror dashboard exception queue: CA must still act."""
    review = (inv.get("review_status") or (
        "approved" if _truthy(inv.get("is_approved")) else "needs_review"
    )).strip()
    if review in ("rejected", "skipped", "approved"):
        return False
    if _truthy(inv.get("is_approved")):
        # Approved but still open 2B risk
        m2b = (inv.get("gstr2b_match_status") or "none").strip().lower()
        if m2b in ("unmatched", "mismatch"):
            return True
        if not _truthy(inv.get("is_calculation_correct")):
            return True
        return False
    return True  # pending / awaiting / client_confirmed etc.


def _truthy(val) -> bool:
    return val in (True, 1, "1")


def _invoice_gst(inv: dict) -> float:
    return (
        float(inv.get("total_cgst") or 0)
        + float(inv.get("total_sgst") or 0)
        + float(inv.get("total_igst") or 0)
    )


def _compute_metrics_from_invoices(
    invoices: list[dict],
    *,
    client_gstin: str,
    has_gstin: bool,
    period,
) -> dict:
    from itc_rules import normalize_gstin

    pending_cnt = 0
    flagged_cnt = 0
    total_taxable = 0.0
    total_gst = 0.0
    sales_taxable = 0.0
    sales_gst = 0.0
    purchase_taxable = 0.0
    itc_cgst = itc_sgst = itc_igst = 0.0
    itc_blocked = 0.0
    itc_provisional = 0.0
    itc_2b_matched = 0.0
    itc_2b_unmatched = 0.0
    itc_2b_mismatch = 0.0
    gstr2b_matched_count = 0
    gstr2b_unmatched_count = 0
    gstr2b_mismatch_count = 0

    gstin = normalize_gstin(client_gstin) if has_gstin else ""

    for inv in invoices:
        taxable = float(inv.get("total_taxable_value") or 0)
        gst = _invoice_gst(inv)
        total_taxable += taxable
        total_gst += gst

        approved = _truthy(inv.get("is_approved"))
        review = (inv.get("review_status") or ("approved" if approved else "needs_review")).strip()
        if not approved and review != "rejected":
            pending_cnt += 1
        if not _truthy(inv.get("is_calculation_correct")):
            flagged_cnt += 1

        if not has_gstin or not gstin:
            continue

        sup = normalize_gstin(inv.get("supplier_gstin"))
        rec = normalize_gstin(inv.get("recipient_gstin"))

        # Sales liability — approved outward supplies only
        if approved and sup == gstin:
            sales_taxable += taxable
            sales_gst += gst

        # Purchases — client must be the recipient
        if rec != gstin:
            continue

        elig_c, elig_s, elig_i, blocked_gst = _invoice_itc_buckets(inv)
        eligible_gst = elig_c + elig_s + elig_i
        match_status = (inv.get("gstr2b_match_status") or "none").strip().lower()

        if approved:
            itc_cgst += elig_c
            itc_sgst += elig_s
            itc_igst += elig_i
            itc_blocked += blocked_gst
            # expenses_taxable ≈ taxable share of eligible portion when partial
            if eligible_gst + blocked_gst > 0 and gst > 0:
                purchase_taxable += taxable * (eligible_gst / (eligible_gst + blocked_gst))
            elif eligible_gst > 0:
                purchase_taxable += taxable

            if eligible_gst > 0:
                if match_status == "matched":
                    itc_2b_matched += eligible_gst
                    gstr2b_matched_count += 1
                elif match_status == "mismatch":
                    itc_2b_mismatch += eligible_gst
                    gstr2b_mismatch_count += 1
                elif match_status == "unmatched":
                    itc_2b_unmatched += eligible_gst
                    gstr2b_unmatched_count += 1
        elif review != "rejected" and eligible_gst > 0:
            itc_provisional += eligible_gst

    itc_claimed = itc_cgst + itc_sgst + itc_igst
    net_raw = sales_gst - itc_claimed
    net_gst_payable = max(0.0, net_raw)
    itc_credit_balance = max(0.0, -net_raw)

    return {
        "period": period,
        "has_gstin": has_gstin,
        "invoice_count": len(invoices),
        "pending_review": pending_cnt,
        "flagged_count": flagged_cnt,
        "total_taxable": total_taxable,
        "total_gst": total_gst,
        "sales_taxable": sales_taxable,
        "sales_gst_liability": sales_gst,
        "expenses_taxable": purchase_taxable,
        "itc_claimed": itc_claimed,
        "itc_cgst": itc_cgst,
        "itc_sgst": itc_sgst,
        "itc_igst": itc_igst,
        "itc_blocked": itc_blocked,
        "itc_provisional": itc_provisional,
        "net_gst_payable": net_gst_payable,
        "itc_credit_balance": itc_credit_balance,
        # GSTR-2B reconciliation (approved eligible purchases only)
        "itc_2b_matched": itc_2b_matched,
        "itc_2b_unmatched": itc_2b_unmatched,
        "itc_2b_mismatch": itc_2b_mismatch,
        "gstr2b_matched_count": gstr2b_matched_count,
        "gstr2b_unmatched_count": gstr2b_unmatched_count,
        "gstr2b_mismatch_count": gstr2b_mismatch_count,
    }


def _invoice_itc_buckets(inv: dict) -> tuple[float, float, float, float]:
    """
    Return (eligible_cgst, eligible_sgst, eligible_igst, blocked_gst).
    Uses line-level rollups only when itc_line_evaluated is set; else legacy whole-invoice.
    """
    if _truthy(inv.get("itc_line_evaluated")):
        return (
            float(inv.get("itc_eligible_cgst") or 0),
            float(inv.get("itc_eligible_sgst") or 0),
            float(inv.get("itc_eligible_igst") or 0),
            float(inv.get("itc_blocked_gst") or 0),
        )

    # Legacy: whole invoice GST is either claimable or blocked
    cgst = float(inv.get("total_cgst") or 0)
    sgst = float(inv.get("total_sgst") or 0)
    igst = float(inv.get("total_igst") or 0)
    if _truthy(inv.get("is_itc_eligible")):
        return cgst, sgst, igst, 0.0
    return 0.0, 0.0, 0.0, cgst + sgst + igst


async def apply_line_itc_evaluation(invoice_id: int, ca_user: str = "system") -> dict | None:
    """Re-run line-level ITC on an existing invoice and persist rollups."""
    from itc_rules import evaluate_line_items_itc, validate_gstin

    detail = await get_invoice_detail(invoice_id)
    if not detail:
        return None

    line_eval = evaluate_line_items_itc(
        detail.get("line_items") or [],
        invoice_category=detail.get("business_category") or "Other",
        is_recipient_registered=validate_gstin(detail.get("recipient_gstin")),
    )

    fields = {
        "is_itc_eligible": line_eval["is_itc_eligible"],
        "itc_ineligibility_reason": line_eval["itc_ineligibility_reason"] or "",
        "itc_eligible_cgst": line_eval["eligible_cgst"],
        "itc_eligible_sgst": line_eval["eligible_sgst"],
        "itc_eligible_igst": line_eval["eligible_igst"],
        "itc_blocked_gst": line_eval["blocked_gst"],
        "itc_partial": bool(line_eval["partial"]),
        "itc_line_evaluated": True,
    }
    await update_invoice(invoice_id, fields, ca_user)

    # Update each line item ITC flags
    conn = await get_connection()
    try:
        for line in line_eval["lines"]:
            lid = line.get("id")
            if not lid:
                continue
            if IS_POSTGRES:
                await conn.execute(
                    """
                    UPDATE line_items
                    SET is_itc_eligible = $1,
                        itc_ineligibility_reason = $2,
                        itc_rule_code = $3,
                        inferred_category = $4
                    WHERE id = $5
                    """,
                    line.get("is_itc_eligible"),
                    line.get("itc_ineligibility_reason"),
                    line.get("itc_rule_code"),
                    line.get("inferred_category"),
                    lid,
                )
            else:
                await conn.execute(
                    """
                    UPDATE line_items
                    SET is_itc_eligible = ?,
                        itc_ineligibility_reason = ?,
                        itc_rule_code = ?,
                        inferred_category = ?
                    WHERE id = ?
                    """,
                    (
                        1 if line.get("is_itc_eligible") else 0,
                        line.get("itc_ineligibility_reason"),
                        line.get("itc_rule_code"),
                        line.get("inferred_category"),
                        lid,
                    ),
                )
        if not IS_POSTGRES:
            await conn.commit()
    finally:
        await conn.close()

    return await get_invoice_detail(invoice_id)


async def get_itc_monthly_trend(client_phone: str, num_months: int = 12) -> list[dict]:
    """
    Returns month-by-month ITC trend for the last `num_months` months.
    Each entry: { month: 'YYYY-MM', itc_eligible, itc_blocked, sales_gst }
    Uses the same classification rules as get_monthly_metrics.
    """
    from datetime import date

    today = date.today()
    results = []
    for i in range(num_months - 1, -1, -1):
        total_months = (today.year * 12 + today.month - 1) - i
        year = total_months // 12
        month = total_months % 12 + 1
        year_month = f"{year:04d}-{month:02d}"
        m = await get_monthly_metrics(client_phone, year_month)
        results.append({
            "month": year_month,
            "itc_eligible": float(m.get("itc_claimed") or 0),
            "itc_blocked": float(m.get("itc_blocked") or 0),
            "sales_gst": float(m.get("sales_gst_liability") or 0),
        })
    return results


# ── CA invite codes ───────────────────────────────────────────────────────────
async def get_ca_by_invite_code(invite_code: str) -> dict | None:
    """Look up a CA profile by 6-digit invite code. Returns None if not found."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow("SELECT * FROM cas WHERE invite_code = $1", invite_code)
            return dict(row) if row else None
        else:
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute("SELECT * FROM cas WHERE invite_code = ?", (invite_code,))
            row = await cursor.fetchone()
            return dict(row) if row else None
    finally:
        await conn.close()


async def link_client_to_ca(client_phone: str, invite_code: str) -> dict:
    """
    Link a client phone to a CA invite code.
    Creates the client row if needed, then upserts the link and assigns firm_id.
    """
    await get_or_create_client(client_phone, name="Onboarding Client")
    ca = await get_ca_by_invite_code(invite_code)
    if not ca:
        raise ValueError("Invite code not found")

    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                INSERT INTO client_ca_links (client_phone, ca_invite_code)
                VALUES ($1, $2)
                ON CONFLICT (client_phone) DO UPDATE SET
                    ca_invite_code = EXCLUDED.ca_invite_code,
                    linked_at = CURRENT_TIMESTAMP
                """,
                client_phone, invite_code
            )
        else:
            await conn.execute(
                """
                INSERT INTO client_ca_links (client_phone, ca_invite_code)
                VALUES (?, ?)
                ON CONFLICT(client_phone) DO UPDATE SET
                    ca_invite_code = excluded.ca_invite_code,
                    linked_at = CURRENT_TIMESTAMP
                """,
                (client_phone, invite_code)
            )
            await conn.commit()
    finally:
        await conn.close()

    firm_id = ca.get("firm_id")
    if firm_id is not None:
        await set_client_firm(client_phone, int(firm_id))
    return ca


async def client_has_ca_link(client_phone: str) -> bool:
    """True if this WhatsApp client is already assigned to a CA."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                "SELECT 1 FROM client_ca_links WHERE client_phone = $1",
                client_phone,
            )
            return row is not None
        cursor = await conn.execute(
            "SELECT 1 FROM client_ca_links WHERE client_phone = ?",
            (client_phone,),
        )
        return await cursor.fetchone() is not None
    finally:
        await conn.close()


async def resolve_default_ca_invite() -> str | None:
    """
    Invite code used to auto-assign WhatsApp clients that have no CA link yet.
    Order: DEFAULT_CA_INVITE_CODE env → sole CA in DB → demo invite 123456 if present.
    """
    env_code = (os.getenv("DEFAULT_CA_INVITE_CODE") or "").strip()
    if env_code and await get_ca_by_invite_code(env_code):
        return env_code

    conn = await get_connection()
    try:
        if IS_POSTGRES:
            rows = await conn.fetch("SELECT invite_code FROM cas ORDER BY invite_code ASC LIMIT 2")
            codes = [r["invite_code"] for r in rows]
        else:
            cursor = await conn.execute(
                "SELECT invite_code FROM cas ORDER BY invite_code ASC LIMIT 2"
            )
            codes = [r[0] for r in await cursor.fetchall()]
    finally:
        await conn.close()

    if len(codes) == 1:
        return codes[0]
    if await get_ca_by_invite_code("123456"):
        return "123456"
    return None


async def ensure_wa_client_linked(client_phone: str) -> str | None:
    """
    Make sure a WhatsApp sender is visible on the CA dashboard.
    Links to DEFAULT_CA_INVITE_CODE / sole CA / demo 123456 when unassigned.
    Returns the invite code used, or None if already linked / no CA available.
    """
    phone = "".join(c for c in str(client_phone or "") if c.isdigit())
    if not phone:
        return None
    if await client_has_ca_link(phone):
        return None
    invite = await resolve_default_ca_invite()
    if not invite:
        logger.warning(
            "WhatsApp client %s has no CA link and no default CA — dashboard will show empty for CA logins",
            phone,
        )
        return None
    await link_client_to_ca(phone, invite)
    logger.info("Auto-linked WhatsApp client %s → CA invite %s", phone, invite)
    return invite


# ── WhatsApp message deduplication ────────────────────────────────────────────
async def is_message_processed(message_id: str) -> bool:
    """Return True if this WhatsApp message ID was already handled."""
    if not message_id:
        return False
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                "SELECT 1 FROM processed_messages WHERE message_id = $1", message_id
            )
            return row is not None
        else:
            cursor = await conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
            )
            return await cursor.fetchone() is not None
    finally:
        await conn.close()


async def mark_message_processed(message_id: str) -> None:
    """Record a WhatsApp message ID as processed (idempotent)."""
    if not message_id:
        return
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                """
                INSERT INTO processed_messages (message_id)
                VALUES ($1)
                ON CONFLICT (message_id) DO NOTHING
                """,
                message_id
            )
            # Keep table bounded — drop entries older than 7 days
            await conn.execute(
                "DELETE FROM processed_messages WHERE created_at < NOW() - INTERVAL '7 days'"
            )
        else:
            await conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)
                """,
                (message_id,)
            )
            await conn.execute(
                """
                DELETE FROM processed_messages
                WHERE created_at < datetime('now', '-7 days')
                """
            )
            await conn.commit()
    finally:
        await conn.close()



async def _ensure_itr_schema(conn) -> None:
    """PAN on clients + ITR returns / Form 16 documents (Phase 1 Income Tax)."""
    if IS_POSTGRES:
        await conn.execute(
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS pan VARCHAR(10)"
        )
        for col in ("pending_doc_intent", "pending_media_path", "pending_media_message_id"):
            await conn.execute(
                f"ALTER TABLE clients ADD COLUMN IF NOT EXISTS {col} TEXT"
            )
        await conn.execute(
            "ALTER TABLE itr_returns ADD COLUMN IF NOT EXISTS ais_json TEXT"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS itr_returns (
                id SERIAL PRIMARY KEY,
                client_phone VARCHAR(20) NOT NULL REFERENCES clients(phone_number),
                financial_year VARCHAR(16) NOT NULL,
                pan VARCHAR(10),
                status VARCHAR(32) DEFAULT 'draft',
                gross_salary DOUBLE PRECISION DEFAULT 0,
                exemptions DOUBLE PRECISION DEFAULT 0,
                other_income DOUBLE PRECISION DEFAULT 0,
                deductions_80c DOUBLE PRECISION DEFAULT 0,
                tds DOUBLE PRECISION DEFAULT 0,
                advance_tax DOUBLE PRECISION DEFAULT 0,
                regime_preferred VARCHAR(8),
                estimate_json TEXT,
                extracted_json TEXT,
                ais_json TEXT,
                ca_notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (client_phone, financial_year)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS itr_documents (
                id SERIAL PRIMARY KEY,
                itr_return_id INTEGER NOT NULL REFERENCES itr_returns(id) ON DELETE CASCADE,
                doc_type VARCHAR(32) DEFAULT 'form16',
                file_path TEXT NOT NULL,
                original_filename TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    else:
        try:
            await conn.execute("ALTER TABLE clients ADD COLUMN pan TEXT")
        except Exception:
            pass
        for col in ("pending_doc_intent", "pending_media_path", "pending_media_message_id"):
            try:
                await conn.execute(f"ALTER TABLE clients ADD COLUMN {col} TEXT")
            except Exception:
                pass
        try:
            await conn.execute("ALTER TABLE itr_returns ADD COLUMN ais_json TEXT")
        except Exception:
            pass
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS itr_returns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_phone TEXT NOT NULL REFERENCES clients(phone_number),
                financial_year TEXT NOT NULL,
                pan TEXT,
                status TEXT DEFAULT 'draft',
                gross_salary REAL DEFAULT 0,
                exemptions REAL DEFAULT 0,
                other_income REAL DEFAULT 0,
                deductions_80c REAL DEFAULT 0,
                tds REAL DEFAULT 0,
                advance_tax REAL DEFAULT 0,
                regime_preferred TEXT,
                estimate_json TEXT,
                extracted_json TEXT,
                ais_json TEXT,
                ca_notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (client_phone, financial_year)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS itr_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                itr_return_id INTEGER NOT NULL REFERENCES itr_returns(id) ON DELETE CASCADE,
                doc_type TEXT DEFAULT 'form16',
                file_path TEXT NOT NULL,
                original_filename TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()


def _row_to_dict(row) -> dict:
    if row is None:
        return None
    return dict(row)


async def list_itr_returns(client_phone: str) -> list[dict]:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            rows = await conn.fetch(
                """
                SELECT * FROM itr_returns
                WHERE client_phone = $1
                ORDER BY financial_year DESC, id DESC
                """,
                client_phone,
            )
            return [dict(r) for r in rows]
        conn.row_factory = sqlite3.Row
        cur = await conn.execute(
            """
            SELECT * FROM itr_returns
            WHERE client_phone = ?
            ORDER BY financial_year DESC, id DESC
            """,
            (client_phone,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def get_itr_return(itr_id: int) -> dict | None:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow("SELECT * FROM itr_returns WHERE id = $1", itr_id)
            return dict(row) if row else None
        conn.row_factory = sqlite3.Row
        cur = await conn.execute("SELECT * FROM itr_returns WHERE id = ?", (itr_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def get_or_create_itr_return(
    client_phone: str,
    financial_year: str,
    pan: str | None = None,
) -> dict:
    fy = (financial_year or "").strip()
    pan_val = (pan or "").strip().upper() or None
    existing_list = await list_itr_returns(client_phone)
    for r in existing_list:
        if r.get("financial_year") == fy:
            if pan_val and not r.get("pan"):
                await update_itr_return(r["id"], {"pan": pan_val})
                return await get_itr_return(r["id"])
            return r

    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                """
                INSERT INTO itr_returns (client_phone, financial_year, pan, status)
                VALUES ($1, $2, $3, 'draft')
                ON CONFLICT (client_phone, financial_year) DO UPDATE
                    SET pan = COALESCE(EXCLUDED.pan, itr_returns.pan),
                        updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                client_phone,
                fy,
                pan_val,
            )
            return dict(row)
        cur = await conn.execute(
            """
            INSERT INTO itr_returns (client_phone, financial_year, pan, status)
            VALUES (?, ?, ?, 'draft')
            ON CONFLICT(client_phone, financial_year) DO UPDATE SET
                pan = COALESCE(excluded.pan, itr_returns.pan),
                updated_at = CURRENT_TIMESTAMP
            """,
            (client_phone, fy, pan_val),
        )
        await conn.commit()
        # Fetch the row
        conn.row_factory = sqlite3.Row
        cur = await conn.execute(
            "SELECT * FROM itr_returns WHERE client_phone = ? AND financial_year = ?",
            (client_phone, fy),
        )
        return dict(await cur.fetchone())
    finally:
        await conn.close()


async def update_itr_return(itr_id: int, fields: dict) -> dict | None:
    allowed = {
        "pan",
        "status",
        "gross_salary",
        "exemptions",
        "other_income",
        "deductions_80c",
        "tds",
        "advance_tax",
        "regime_preferred",
        "estimate_json",
        "extracted_json",
        "ais_json",
        "ca_notes",
        "financial_year",
    }
    updates = {k: v for k, v in (fields or {}).items() if k in allowed}
    if not updates:
        return await get_itr_return(itr_id)

    conn = await get_connection()
    try:
        cols = list(updates.keys())
        if IS_POSTGRES:
            sets = ", ".join(f"{c} = ${i+1}" for i, c in enumerate(cols))
            vals = [updates[c] for c in cols]
            vals.append(itr_id)
            await conn.execute(
                f"UPDATE itr_returns SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ${len(cols)+1}",
                *vals,
            )
        else:
            sets = ", ".join(f"{c} = ?" for c in cols)
            vals = [updates[c] for c in cols]
            vals.append(itr_id)
            await conn.execute(
                f"UPDATE itr_returns SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                vals,
            )
            await conn.commit()
    finally:
        await conn.close()
    return await get_itr_return(itr_id)


async def add_itr_document(
    itr_return_id: int,
    file_path: str,
    doc_type: str = "form16",
    original_filename: str | None = None,
) -> dict:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            row = await conn.fetchrow(
                """
                INSERT INTO itr_documents (itr_return_id, doc_type, file_path, original_filename)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                itr_return_id,
                doc_type,
                file_path,
                original_filename,
            )
            return dict(row)
        cur = await conn.execute(
            """
            INSERT INTO itr_documents (itr_return_id, doc_type, file_path, original_filename)
            VALUES (?, ?, ?, ?)
            """,
            (itr_return_id, doc_type, file_path, original_filename),
        )
        doc_id = cur.lastrowid
        await conn.commit()
        conn.row_factory = sqlite3.Row
        cur = await conn.execute("SELECT * FROM itr_documents WHERE id = ?", (doc_id,))
        return dict(await cur.fetchone())
    finally:
        await conn.close()


async def list_itr_documents(itr_return_id: int) -> list[dict]:
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            rows = await conn.fetch(
                "SELECT * FROM itr_documents WHERE itr_return_id = $1 ORDER BY id DESC",
                itr_return_id,
            )
            return [dict(r) for r in rows]
        conn.row_factory = sqlite3.Row
        cur = await conn.execute(
            "SELECT * FROM itr_documents WHERE itr_return_id = ? ORDER BY id DESC",
            (itr_return_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()
