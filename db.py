"""
db.py — Database management layer for GST Autopilot

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

# Default local SQLite database file path
SQLITE_DB_PATH = Path(os.getenv("STORAGE_DIR", "./storage")) / "gst_autopilot.db"


async def get_connection():
    """Returns an active database connection context or manager."""
    if IS_POSTGRES:
        import asyncpg
        return await asyncpg.connect(DATABASE_URL)
    else:
        # Ensure directory exists for local SQLite
        SQLITE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return await aiosqlite.connect(SQLITE_DB_PATH)


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
            """)
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
            await conn.commit()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.exception("Error creating tables:")
        raise e
    finally:
        await conn.close()


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


async def update_client_profile(phone_number: str, gstin: str, registered: bool, name: str = None):
    """Updates client registration status and GSTIN."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            if name:
                await conn.execute(
                    "UPDATE clients SET gstin = $1, registered = $2, name = $3 WHERE phone_number = $4",
                    gstin, registered, name, phone_number
                )
            else:
                await conn.execute(
                    "UPDATE clients SET gstin = $1, registered = $2 WHERE phone_number = $3",
                    gstin, registered, phone_number
                )
        else:
            if name:
                await conn.execute(
                    "UPDATE clients SET gstin = ?, registered = ?, name = ? WHERE phone_number = ?",
                    (gstin, registered, name, phone_number)
                )
            else:
                await conn.execute(
                    "UPDATE clients SET gstin = ?, registered = ? WHERE phone_number = ?",
                    (gstin, registered, phone_number)
                )
            await conn.commit()
    finally:
        await conn.close()


async def save_invoice(client_phone: str, file_path: str, result: dict) -> int:
    """
    Saves an extracted invoice, line items, and audit errors to database.
    Args:
        client_phone: Phone number of the client who sent the invoice.
        file_path: Relative storage path of the file.
        result: The `ProcessingResult` model dump as dictionary.
    Returns:
        The newly created invoice ID.
    """
    conn = await get_connection()
    ext = result["extraction"]

    try:
        if IS_POSTGRES:
            # Insert invoice
            invoice_id = await conn.fetchval("""
                INSERT INTO invoices (
                    client_phone, file_path, supplier_name, supplier_gstin, recipient_name, recipient_gstin,
                    invoice_number, invoice_date, place_of_supply, total_taxable_value,
                    total_cgst, total_sgst, total_igst, grand_total, business_category,
                    is_calculation_correct, is_itc_eligible, itc_ineligibility_reason, supply_type
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
                RETURNING id
            """, 
                client_phone, file_path, ext["supplier_name"], ext["supplier_gstin"],
                ext["recipient_name"], ext["recipient_gstin"], ext["invoice_number"],
                ext["invoice_date"], ext["place_of_supply"], ext["total_taxable_value"],
                ext["total_cgst"], ext["total_sgst"], ext["total_igst"], ext["grand_total"],
                ext["business_category"], result["is_calculation_correct"], result["is_itc_eligible"],
                result["itc_ineligibility_reason"], result["supply_type"]
            )

            # Insert line items
            for item in ext["line_items"]:
                await conn.execute("""
                    INSERT INTO line_items (
                        invoice_id, description, hsn_or_sac, quantity, unit_price,
                        taxable_value, gst_rate, cgst, sgst, igst, line_total
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                    invoice_id, item["description"], item["hsn_or_sac"], item["quantity"],
                    item["unit_price"], item["taxable_value"], item["gst_rate"],
                    item["cgst"], item["sgst"], item["igst"], item["line_total"]
                )

            # Insert calculation errors
            for err in result["calculation_errors"]:
                await conn.execute(
                    "INSERT INTO audit_errors (invoice_id, error_message) VALUES ($1, $2)",
                    invoice_id, err
                )
        else:
            # SQLite Insert
            cursor = await conn.execute("""
                INSERT INTO invoices (
                    client_phone, file_path, supplier_name, supplier_gstin, recipient_name, recipient_gstin,
                    invoice_number, invoice_date, place_of_supply, total_taxable_value,
                    total_cgst, total_sgst, total_igst, grand_total, business_category,
                    is_calculation_correct, is_itc_eligible, itc_ineligibility_reason, supply_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                client_phone, file_path, ext["supplier_name"], ext["supplier_gstin"],
                ext["recipient_name"], ext["recipient_gstin"], ext["invoice_number"],
                ext["invoice_date"], ext["place_of_supply"], ext["total_taxable_value"],
                ext["total_cgst"], ext["total_sgst"], ext["total_igst"], ext["grand_total"],
                ext["business_category"], result["is_calculation_correct"], result["is_itc_eligible"],
                result["itc_ineligibility_reason"], result["supply_type"]
            ))
            invoice_id = cursor.lastrowid

            # Insert line items
            for item in ext["line_items"]:
                await conn.execute("""
                    INSERT INTO line_items (
                        invoice_id, description, hsn_or_sac, quantity, unit_price,
                        taxable_value, gst_rate, cgst, sgst, igst, line_total
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    invoice_id, item["description"], item["hsn_or_sac"], item["quantity"],
                    item["unit_price"], item["taxable_value"], item["gst_rate"],
                    item["cgst"], item["sgst"], item["igst"], item["line_total"]
                ))

            # Insert calculation errors
            for err in result["calculation_errors"]:
                await conn.execute(
                    "INSERT INTO audit_errors (invoice_id, error_message) VALUES (?, ?)",
                    (invoice_id, err)
                )

            await conn.commit()
            
        logger.info("Saved invoice to database with ID: %d", invoice_id)
        return invoice_id
    finally:
        await conn.close()


async def get_clients() -> list[dict]:
    """Fetch all clients in the system."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            rows = await conn.fetch("SELECT * FROM clients ORDER BY name ASC")
            return [dict(r) for r in rows]
        else:
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute("SELECT * FROM clients ORDER BY name ASC")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_invoices(client_phone: str = None, status: str = None, month: str = None) -> list[dict]:
    """
    Fetch list of invoices with filters.
    Args:
        client_phone: Filter by phone number.
        status: 'approved' or 'pending_review' or 'flagged'.
        month: YYYY-MM based on invoice_date.
    """
    conn = await get_connection()
    query = "SELECT * FROM invoices WHERE 1=1"
    params = []

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
        query += " AND is_approved = 0" if not IS_POSTGRES else " AND is_approved = FALSE"
    elif status == "flagged":
        query += " AND is_calculation_correct = 0" if not IS_POSTGRES else " AND is_calculation_correct = FALSE"

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


async def update_invoice(invoice_id: int, updated_fields: dict, ca_user: str = "CA Operator") -> bool:
    """
    Updates invoice metadata and audits the edits.
    Handles fields like supplier_name, supplier_gstin, grand_total, categories, etc.
    """
    conn = await get_connection()
    try:
        # Get old values for logging
        old_invoice = await get_invoice_detail(invoice_id)
        if not old_invoice:
            return False

        # Build dynamic query
        sets = []
        params = []
        for key, new_val in updated_fields.items():
            # Basic validation check to ensure key exists in table
            if key in old_invoice and key not in ["id", "client_phone", "file_path", "line_items", "calculation_errors", "ca_action_logs"]:
                old_val = old_invoice[key]
                if old_val != new_val:
                    if IS_POSTGRES:
                        params.append(new_val)
                        sets.append(f"{key} = ${len(params)}")
                    else:
                        params.append(new_val)
                        sets.append(f"{key} = ?")

                    # Log the change
                    await insert_audit_log(conn, invoice_id, ca_user, "EDIT_FIELD", key, str(old_val), str(new_val))

        if not sets:
            return True  # No changes detected

        if IS_POSTGRES:
            params.append(invoice_id)
            query = f"UPDATE invoices SET {', '.join(sets)} WHERE id = ${len(params)}"
            await conn.execute(query, *params)
        else:
            params.append(invoice_id)
            query = f"UPDATE invoices SET {', '.join(sets)} WHERE id = ?"
            await conn.execute(query, params)
            await conn.commit()
            
        logger.info("Updated invoice ID: %d fields: %s by %s", invoice_id, list(updated_fields.keys()), ca_user)
        return True
    finally:
        await conn.close()


async def approve_invoice(invoice_id: int, ca_user: str = "CA Operator") -> bool:
    """Marks an invoice as verified and approved."""
    conn = await get_connection()
    try:
        if IS_POSTGRES:
            await conn.execute(
                "UPDATE invoices SET is_approved = TRUE WHERE id = $1",
                invoice_id
            )
            await insert_audit_log(conn, invoice_id, ca_user, "APPROVE", None, "FALSE", "TRUE")
        else:
            await conn.execute(
                "UPDATE invoices SET is_approved = 1 WHERE id = ?",
                (invoice_id,)
            )
            await insert_audit_log(conn, invoice_id, ca_user, "APPROVE", None, "FALSE", "TRUE")
            await conn.commit()

        logger.info("Invoice approved: %d by CA %s", invoice_id, ca_user)
        return True
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


async def get_monthly_metrics(client_phone: str, year_month: str) -> dict:
    """
    Computes key performance indicators (KPIs) for a given month:
    - Total sales
    - Taxable expenses
    - Total eligible ITC (from approved purchase invoices)
    - Pending review count
    """
    conn = await get_connection()
    like_str = f"{year_month}%"
    
    try:
        if IS_POSTGRES:
            # Pending review count
            pending_cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM invoices WHERE client_phone = $1 AND invoice_date LIKE $2 AND is_approved = FALSE",
                client_phone, like_str
            )
            
            # Taxable value & GST liability (from sales)
            # Sales are categorized when the supplier_gstin matches client's GSTIN
            # Fetch client gstin
            client_gstin = await conn.fetchval("SELECT gstin FROM clients WHERE phone_number = $1", client_phone)
            
            sales_taxable = 0.0
            sales_gst = 0.0
            purchase_taxable = 0.0
            purchase_itc_claimed = 0.0

            if client_gstin:
                # Sales: client is supplier
                sales_row = await conn.fetchrow("""
                    SELECT SUM(total_taxable_value) as taxable, SUM(total_cgst + total_sgst + total_igst) as gst
                    FROM invoices 
                    WHERE client_phone = $1 AND invoice_date LIKE $2 AND supplier_gstin = $3
                """, client_phone, like_str, client_gstin)
                
                if sales_row and sales_row["taxable"]:
                    sales_taxable = float(sales_row["taxable"])
                    sales_gst = float(sales_row["gst"] or 0.0)

                # Purchases: client is recipient
                purchase_row = await conn.fetchrow("""
                    SELECT SUM(total_taxable_value) as taxable, SUM(total_cgst + total_sgst + total_igst) as gst
                    FROM invoices 
                    WHERE client_phone = $1 AND invoice_date LIKE $2 AND recipient_gstin = $3 AND is_itc_eligible = TRUE AND is_approved = TRUE
                """, client_phone, like_str, client_gstin)

                if purchase_row and purchase_row["taxable"]:
                    purchase_taxable = float(purchase_row["taxable"])
                    purchase_itc_claimed = float(purchase_row["gst"] or 0.0)
            else:
                # Fallback if no GSTIN matches, treat all invoices as purchases for simplicity (Expenses tracker)
                purchase_row = await conn.fetchrow("""
                    SELECT SUM(total_taxable_value) as taxable, SUM(total_cgst + total_sgst + total_igst) as gst
                    FROM invoices 
                    WHERE client_phone = $1 AND invoice_date LIKE $2 AND is_itc_eligible = TRUE AND is_approved = TRUE
                """, client_phone, like_str)
                if purchase_row and purchase_row["taxable"]:
                    purchase_taxable = float(purchase_row["taxable"])
                    purchase_itc_claimed = float(purchase_row["gst"] or 0.0)
            
            return {
                "pending_review": pending_cnt or 0,
                "sales_taxable": sales_taxable,
                "sales_gst_liability": sales_gst,
                "expenses_taxable": purchase_taxable,
                "itc_claimed": purchase_itc_claimed,
            }
        else:
            # SQLite Implementation
            # Client details
            cursor = await conn.execute("SELECT gstin FROM clients WHERE phone_number = ?", (client_phone,))
            row = await cursor.fetchone()
            client_gstin = row[0] if row else None

            # Pending count
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE client_phone = ? AND invoice_date LIKE ? AND is_approved = 0",
                (client_phone, like_str)
            )
            pending_cnt = (await cursor.fetchone())[0]

            sales_taxable = 0.0
            sales_gst = 0.0
            purchase_taxable = 0.0
            purchase_itc_claimed = 0.0

            if client_gstin:
                # Sales
                cursor = await conn.execute("""
                    SELECT SUM(total_taxable_value), SUM(total_cgst + total_sgst + total_igst)
                    FROM invoices 
                    WHERE client_phone = ? AND invoice_date LIKE ? AND supplier_gstin = ?
                """, (client_phone, like_str, client_gstin))
                res = await cursor.fetchone()
                if res and res[0] is not None:
                    sales_taxable = float(res[0])
                    sales_gst = float(res[1] or 0.0)

                # Purchases (approved only for claiming ITC)
                cursor = await conn.execute("""
                    SELECT SUM(total_taxable_value), SUM(total_cgst + total_sgst + total_igst)
                    FROM invoices 
                    WHERE client_phone = ? AND invoice_date LIKE ? AND recipient_gstin = ? AND is_itc_eligible = 1 AND is_approved = 1
                """, (client_phone, like_str, client_gstin))
                res = await cursor.fetchone()
                if res and res[0] is not None:
                    purchase_taxable = float(res[0])
                    purchase_itc_claimed = float(res[1] or 0.0)
            else:
                cursor = await conn.execute("""
                    SELECT SUM(total_taxable_value), SUM(total_cgst + total_sgst + total_igst)
                    FROM invoices 
                    WHERE client_phone = ? AND invoice_date LIKE ? AND is_itc_eligible = 1 AND is_approved = 1
                """, (client_phone, like_str))
                res = await cursor.fetchone()
                if res and res[0] is not None:
                    purchase_taxable = float(res[0])
                    purchase_itc_claimed = float(res[1] or 0.0)

            return {
                "pending_review": pending_cnt,
                "sales_taxable": sales_taxable,
                "sales_gst_liability": sales_gst,
                "expenses_taxable": purchase_taxable,
                "itc_claimed": purchase_itc_claimed,
            }
    finally:
        await conn.close()
