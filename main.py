"""
main.py — Taxova.ai WhatsApp Webhook & CA Dashboard Server

Endpoints:
  GET  /webhook  → Meta verification
  POST /webhook  → WhatsApp message ingestion & client mapping
  GET  /dashboard → CA Admin review console
  GET  /api/*    → REST APIs for CA dashboard operations
"""

import os
import copy
import hmac
import hashlib
import json
import logging
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Query, HTTPException, BackgroundTasks, UploadFile, File, Form, Depends, Security, Header
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from dotenv import load_dotenv

load_dotenv(override=True)

import whatsapp
import storage
import db
import gstr
import rag
import agent
import hitl
import security
from processor import process_invoice, evaluate_itc_eligibility, validate_gstin
import re
import sandbox
import itr
import document_router
import ais as ais_mod

# ── Load config ───────────────────────────────────────────────────────────────
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("APP_SECRET", "")
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "")
WHATSAPP_DISPLAY_NUMBER = "".join(
    ch for ch in os.getenv("WHATSAPP_DISPLAY_NUMBER", "") if ch.isdigit()
)


def _webhook_internal_url() -> str:
    """URL for simulator → same-process /webhook (works on Railway + local)."""
    explicit = os.getenv("WEBHOOK_INTERNAL_URL", "").strip()
    if explicit:
        return explicit if explicit.endswith("/webhook") else explicit.rstrip("/") + "/webhook"
    # Loopback on the port this process listens on (Railway sets PORT).
    port = (os.getenv("PORT") or os.getenv("WEBHOOK_PORT") or "8001").strip()
    return f"http://127.0.0.1:{port}/webhook"


def whatsapp_me_url(prefill: str = "Hi Taxova.ai — I want to start GST filing") -> str:
    """Build https://wa.me/<number>?text=... or fall back to onboarding."""
    if not WHATSAPP_DISPLAY_NUMBER:
        return "/onboarding"
    from urllib.parse import quote

    return f"https://wa.me/{WHATSAPP_DISPLAY_NUMBER}?text={quote(prefill)}"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)-12s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webhook")

# ── Dashboard API auth ────────────────────────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_dashboard_auth(
    request: Request,
    api_key: str | None = Security(_api_key_header),
    x_ca_session: str | None = Header(None, alias="X-CA-Session"),
) -> security.AuthContext:
    """
    Protect CA dashboard REST APIs.
    Accepts either:
      - X-API-Key matching DASHBOARD_API_KEY (full admin / scripts), or
      - X-CA-Session from CA login (scoped to linked clients).
    If DASHBOARD_API_KEY is unset and no session, open (local/dev only).
    """
    # CA session first (so logged-in CAs aren't blocked when API key also sent)
    session_token = (x_ca_session or "").strip()
    if not session_token:
        auth_h = request.headers.get("Authorization") or ""
        if auth_h.lower().startswith("bearer "):
            session_token = auth_h[7:].strip()

    if session_token:
        row = await db.get_ca_session(session_token)
        if row:
            firm_id = row.get("firm_id")
            try:
                firm_id = int(firm_id) if firm_id is not None else None
            except (TypeError, ValueError):
                firm_id = None
            ctx = security.AuthContext(
                mode="ca_session",
                ca_invite_code=row.get("ca_invite_code"),
                ca_name=row.get("name"),
                ca_email=row.get("email"),
                firm_id=firm_id,
                firm_name=row.get("firm_display_name") or row.get("firm_name"),
                is_admin_key=False,
            )
            security.set_auth_context(ctx)
            return ctx
        # Invalid session — fall through to API key / reject

    if DASHBOARD_API_KEY:
        if api_key and hmac.compare_digest(api_key, DASHBOARD_API_KEY):
            ctx = security.AuthContext(mode="api_key", is_admin_key=True)
            security.set_auth_context(ctx)
            return ctx
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing credentials. Log in as CA or send X-API-Key.",
        )

    # Dev open mode
    ctx = security.AuthContext(mode="open", is_admin_key=True)
    security.set_auth_context(ctx)
    return ctx


def effective_ca_user(body: dict | None = None) -> str:
    auth = security.get_auth_context()
    if auth.mode == "ca_session":
        return auth.display_name
    if body and body.get("ca_user"):
        return str(body.get("ca_user")).strip() or auth.display_name
    return auth.display_name


async def assert_client_access(client_phone: str) -> None:
    auth = security.get_auth_context()
    if auth.can_see_all_clients or not client_phone:
        return
    if not auth.ca_invite_code:
        raise HTTPException(status_code=403, detail="CA session missing invite code.")
    if auth.firm_id is not None:
        in_firm = await db.client_in_firm(client_phone, auth.firm_id)
        if not in_firm:
            raise HTTPException(status_code=403, detail="Client not in your firm.")
    ok = await db.ca_has_client(auth.ca_invite_code, client_phone)
    if not ok:
        raise HTTPException(status_code=403, detail="Client not linked to this CA.")


def require_platform_admin(auth: security.AuthContext | None = None) -> security.AuthContext:
    """DASHBOARD_API_KEY / open-dev only — not CA sessions."""
    ctx = auth or security.get_auth_context()
    if not ctx.is_admin_key:
        raise HTTPException(
            status_code=403,
            detail="Platform admin API key required.",
        )
    return ctx

# ── Message deduplication (persisted in DB; see db.processed_messages) ────────


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Taxova.ai webhook server starting...")
    logger.info("   Verify token configured: %s", bool(WEBHOOK_VERIFY_TOKEN))
    logger.info("   App secret configured:   %s", bool(APP_SECRET))
    logger.info("   Dashboard API key set:   %s", bool(DASHBOARD_API_KEY))
    logger.info("   File encryption enabled: %s", security.encryption_enabled())
    _tok, _pid = whatsapp._refresh_credentials()
    logger.info(
        "   WhatsApp token loaded: %s (ends …%s) phone_id=%s",
        bool(_tok),
        (_tok[-6:] if _tok else ""),
        _pid or "(missing)",
    )
    logger.info("   wa.me CTA number set:  %s", bool(WHATSAPP_DISPLAY_NUMBER))
    if not APP_SECRET:
        logger.error("APP_SECRET is empty — webhook POSTs will be rejected until it is set.")
    if not DASHBOARD_API_KEY:
        logger.warning("DASHBOARD_API_KEY is empty — dashboard APIs are open (dev mode).")
    if not security.encryption_enabled():
        logger.warning("ENCRYPTION_KEY is empty — invoice files stored in plaintext.")

    # Initialize storage + database schema on start
    storage_dir = os.getenv("STORAGE_DIR", "./storage")
    os.makedirs(storage_dir, exist_ok=True)
    logger.info("   STORAGE_DIR=%s", storage_dir)

    await db.init_db()
    
    # Optional startup seed (can block Railway health checks — off by default)
    auto_index = os.getenv("RAG_AUTO_INDEX", "").strip().lower() in {"1", "true", "yes"}
    if auto_index:
        try:
            coll = rag.ensure_chroma()
            chunk_count = coll.count() if coll is not None else -1
            if chunk_count == 0:
                logger.info("Knowledge base is empty. Auto-indexing reference docs on startup...")
                ref_dir = os.path.join(os.getcwd(), "reference_docs")
                if os.path.exists(ref_dir):
                    files = [f for f in os.listdir(ref_dir) if f.endswith(".md") or f.endswith(".txt")]
                    for filename in files:
                        filepath = os.path.join(ref_dir, filename)
                        with open(filepath, "r", encoding="utf-8") as f:
                            content = f.read()
                        title = filename
                        first_line = content.splitlines()[0] if content.splitlines() else ""
                        if first_line.startswith("# "):
                            title = first_line.replace("# ", "").strip()
                        await rag.index_document(
                            title=title, text=content, source_file=filename
                        )
                    logger.info("Startup seed indexing completed successfully.")
        except Exception as e:
            logger.warning("Startup seed indexing failed: %s", e)
    else:
        logger.info("   RAG auto-index skipped (set RAG_AUTO_INDEX=true to enable).")
        
    yield
    logger.info("👋 Webhook server shutting down")


app = FastAPI(
    title="Taxova.ai — WhatsApp Webhook & CA Dashboard",
    version="0.2.0",
    lifespan=lifespan,
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def verify_signature(payload: bytes, signature_header: str | None) -> bool:
    """
    Verify the X-Hub-Signature-256 header from Meta.
    Meta signs every webhook payload with your App Secret using HMAC-SHA256.
    """
    if not APP_SECRET:
        logger.error("APP_SECRET not set — rejecting webhook (configure APP_SECRET in .env)")
        return False

    if not signature_header:
        logger.warning("No X-Hub-Signature-256 header in request")
        return False

    # Header format: "sha256=<hex_digest>"
    expected_signature = hmac.new(
        APP_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    received_signature = signature_header.replace("sha256=", "")
    return hmac.compare_digest(expected_signature, received_signature)


def extract_messages(body: dict) -> list[dict]:
    """Extract message objects from Meta's webhook payload."""
    messages = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if "messages" in value:
                # Attach contact info to each message for convenience
                contacts = {c["wa_id"]: c for c in value.get("contacts", [])}
                for msg in value["messages"]:
                    msg["_contact"] = contacts.get(msg.get("from", ""), {})
                    messages.append(msg)
    return messages


# ── Webhook Verification (GET) ───────────────────────────────────────────────
@app.get("/webhook")
async def verify_webhook(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta sends a GET request to verify your webhook URL during setup."""
    logger.info("Webhook verification: mode=%s, token=%s", mode, token)

    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        logger.info("✅ Webhook verified successfully")
        return Response(content=challenge, media_type="text/plain")

    logger.warning("❌ Webhook verification failed (token mismatch)")
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Webhook Handler (POST) ───────────────────────────────────────────────────
@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives and processes every incoming WhatsApp message."""
    # ── Step 1: Read and verify the payload ──
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_signature(raw_body, signature):
        logger.error("🚨 Invalid webhook signature — rejecting request")
        raise HTTPException(status_code=401, detail="Invalid signature")

    body = json.loads(raw_body)

    # ── Step 2: Extract messages ──
    messages = extract_messages(body)

    if not messages:
        # This is likely a status update (delivered, read, etc.) — not an error
        logger.debug("Webhook received (no messages — likely a status update)")
        return {"status": "ok"}

    # ── Step 3: Process each message ──
    for msg in messages:
        message_id = msg.get("id", "")
        sender = msg.get("from", "unknown")
        msg_type = msg.get("type", "unknown")
        contact_name = msg.get("_contact", {}).get("profile", {}).get("name", "Unknown")

        # Deduplication check (persisted across restarts)
        if await db.is_message_processed(message_id):
            logger.info("⏭️  Skipping duplicate message %s from %s", message_id, sender)
            continue

        await db.mark_message_processed(message_id)

        logger.info(
            "📩 New message: type=%s, from=%s (%s), id=%s",
            msg_type, sender, contact_name, message_id,
        )

        try:
            # Map client profile context in database
            await db.get_or_create_client(sender, contact_name)

            if msg_type in ("image", "document"):
                await _handle_media_message(msg, sender, message_id, msg_type, background_tasks)
            elif msg_type == "text":
                await _handle_text_message(msg, sender, message_id, background_tasks)
            elif msg_type == "audio":
                await _handle_audio_message(msg, sender, message_id)
            else:
                logger.info("Unhandled message type: %s", msg_type)
        except Exception:
            logger.exception("Error processing message %s from %s", message_id, sender)

    return {"status": "ok"}


# ── Message Handlers ──────────────────────────────────────────────────────────
def _default_itr_fy() -> str:
    """Indian financial year (Apr–Mar) as YYYY-YY."""
    now = datetime.now()
    y = now.year
    if now.month >= 4:
        return f"{y}-{str(y + 1)[-2:]}"
    return f"{y - 1}-{str(y)[-2:]}"


async def _dispatch_wa_document(
    sender: str,
    message_id: str,
    saved_path: str,
    doc_type: str,
    file_label: str,
    background_tasks: BackgroundTasks,
) -> None:
    """Acknowledge and schedule GST invoice or Form 16 background processing."""
    if doc_type == document_router.DOC_FORM16:
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                f"✅ Got your Form 16 ({file_label})!\n"
                "We are reading salary and TDS details now.\n\n"
                "We'll send a tax estimate summary in a moment. ⏳"
            ),
        )
        background_tasks.add_task(
            _background_process_form16_wa,
            sender=sender,
            message_id=message_id,
            saved_path=saved_path,
        )
    else:
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                f"✅ Got your {file_label}! "
                "We are analyzing the invoice details right now.\n\n"
                "We'll send you a summary of the extracted data in a moment. ⏳"
            ),
        )
        background_tasks.add_task(
            _background_process_invoice,
            sender=sender,
            message_id=message_id,
            saved_path=saved_path,
            file_label=file_label,
        )
    await db.clear_client_wa_routing(sender)


async def _route_incoming_wa_media(
    sender: str,
    message_id: str,
    saved_path: str,
    file_label: str,
    background_tasks: BackgroundTasks,
    *,
    original_filename: str | None = None,
    hint: str | None = None,
) -> None:
    """Classify upload and dispatch, or ask user to choose GST vs Form 16."""
    result = document_router.classify_incoming_document(
        saved_path,
        original_filename=original_filename,
        hint=hint,
    )
    doc_type = result["doc_type"]
    logger.info(
        "Document classify: type=%s confidence=%s method=%s scores=%s path=%s",
        doc_type,
        result.get("confidence"),
        result.get("method"),
        result.get("scores"),
        saved_path,
    )

    if doc_type == document_router.DOC_UNKNOWN:
        await db.set_pending_media_route(sender, saved_path, message_id)
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                f"✅ Got your {file_label}!\n\n"
                "What kind of document is this?\n"
                "• Reply *1* — GST purchase bill / invoice (ITC)\n"
                "• Reply *2* — Form 16 (salary / ITR)\n\n"
                "_Tip: you can also say \"GST bill\" or \"Form 16\" before sending the file._"
            ),
        )
        return

    await _dispatch_wa_document(
        sender, message_id, saved_path, doc_type, file_label, background_tasks
    )


async def _handle_media_message(
    msg: dict, sender: str, message_id: str, msg_type: str, background_tasks: BackgroundTasks
) -> None:
    """Handle an image or document message — download, save, and confirm."""
    media_info = msg.get(msg_type, {})
    media_id = media_info.get("id")
    mime_type = media_info.get("mime_type", "application/octet-stream")
    original_filename = media_info.get("filename")

    if not media_id:
        logger.warning("Media message without media_id — skipping")
        return

    logger.info(
        "📎 Downloading %s: media_id=%s, mime=%s, filename=%s",
        msg_type, media_id, mime_type, original_filename,
    )

    # Download the file from WhatsApp
    file_bytes, actual_mime = await whatsapp.download_media(media_id)

    # Save to disk
    saved_path = await storage.save_file(
        phone_number=sender,
        file_bytes=file_bytes,
        media_type=msg_type,
        mime_type=actual_mime or mime_type,
        original_filename=original_filename,
    )

    logger.info("💾 File saved: %s", saved_path)

    # Mark as read (blue ticks)
    await whatsapp.mark_as_read(message_id)

    file_label = "document" if msg_type == "document" else "photo"
    routing = await db.get_client_wa_routing(sender)
    hint = routing.get("pending_doc_intent")

    await _route_incoming_wa_media(
        sender,
        message_id,
        saved_path,
        file_label,
        background_tasks,
        original_filename=original_filename,
        hint=hint,
    )


async def _background_process_invoice(
    sender: str, message_id: str, saved_path: str, file_label: str
) -> None:
    """Run AI extraction, HITL routing, save to DB, and send a summary reply."""
    try:
        # Build the full absolute path of the saved file
        full_path = os.path.join(storage.STORAGE_DIR, saved_path)
        logger.info("Background task: Processing invoice at %s", full_path)

        # Run AI invoice processing
        result = await process_invoice(full_path)
        payload = result.model_dump()

        # Human-in-the-loop routing
        review_status, reasons = hitl.decide_hitl_route(payload)
        payload["review_status"] = review_status
        payload["hitl_reason"] = "; ".join(reasons) if reasons else None

        # Save extracted JSON next to the invoice file (backup).
        # Deep-copy so later DB enrichment (line-level ITC) does not mutate the backup payload.
        await storage.save_json(
            phone_number=sender,
            invoice_path_str=saved_path,
            data=copy.deepcopy(payload),
        )

        # Save extracted invoice details to SQLite/Postgres DB
        invoice_id = await db.save_invoice(
            client_phone=sender,
            file_path=saved_path,
            result=payload,
        )

        ext = result.extraction

        # Format a user-friendly summary message
        total_gst = (ext.total_cgst or 0.0) + (ext.total_sgst or 0.0) + (ext.total_igst or 0.0)

        summary_lines = [
            f"🧾 *Invoice Summary (ID: #{invoice_id})*",
            f"• *Supplier:* {ext.supplier_name or 'Not Found'}",
            f"• *GSTIN:* {ext.supplier_gstin or 'None'}",
            f"• *Invoice No:* {ext.invoice_number or 'None'}",
            f"• *Date:* {ext.invoice_date or 'None'}",
            f"• *Category:* {ext.business_category or 'Other'}",
            "",
            f"💰 *Amounts:*",
            f"• *Taxable:* ₹{ext.total_taxable_value:,.2f}",
            f"• *Total GST:* ₹{total_gst:,.2f}",
            f"• *Grand Total:* ₹{ext.grand_total:,.2f}",
            "",
            "🔍 *Verification:*",
        ]

        if result.is_calculation_correct:
            summary_lines.append("✅ *Math Check:* All calculations are correct.")
        else:
            summary_lines.append("⚠️ *Math Check:* Calculation issue detected:")
            for err in result.calculation_errors:
                summary_lines.append(f"  - {err}")

        if result.is_itc_eligible:
            summary_lines.append("✅ *ITC Eligibility:* Eligible for Input Tax Credit.")
        else:
            summary_lines.append("❌ *ITC Eligibility:* Ineligible for Input Tax Credit.")
            if result.itc_ineligibility_reason:
                summary_lines.append(f"  Reason: {result.itc_ineligibility_reason}")

        summary_body = "\n".join(summary_lines)
        summary_body += hitl.format_hitl_whatsapp_footer(invoice_id, review_status, reasons)

        # Send reply
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=summary_body,
        )

    except Exception as e:
        logger.exception("Error in background invoice processing for %s", saved_path)
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                "⚠️ Sorry, we encountered an error while processing your invoice file. "
                "This needs *human review* — please resend a clearer photo, or your CA will check it manually."
            ),
        )


async def _background_process_form16_wa(
    sender: str, message_id: str, saved_path: str
) -> None:
    """Extract Form 16 from WhatsApp upload, save to ITR return, send estimate summary."""
    try:
        extracted = await itr.extract_form16_fields(saved_path)
        fy = (extracted.get("financial_year") or "").strip() or _default_itr_fy()
        pan = (extracted.get("pan") or "").strip().upper()
        pan_valid = pan if pan and itr.validate_pan(pan) else None

        row = await db.get_or_create_itr_return(sender, fy, pan=pan_valid)
        itr_id = row["id"]
        if pan_valid:
            await db.update_client_pan(sender, pan)

        basename = os.path.basename(saved_path)
        await db.add_itr_document(
            itr_id,
            saved_path,
            doc_type="form16",
            original_filename=basename,
        )

        fields = {
            "gross_salary": extracted.get("gross_salary") or 0,
            "exemptions": extracted.get("exemptions") or 0,
            "other_income": extracted.get("other_income") or 0,
            "deductions_80c": extracted.get("deductions_80c") or 0,
            "tds": extracted.get("tds") or 0,
            "extracted_json": json.dumps(extracted),
            "status": "needs_review",
        }
        if pan_valid:
            fields["pan"] = pan_valid

        estimate = itr.compute_estimate(
            gross_salary=fields["gross_salary"],
            exemptions=fields["exemptions"],
            other_income=fields["other_income"],
            deductions_80c=fields["deductions_80c"],
            tds=fields["tds"],
            advance_tax=row.get("advance_tax") or 0,
        )
        fields["estimate_json"] = json.dumps(estimate)
        fields["regime_preferred"] = estimate["regime_preferred"]
        updated = await db.update_itr_return(itr_id, fields) or row

        gross = fields["gross_salary"]
        tds = fields["tds"]
        regime = estimate.get("regime_preferred") or "new"
        if regime == "old":
            net_tax = estimate.get("net_tax_old") or 0
            payable = estimate.get("payable_or_refund_old") or 0
        else:
            net_tax = estimate.get("net_tax_new") or 0
            payable = estimate.get("payable_or_refund_new") or 0

        summary_lines = [
            "📋 *Form 16 received*",
            f"• *FY:* {fy}",
            f"• *PAN:* {pan or updated.get('pan') or '—'}",
            f"• *Gross salary:* ₹{gross:,.0f}",
            f"• *TDS deducted:* ₹{tds:,.0f}",
            "",
            f"📊 *Tax estimate ({regime} regime)*",
            f"• *Net tax:* ₹{net_tax:,.0f}",
        ]
        if payable < 0:
            summary_lines.append(f"• *Estimated refund:* ₹{abs(payable):,.0f}")
        elif payable > 0:
            summary_lines.append(f"• *Tax payable:* ₹{payable:,.0f}")
        dash = (os.getenv("PUBLIC_BASE_URL") or "https://taxova.pro").rstrip("/")
        summary_lines.extend(
            [
                "",
                "Your CA will verify this in the dashboard before filing.",
                "",
                "📥 *Optional next step — AIS*",
                "Download your AIS JSON from incometax.gov.in and upload it in the "
                "dashboard (Income Tax → Upload AIS) to catch interest / TDS gaps vs Form 16.",
                f"Open: {dash}/dashboard",
                "",
                "Send more Form 16s or GST bills anytime. Commands: *summary* · *status*",
            ]
        )

        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body="\n".join(summary_lines),
        )
    except Exception:
        logger.exception("Error in background Form 16 processing for %s", saved_path)
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                "⚠️ We saved your Form 16 but could not read all fields automatically.\n"
                "Your CA can review it in the dashboard, or try a clearer PDF scan."
            ),
        )


async def _handle_text_message(
    msg: dict, sender: str, message_id: str, background_tasks: BackgroundTasks
) -> None:
    """Handle a text message via the GST agent (with lightweight command shortcuts)."""
    text_body = msg.get("text", {}).get("body", "").strip()
    text_lower = text_body.lower()
    logger.info("💬 Text from %s: %s", sender, text_body[:100])

    await whatsapp.mark_as_read(message_id)

    routing = await db.get_client_wa_routing(sender)
    pending_path = routing.get("pending_media_path")
    if pending_path:
        choice = document_router.parse_doc_choice_reply(text_body)
        if choice:
            await _dispatch_wa_document(
                sender,
                routing.get("pending_media_message_id") or message_id,
                pending_path,
                choice,
                "document",
                background_tasks,
            )
            return
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                "I still need to know what you uploaded.\n"
                "• Reply *1* — GST purchase bill / invoice\n"
                "• Reply *2* — Form 16 (salary / ITR)"
            ),
        )
        return

    intent = document_router.parse_doc_intent_from_text(text_body)
    if intent and not pending_path:
        await db.set_client_doc_intent(sender, intent)
        label = (
            "Form 16 (salary / ITR)"
            if intent == document_router.DOC_FORM16
            else "GST bill / invoice"
        )
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                f"👍 Got it — send your *{label}* photo or PDF next.\n\n"
                "_I'll route it to the right workflow automatically._"
            ),
        )
        return

    # ── HITL commands: CONFIRM 12 / REJECT 12 wrong gstin ──
    confirm_match = re.match(r"^(?:confirm|#?confirm)\s*#?(\d+)\s*$", text_lower)
    reject_match = re.match(r"^(?:reject|#?reject)\s*#?(\d+)\s*(.*)$", text_lower)
    if confirm_match:
        invoice_id = int(confirm_match.group(1))
        ok, msg = await db.client_confirm_invoice(invoice_id, sender)
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=f"{'✅' if ok else '⚠️'} {msg}",
        )
        return

    if reject_match:
        invoice_id = int(reject_match.group(1))
        reason = (reject_match.group(2) or "Rejected by client").strip() or "Rejected by client"
        detail = await db.get_invoice_detail(invoice_id)
        if not detail or detail.get("client_phone") != sender:
            await whatsapp.send_reply(
                to=sender,
                message_id=message_id,
                body="⚠️ Invoice not found on your account.",
            )
            return
        await db.reject_invoice(invoice_id, reason=reason, actor=sender, source="client")
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                f"❌ Invoice #{invoice_id} rejected.\n"
                f"Reason: {reason}\n\n"
                "Please send a clearer invoice photo, or ask your CA to fix it."
            ),
        )
        return

    # Fast command shortcuts (still backed by DB metrics)
    if text_lower.startswith("summary") or text_lower == "summary":
        year_month = datetime.now().strftime("%Y-%m")
        parts = text_lower.split()
        if len(parts) > 1:
            candidate = parts[1].strip()
            if len(candidate) == 7 and candidate[4] == "-":
                year_month = candidate

        metrics = await db.get_monthly_metrics(sender, year_month)
        summary_msg = (
            f"📊 *GST Monthly Summary ({year_month})*\n\n"
            f"• *Outward Sales:* ₹{metrics['sales_taxable']:,.2f}\n"
            f"• *GST Tax Liability:* ₹{metrics['sales_gst_liability']:,.2f}\n"
            f"• *Inward Expenses:* ₹{metrics['expenses_taxable']:,.2f}\n"
            f"• *Eligible ITC Claimed:* ₹{metrics['itc_claimed']:,.2f}\n\n"
            f"• *Pending CA Review:* {metrics['pending_review']} invoices\n\n"
            f"🤖 _Taxova.ai Agent_"
        )
        await whatsapp.send_reply(to=sender, message_id=message_id, body=summary_msg)
        return

    if "status" in text_lower or "filing" in text_lower:
        year_month = datetime.now().strftime("%Y-%m")
        metrics = await db.get_monthly_metrics(sender, year_month)
        status_msg = (
            f"📅 *GST Filing Status ({year_month})*\n\n"
            f"• *GSTR-1:* In Preparation (Pending CA approval of {metrics['pending_review']} invoices)\n"
            f"• *GSTR-3B:* Open (Due next month)\n\n"
            f"Please send all remaining invoice photos or PDFs directly in this chat! 📄"
        )
        await whatsapp.send_reply(to=sender, message_id=message_id, body=status_msg)
        return

    # Greetings should not depend on Groq/Gemini being up
    greeting_text = re.sub(r"[^\w\s]", "", text_lower).strip()
    if greeting_text in {
        "hi", "hii", "hiii", "hello", "hey", "helo", "hola",
        "namaste", "namaskar", "good morning", "good afternoon", "good evening",
        "yo", "ok", "okay", "thanks", "thank you", "thx",
    }:
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                "👋 Hi! I am your *Taxova.ai* agent.\n\n"
                "Send *GST invoice* photos/PDFs or your *Form 16* for ITR prep.\n"
                "You can also ask:\n"
                "• How much ITC on invoice #12?\n"
                "• Can I claim ITC on outdoor catering?\n"
                "• Show my pending invoices\n\n"
                "Commands: *summary* · *status*"
            ),
        )
        return

    # Agentic reply for everything else
    try:
        result = await agent.run_gst_agent(
            message=text_body,
            client_phone=sender,
            channel="whatsapp",
        )
        answer = result.get("response") or "Sorry, I could not answer that."
        # WhatsApp has message length limits — keep reply tight
        if len(answer) > 3500:
            answer = answer[:3400] + "\n\n…(truncated)"
        reply_body = f"{answer}\n\n🤖 _Taxova.ai Agent_"
        await whatsapp.send_reply(to=sender, message_id=message_id, body=reply_body)
    except Exception as e:
        logger.exception("Agent WhatsApp reply failed: %s", e)
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                "👋 Hi! I am your *Taxova.ai* agent.\n\n"
                "Send *GST invoice* photos/PDFs or your *Form 16* for ITR prep.\n"
                "Or ask things like:\n"
                "• How much ITC on invoice #12?\n"
                "• Can I claim ITC on outdoor catering?\n"
                "• Show my pending invoices\n\n"
                "Commands: *summary* · *status*"
            ),
        )


async def _handle_audio_message(msg: dict, sender: str, message_id: str) -> None:
    """Handle an audio message — save it and acknowledge."""
    media_info = msg.get("audio", {})
    media_id = media_info.get("id")
    mime_type = media_info.get("mime_type", "audio/ogg")

    if not media_id:
        return

    file_bytes, actual_mime = await whatsapp.download_media(media_id)
    saved_path = await storage.save_file(
        phone_number=sender,
        file_bytes=file_bytes,
        media_type="audio",
        mime_type=actual_mime or mime_type,
    )

    logger.info("🎤 Audio saved: %s", saved_path)

    await whatsapp.mark_as_read(message_id)
    await whatsapp.send_reply(
        to=sender,
        message_id=message_id,
        body=(
            "🎤 Got your voice message! "
            "For best results, please send invoices as photos or PDFs. "
            "We'll review this audio and follow up if needed."
        ),
    )


# ── Health Check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health_check():
    """Simple health check for monitoring and uptime checks."""
    return {
        "status": "healthy",
        "service": "gst-autopilot-webhook",
        "version": "0.2.2",
        "features": {
            "wa_document_routing": True,
            "form16_whatsapp_pipeline": True,
        },
    }


# ── Dashboard API Endpoints ───────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def get_landing():
    """Serves the Taxova.ai landing page (injects wa.me CTA from env)."""
    landing_path = os.path.join("static", "index.html")
    if not os.path.exists(landing_path):
        return HTMLResponse("<h1>Landing page not found.</h1>", status_code=404)
    with open(landing_path, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{WA_ME_URL}}", whatsapp_me_url())
    return HTMLResponse(html)


@app.get("/favicon.ico", include_in_schema=False)
async def get_favicon():
    """Browsers and Google request /favicon.ico for the SERP / tab icon."""
    path = os.path.join("static", "favicon.ico")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(path, media_type="image/x-icon")


@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard():
    """Serves the main CA Review Dashboard HTML page."""
    dashboard_path = os.path.join("static", "dashboard.html")
    if not os.path.exists(dashboard_path):
        raise HTTPException(status_code=404, detail="Dashboard UI not built yet.")
    return FileResponse(dashboard_path)


@app.get("/onboarding", response_class=HTMLResponse)
async def get_onboarding():
    """Serves the business onboarding page."""
    onboarding_path = os.path.join("static", "onboarding.html")
    if not os.path.exists(onboarding_path):
        return HTMLResponse("<h1>Onboarding page not found.</h1>", status_code=404)
    return FileResponse(onboarding_path)


@app.get("/success", response_class=HTMLResponse)
async def get_success():
    """Serves the onboarding success confirmation page."""
    success_path = os.path.join("static", "success.html")
    if not os.path.exists(success_path):
        return HTMLResponse("<h1>Success page not found.</h1>", status_code=404)
    return FileResponse(success_path)


@app.get("/ca-assign", response_class=HTMLResponse)
async def get_ca_assign():
    """Serves the CA assignment page (Step 2 of 3 in onboarding)."""
    ca_path = os.path.join("static", "ca_assign.html")
    if not os.path.exists(ca_path):
        return HTMLResponse("<h1>CA assignment page not found.</h1>", status_code=404)
    return FileResponse(ca_path)


@app.get("/privacy", response_class=HTMLResponse)
async def get_privacy():
    """Public privacy policy page (required by Meta to publish the app)."""
    privacy_path = os.path.join("static", "privacy.html")
    if not os.path.exists(privacy_path):
        return HTMLResponse("<h1>Privacy policy not found.</h1>", status_code=404)
    return FileResponse(privacy_path)


@app.get("/terms", response_class=HTMLResponse)
async def get_terms():
    """Public terms of service (Meta app settings)."""
    terms_path = os.path.join("static", "terms.html")
    if not os.path.exists(terms_path):
        return HTMLResponse("<h1>Terms not found.</h1>", status_code=404)
    return FileResponse(terms_path)


@app.get("/data-deletion", response_class=HTMLResponse)
async def get_data_deletion():
    """User data deletion instructions (Meta app settings)."""
    path = os.path.join("static", "data-deletion.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>Data deletion page not found.</h1>", status_code=404)
    return FileResponse(path)


@app.post("/api/ca/link")
async def api_link_ca(request: Request):
    """Validates a CA invite code and links the CA to the client session."""
    try:
        body = await request.json()
        invite_code = str(body.get("invite_code", "")).strip()
        client_phone = str(body.get("client_phone", "")).strip()

        if not invite_code or len(invite_code) != 6 or not invite_code.isdigit():
            raise HTTPException(status_code=400, detail="Invite code must be exactly 6 digits.")

        ca = await db.get_ca_by_invite_code(invite_code)
        if not ca:
            raise HTTPException(
                status_code=404,
                detail="Invite code not found. Please verify the code with your CA.",
            )

        if client_phone:
            phone_digits = "".join(c for c in client_phone if c.isdigit())
            if len(phone_digits) == 10:
                phone_digits = "91" + phone_digits
            await db.link_client_to_ca(phone_digits, invite_code)
            logger.info("CA %s linked to client %s", invite_code, phone_digits)
        else:
            logger.info("CA invite code validated (no client phone yet): %s", invite_code)

        return {
            "status": "success",
            "invite_code": invite_code,
            "ca": {
                "name": ca.get("name"),
                "firm_name": ca.get("firm_name"),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to link CA:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/onboard")
async def api_onboard_client(request: Request):
    """Processes business onboarding details (GSTIN & WhatsApp)."""
    try:
        body = await request.json()
        gstin = body.get("gstin", "").strip().upper()
        whatsapp_number = body.get("whatsapp", "").replace(" ", "").replace("-", "").strip()
        
        if not gstin or not whatsapp_number:
            raise HTTPException(status_code=400, detail="GSTIN and WhatsApp number are required.")
        
        whatsapp_digits = "".join(c for c in whatsapp_number if c.isdigit())
        if len(whatsapp_digits) == 10:
            whatsapp_digits = "91" + whatsapp_digits
        elif len(whatsapp_digits) < 10 or len(whatsapp_digits) > 13:
            raise HTTPException(status_code=400, detail="Invalid WhatsApp number format. Must be 10 digits.")

        if len(gstin) != 15 or not validate_gstin(gstin):
            raise HTTPException(
                status_code=400,
                detail="Invalid GSTIN. Must be a valid 15-character Indian GSTIN.",
            )

        # Deriving business name based on GSTIN
        client_name = f"Business {gstin[:2]}{gstin[2:7]}"
        await db.get_or_create_client(whatsapp_digits, name=client_name)
        await db.update_client_profile(whatsapp_digits, gstin, registered=True, name=client_name)
        
        logger.info("Successfully onboarded client: phone=%s, GSTIN=%s", whatsapp_digits, gstin)
        
        return {
            "status": "success",
            "client": {
                "phone_number": whatsapp_digits,
                "gstin": gstin,
                "name": client_name
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to onboard client:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/login")
async def api_ca_login(request: Request):
    """
    CA login with invite_code + password.
    Returns a session token to send as X-CA-Session on later requests.
    """
    body = await request.json()
    invite = str(body.get("invite_code") or "").strip()
    password = str(body.get("password") or "")
    if len(invite) != 6 or not invite.isdigit():
        raise HTTPException(status_code=400, detail="invite_code must be 6 digits.")
    if not password:
        raise HTTPException(status_code=400, detail="password is required.")

    ca = await db.get_ca_by_invite_code(invite)
    if not ca or not ca.get("password_hash") or not ca.get("password_salt"):
        raise HTTPException(status_code=401, detail="Invalid invite code or password.")
    if not security.verify_password(password, ca["password_salt"], ca["password_hash"]):
        await db.insert_security_audit_log(
            actor=invite,
            action="login_failed",
            resource_type="ca",
            resource_id=invite,
            ip=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=401, detail="Invalid invite code or password.")

    token = security.new_session_token()
    await db.create_ca_session(invite, token)
    await db.insert_security_audit_log(
        actor=ca.get("name") or invite,
        action="login_ok",
        resource_type="ca",
        resource_id=invite,
        ip=request.client.host if request.client else None,
    )
    firm_id = ca.get("firm_id")
    firm = None
    if firm_id is not None:
        try:
            firm = await db.get_firm(int(firm_id))
        except (TypeError, ValueError):
            firm = None
    firm_name = (firm or {}).get("name") or ca.get("firm_name")
    return {
        "ok": True,
        "session_token": token,
        "ca": {
            "invite_code": invite,
            "name": ca.get("name"),
            "firm_name": firm_name,
            "firm_id": firm_id,
            "email": ca.get("email"),
        },
        "firm": {
            "id": firm_id,
            "name": firm_name,
            "slug": (firm or {}).get("slug"),
        }
        if firm_id is not None
        else None,
        "expires_hours": 12,
    }


@app.post("/api/auth/logout")
async def api_ca_logout(
    request: Request,
    x_ca_session: str | None = Header(None, alias="X-CA-Session"),
):
    """
    Invalidate the CA session token server-side (deletes ca_sessions row).
    Client must also clear CA_SESSION_TOKEN and DASHBOARD_API_KEY from localStorage
    so logout cannot fall back to admin "see all firms".
    """
    token = (x_ca_session or "").strip()
    if not token:
        auth_h = request.headers.get("Authorization") or ""
        if auth_h.lower().startswith("bearer "):
            token = auth_h[7:].strip()

    if not token:
        return {
            "ok": True,
            "invalidated": False,
            "detail": "No session token sent. Clear CA_SESSION_TOKEN and DASHBOARD_API_KEY locally.",
            "clear_local": ["CA_SESSION_TOKEN", "CA_PROFILE", "CA_FIRM", "DASHBOARD_API_KEY"],
        }

    meta = await db.revoke_ca_session(token)
    invalidated = meta is not None
    # Confirm token is dead (defense against soft-delete mistakes)
    if await db.get_ca_session(token) is not None:
        logger.error("Logout failed to invalidate session hash for invite=%s", (meta or {}).get("ca_invite_code"))
        raise HTTPException(status_code=500, detail="Failed to invalidate session.")

    await db.insert_security_audit_log(
        actor=(meta or {}).get("name") or (meta or {}).get("ca_invite_code") or "ca",
        action="logout_ok" if invalidated else "logout_noop",
        resource_type="ca_session",
        resource_id=(meta or {}).get("ca_invite_code"),
        detail="invalidated" if invalidated else "token_already_gone",
        ip=request.client.host if request.client else None,
    )
    return {
        "ok": True,
        "invalidated": invalidated,
        "ca_invite_code": (meta or {}).get("ca_invite_code"),
        "firm_id": (meta or {}).get("firm_id"),
        "clear_local": ["CA_SESSION_TOKEN", "CA_PROFILE", "CA_FIRM", "DASHBOARD_API_KEY"],
    }


@app.get("/api/auth/me")
async def api_auth_me(_auth: security.AuthContext = Depends(require_dashboard_auth)):
    return {
        "mode": _auth.mode,
        "is_admin_key": _auth.is_admin_key,
        "ca_invite_code": _auth.ca_invite_code,
        "ca_name": _auth.ca_name,
        "ca_email": _auth.ca_email,
        "firm_id": _auth.firm_id,
        "firm_name": _auth.firm_name,
        "encryption_enabled": security.encryption_enabled(),
    }


@app.get("/api/admin/extraction-edit-stats")
async def api_extraction_edit_stats(
    days: int = Query(30, ge=1, le=365),
    firm_id: int | None = Query(None),
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """
    Platform admin: CA edit-rate on AI-extracted invoice fields (production precision signal).
    """
    require_platform_admin(_auth)
    return await db.get_extraction_edit_stats(days=days, firm_id=firm_id)


@app.post("/api/admin/extraction-edit-stats/reset")
async def api_reset_extraction_edit_stats(
    request: Request,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """
    Platform admin: day-zero wipe of extraction_field_edits + invoice_extraction_outcomes.
    Body: {"confirm": "RESET_EDIT_STATS", "firm_id": optional}
    Does not delete invoices — only pilot metrics tables.
    """
    require_platform_admin(_auth)
    try:
        body = await request.json()
    except Exception:
        body = {}
    confirm = str((body or {}).get("confirm") or "").strip()
    if confirm != "RESET_EDIT_STATS":
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"confirm": "RESET_EDIT_STATS"} (optional firm_id).',
        )
    firm_raw = (body or {}).get("firm_id")
    firm_id = None
    if firm_raw is not None and str(firm_raw).strip() != "":
        try:
            firm_id = int(firm_raw)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="firm_id must be an integer") from e
    result = await db.reset_extraction_edit_stats(firm_id=firm_id)
    await db.insert_security_audit_log(
        actor=_auth.display_name,
        action="extraction_edit_stats_reset",
        resource_type="pilot",
        resource_id=str(firm_id) if firm_id is not None else "all",
        detail=json.dumps(
            {
                "deleted_edit_events": result.get("deleted_edit_events"),
                "deleted_outcomes": result.get("deleted_outcomes"),
            }
        ),
        ip=request.client.host if request.client else None,
    )
    return result


@app.get("/api/pilot/stats")
async def api_pilot_stats(
    days: int = Query(30, ge=1, le=365),
    firm_id: int | None = Query(None),
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """
    GST pilot KPIs: approvals toward 25, edit-rate gate, filing-critical fields, reject/skip.
    CA sessions are firm-scoped; platform admin may pass firm_id or omit for all firms.
    """
    scoped_firm = firm_id
    if _auth.mode == "ca_session":
        if _auth.firm_id is None:
            raise HTTPException(status_code=403, detail="CA session missing firm.")
        scoped_firm = int(_auth.firm_id)
    elif firm_id is not None:
        require_platform_admin(_auth)
    # Admin with no firm_id → all firms; CA → always their firm
    return await db.get_pilot_stats(days=days, firm_id=scoped_firm)


@app.post("/api/admin/firms")
async def api_admin_create_firm(
    request: Request,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """
    Platform admin only (DASHBOARD_API_KEY): create a firm + first CA.
    Body: {
      "firm_name": "...", "slug": optional,
      "ca_name": "...", "invite_code": "123456", "password": "...",
      "ca_email": optional, "ca_phone": optional
    }
    """
    require_platform_admin(_auth)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")

    firm_name = str(body.get("firm_name") or "").strip()
    ca_name = str(body.get("ca_name") or "").strip()
    invite = str(body.get("invite_code") or "").strip()
    password = str(body.get("password") or "").strip()
    if not firm_name or not ca_name or not invite or not password:
        raise HTTPException(
            status_code=400,
            detail="firm_name, ca_name, invite_code, and password are required.",
        )
    try:
        firm = await db.create_firm(firm_name, slug=body.get("slug"))
        ca = await db.create_ca(
            invite_code=invite,
            name=ca_name,
            firm_id=int(firm["id"]),
            firm_name=firm_name,
            phone=(str(body.get("ca_phone") or "").strip() or None),
            email=(str(body.get("ca_email") or "").strip() or None),
            password=password,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await db.insert_security_audit_log(
        actor=_auth.display_name,
        action="firm_create",
        resource_type="firm",
        resource_id=str(firm.get("id")),
        detail=f"ca={invite}",
        ip=request.client.host if request.client else None,
    )
    return {
        "ok": True,
        "firm": firm,
        "ca": {
            "invite_code": ca.get("invite_code"),
            "name": ca.get("name"),
            "firm_id": ca.get("firm_id"),
            "firm_name": ca.get("firm_name"),
            "email": ca.get("email"),
        },
    }


@app.get("/api/files/{file_path:path}")
async def api_get_encrypted_file(
    file_path: str,
    request: Request,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """Decrypt and stream an invoice file (replaces public /storage mount)."""
    # Prevent path traversal
    rel = file_path.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        raise HTTPException(status_code=400, detail="Invalid path.")
    phone = rel.split("/", 1)[0] if "/" in rel else ""
    if phone:
        await assert_client_access(phone)
    try:
        data = storage.read_file_bytes(rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    await db.insert_security_audit_log(
        actor=_auth.display_name,
        action="file_view",
        resource_type="file",
        resource_id=rel,
        ip=request.client.host if request.client else None,
    )

    import mimetypes

    mime, _ = mimetypes.guess_type(rel)
    return Response(content=data, media_type=mime or "application/octet-stream")


@app.get("/api/clients")
async def api_get_clients(_auth: security.AuthContext = Depends(require_dashboard_auth)):
    """Returns client profiles (scoped to CA / firm when logged in via session)."""
    try:
        if _auth.can_see_all_clients:
            clients = await db.get_clients()
        elif _auth.ca_invite_code:
            clients = await db.get_clients_for_ca(_auth.ca_invite_code)
        else:
            clients = []
        return clients
    except Exception as e:
        logger.exception("Failed to get clients:")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/clients/{phone}")
async def api_update_client(
    phone: str, request: Request, _auth: security.AuthContext = Depends(require_dashboard_auth)
):
    """Update client GSTIN / name / PAN so sales & ITC / ITR can be classified."""
    await assert_client_access(phone)
    try:
        body = await request.json()
        gstin = (body.get("gstin") or "").strip().upper()
        name = (body.get("name") or "").strip() or None
        pan_raw = body.get("pan")
        pan = (pan_raw or "").strip().upper() if pan_raw is not None else None

        if gstin and not validate_gstin(gstin):
            raise HTTPException(
                status_code=400,
                detail="Invalid GSTIN. Must be a valid 15-character Indian GSTIN.",
            )
        if pan and not itr.validate_pan(pan):
            raise HTTPException(
                status_code=400,
                detail="Invalid PAN. Must be like ABCDE1234F.",
            )

        clients = await db.get_clients()
        if not any(c.get("phone_number") == phone for c in clients):
            await db.get_or_create_client(phone, name=name or "Client")

        # Preserve existing GSTIN when only PAN/name is sent
        existing = next((c for c in clients if c.get("phone_number") == phone), None)
        if not gstin and existing:
            gstin = (existing.get("gstin") or "").strip().upper()

        await db.update_client_profile(
            phone,
            gstin,
            registered=bool(gstin),
            name=name,
            pan=pan if pan_raw is not None else None,
        )
        updated = next(
            (c for c in await db.get_clients() if c.get("phone_number") == phone),
            None,
        )
        return {"status": "success", "client": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update client:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/clients/{phone}/recompute-itc")
async def api_recompute_client_itc(
    phone: str, request: Request, _auth: None = Depends(require_dashboard_auth)
):
    """Re-run Sec 17(5) ITC rules for every invoice of a client."""
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        ca_user = effective_ca_user(body)

        invoices = await db.get_invoices(client_phone=phone)
        updated = 0
        for inv in invoices:
            detail = await db.apply_line_itc_evaluation(inv["id"], ca_user=ca_user)
            if detail:
                updated += 1

        metrics = await db.get_monthly_metrics(phone, "all")
        return {"status": "success", "updated": updated, "metrics": metrics}
    except Exception as e:
        logger.exception("Failed to bulk-recompute ITC:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/invoices")
async def api_get_invoices(
    client_phone: str = Query(None),
    status: str = Query(None),
    month: str = Query(None),
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """Fetch invoices based on status, client_phone, or month filters."""
    try:
        if client_phone:
            await assert_client_access(client_phone)
            invoices = await db.get_invoices(
                client_phone,
                status,
                month,
                firm_id=_auth.firm_id if not _auth.can_see_all_clients else None,
            )
            return await _enrich_invoices_with_portal_cache(invoices)
        if not _auth.can_see_all_clients:
            linked = await db.get_clients_for_ca(_auth.ca_invite_code)
            phones = {c.get("phone_number") for c in linked}
            invoices = await db.get_invoices(
                None, status, month, firm_id=_auth.firm_id
            )
            invoices = [i for i in invoices if i.get("client_phone") in phones]
            return await _enrich_invoices_with_portal_cache(invoices)
        invoices = await db.get_invoices(client_phone, status, month)
        return await _enrich_invoices_with_portal_cache(invoices)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get invoices:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/invoices/{invoice_id}")
async def api_get_invoice_detail(
    invoice_id: int, _auth: security.AuthContext = Depends(require_dashboard_auth)
):
    """Fetch full details, line items, and audit trail of a specific invoice."""
    try:
        detail = await db.get_invoice_detail(invoice_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        await assert_client_access(detail.get("client_phone") or "")
        await db.insert_security_audit_log(
            actor=_auth.display_name,
            action="invoice_view",
            resource_type="invoice",
            resource_id=str(invoice_id),
        )
        return detail
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get invoice detail:")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/invoices/{invoice_id}")
async def api_update_invoice(
    invoice_id: int, request: Request, _auth: security.AuthContext = Depends(require_dashboard_auth)
):
    """Updates invoice metadata fields and records CA modifications."""
    try:
        body = await request.json()
        ca_user = effective_ca_user(body)
        fields_to_update = body.get("fields", {})

        # Get existing invoice to check for category change
        existing_invoice = await db.get_invoice_detail(invoice_id)
        if not existing_invoice:
            raise HTTPException(status_code=404, detail="Invoice not found.")

        await assert_client_access(existing_invoice.get("client_phone") or "")

        # ── Auto re-evaluate line-level ITC when category or recipient GSTIN changes ──
        cat_changed = (
            "business_category" in fields_to_update
            and fields_to_update["business_category"] != existing_invoice.get("business_category")
        )
        rec_changed = (
            "recipient_gstin" in fields_to_update
            and (fields_to_update.get("recipient_gstin") or "").strip().upper()
            != (existing_invoice.get("recipient_gstin") or "").strip().upper()
        )
        success = await db.update_invoice(
            invoice_id,
            fields_to_update,
            ca_user,
            ca_invite_code=_auth.ca_invite_code,
            firm_id=_auth.firm_id if _auth.firm_id is not None else existing_invoice.get("firm_id"),
        )
        if not success:
            raise HTTPException(status_code=404, detail="Invoice not found.")

        if cat_changed or rec_changed:
            await db.apply_line_itc_evaluation(invoice_id, ca_user=ca_user)

        updated_detail = await db.get_invoice_detail(invoice_id)
        return {"status": "success", "invoice": updated_detail}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update invoice:")
        raise HTTPException(status_code=500, detail=str(e))


def _line_items_summary_from_invoice(invoice: dict) -> str:
    items = invoice.get("line_items") or []
    if isinstance(items, str):
        try:
            import json as _json
            items = _json.loads(items)
        except Exception:
            return items
    if not isinstance(items, list):
        return ""
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        desc = item.get("description") or ""
        hsn = item.get("hsn_or_sac") or "N/A"
        parts.append(f"{desc} (sac/hsn: {hsn})")
    return ", ".join(parts)


@app.post("/api/invoices/{invoice_id}/recompute-itc")
async def api_recompute_itc(
    invoice_id: int, request: Request, _auth: None = Depends(require_dashboard_auth)
):
    """Re-run line-level Sec 17(5) ITC rules for an invoice."""
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        ca_user = effective_ca_user(body)

        updated = await db.apply_line_itc_evaluation(invoice_id, ca_user=ca_user)
        if not updated:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return {
            "status": "success",
            "is_itc_eligible": bool(updated.get("is_itc_eligible")),
            "itc_ineligibility_reason": updated.get("itc_ineligibility_reason"),
            "itc_eligible_gst": float(updated.get("itc_eligible_cgst") or 0)
            + float(updated.get("itc_eligible_sgst") or 0)
            + float(updated.get("itc_eligible_igst") or 0),
            "itc_blocked_gst": float(updated.get("itc_blocked_gst") or 0),
            "itc_partial": bool(updated.get("itc_partial")),
            "invoice": updated,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to recompute ITC:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/invoices/{invoice_id}/approve")
async def api_approve_invoice(
    invoice_id: int, request: Request, _auth: security.AuthContext = Depends(require_dashboard_auth)
):
    """Marks invoice as verified and approved (HITL final gate for GSTR)."""
    try:
        body = await request.json()
        ca_user = effective_ca_user(body)

        existing = await db.get_invoice_detail(invoice_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        await assert_client_access(existing.get("client_phone") or "")

        success = await db.approve_invoice(
            invoice_id,
            ca_user,
            ca_invite_code=_auth.ca_invite_code,
            firm_id=_auth.firm_id if _auth.firm_id is not None else existing.get("firm_id"),
        )
        if not success:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return {
            "status": "success",
            "review_status": "approved",
            "extraction_edit_count": existing.get("extraction_edit_count") or 0,
            "extraction_edited_fields": existing.get("extraction_edited_fields"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to approve invoice:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/invoices/{invoice_id}/reject")
async def api_reject_invoice(
    invoice_id: int, request: Request, _auth: None = Depends(require_dashboard_auth)
):
    """CA rejects an invoice in the HITL queue."""
    try:
        body = await request.json()
        ca_user = effective_ca_user(body)
        reason = (body.get("reason") or "").strip() or "Rejected by CA"
        success = await db.reject_invoice(invoice_id, reason=reason, actor=ca_user, source="ca")
        if not success:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return {"status": "success", "review_status": "rejected", "reason": reason}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to reject invoice:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/invoices/{invoice_id}/skip")
async def api_skip_invoice(
    invoice_id: int, request: Request, _auth: None = Depends(require_dashboard_auth)
):
    """Defer invoice out of the CA queue without approve/reject."""
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        ca_user = effective_ca_user(body)
        reason = (body.get("reason") or "").strip() or "Skipped by CA"
        success = await db.skip_invoice(invoice_id, reason=reason, ca_user=ca_user)
        if not success:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return {"status": "success", "review_status": "skipped", "reason": reason}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to skip invoice:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/invoices/{invoice_id}/acknowledge-mismatch")
async def api_acknowledge_mismatch(
    invoice_id: int, request: Request, _auth: None = Depends(require_dashboard_auth)
):
    """CA notes a GSTR-2B mismatch so it stops driving exception risk."""
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        ca_user = effective_ca_user(body)
        note = (body.get("note") or "").strip() or "CA noted GSTR-2B mismatch"
        success = await db.acknowledge_gstr2b_mismatch(invoice_id, note=note, ca_user=ca_user)
        if not success:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return {"status": "success", "gstr2b_match_status": "mismatch_accepted", "note": note}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to acknowledge 2B mismatch:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstr/export")
async def api_export_gstr(
    client_phone: str = Query(...),
    month: str = Query(...),
    type: str = Query(...),  # 'GSTR1' or 'GSTR3B'
    _auth: None = Depends(require_dashboard_auth),
):
    """Compiles and exports GSTR JSON offline utilities."""
    try:
        if type == "GSTR1":
            data = await gstr.generate_gstr1_json(client_phone, month)
        elif type == "GSTR3B":
            data = await gstr.generate_gstr3b_json(client_phone, month)
        else:
            raise HTTPException(status_code=400, detail="Invalid GSTR type. Choose GSTR1 or GSTR3B.")
        return data
    except Exception as e:
        logger.exception("Failed to generate GSTR export:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstin/lookup")
async def api_gstin_lookup(
    gstin: str = Query(..., min_length=15, max_length=15),
    financial_year: str | None = Query(
        None,
        description="Indian FY for filing track, e.g. 'FY 2025-26' or '2025-26'. Defaults to previous FY.",
    ),
    include_history: bool = Query(True, description="Also fetch public GSTR filing track"),
    refresh: bool = Query(False, description="Bypass 24h portal profile cache"),
    compare_name: str | None = Query(None, description="Optional bill party name to assess mismatch"),
    role: str = Query("supplier", description="supplier | recipient — for assessment labels"),
    _auth: None = Depends(require_dashboard_auth),
):
    """
    Public GSTIN search via Sandbox (Quicko GSP).
    Profile is cached ~24h. Filing history is fetched live when requested.
    """
    if not sandbox.configured():
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "sandbox_not_configured",
                "user_message": (
                    "GSTN lookup is not configured. Invoice data is fine — "
                    "you can still review, edit, and approve."
                ),
            },
        )
    try:
        profile, from_cache = await _get_gstin_profile_cached(gstin, refresh=refresh)
        if include_history:
            result = dict(profile)
            fy = (financial_year or "").strip() or sandbox.previous_financial_year()
            try:
                history = sandbox.track_gst_returns(gstin, fy)
                if not financial_year and history.get("count", 0) == 0:
                    current = sandbox.indian_financial_year()
                    if current != history.get("financial_year"):
                        alt = sandbox.track_gst_returns(gstin, current)
                        if alt.get("count", 0) > 0:
                            history = alt
                result["filing_history"] = history
            except Exception as e:
                logger.warning("Filing history fetch failed for %s: %s", gstin, e)
                result["filing_history"] = {
                    "financial_year": fy if str(fy).upper().startswith("FY") else f"FY {fy}",
                    "filings": [],
                    "count": 0,
                    "message": str(e),
                    "error_code": "fetch_failed",
                }
        else:
            result = dict(profile)
            result["filing_history"] = None

        result["from_cache"] = from_cache
        result["assessment"] = sandbox.assess_portal_party(
            profile, compare_name, role=role if role in ("supplier", "recipient") else "supplier"
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.warning("Sandbox GSTIN lookup failed: %s", e)
        classified = sandbox.classify_sandbox_error(e)
        raise HTTPException(
            status_code=502,
            detail={
                "error_code": classified.get("error_code"),
                "user_message": classified.get("user_message"),
                "technical": classified.get("technical"),
            },
        )
    except Exception as e:
        logger.exception("Unexpected Sandbox GSTIN lookup error:")
        classified = sandbox.classify_sandbox_error(e)
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": classified.get("error_code") or "sandbox_error",
                "user_message": classified.get("user_message"),
                "technical": classified.get("technical") or str(e)[:400],
            },
        )

@app.post("/api/gstin/cache/warm")
async def api_gstin_cache_warm(
    request: Request,
    _auth: None = Depends(require_dashboard_auth),
):
    """
    Warm portal cache for unique GSTINs (max 10 per call) and return assessments.
    Body: { "parties": [ {"gstin": "...", "name": "...", "role": "supplier" }, ... ] }
    """
    if not sandbox.configured():
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "sandbox_not_configured",
                "user_message": (
                    "GSTN lookup is not configured. Invoice data is fine — "
                    "you can still review, edit, and approve."
                ),
            },
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")

    parties = body.get("parties") if isinstance(body, dict) else None
    if not isinstance(parties, list):
        raise HTTPException(status_code=400, detail="'parties' array required")

    # Dedupe by GSTIN, keep first name/role
    seen: dict[str, dict] = {}
    for p in parties:
        if not isinstance(p, dict):
            continue
        g = str(p.get("gstin") or "").strip().upper()
        if len(g) != 15 or g in seen:
            continue
        seen[g] = {
            "gstin": g,
            "name": p.get("name"),
            "role": p.get("role") if p.get("role") in ("supplier", "recipient") else "supplier",
        }
        if len(seen) >= 10:
            break

    assessments: dict[str, dict] = {}
    fetched = 0
    for g, meta in seen.items():
        try:
            profile, from_cache = await _get_gstin_profile_cached(g, refresh=False)
            if not from_cache:
                fetched += 1
            assessments[g] = sandbox.assess_portal_party(
                profile, meta.get("name"), role=meta["role"]
            )
            assessments[g]["from_cache"] = from_cache
        except ValueError as e:
            assessments[g] = sandbox.assess_portal_party(
                None, meta.get("name"), role=meta["role"], error_message=str(e)
            )
        except Exception as e:
            logger.warning("Warm cache failed for %s: %s", g, e)
            try:
                await db.upsert_gstin_portal_cache(g, profile=None, error_message=str(e))
            except Exception:
                pass
            assessments[g] = sandbox.assess_portal_party(
                None, meta.get("name"), role=meta["role"], error_message=str(e)
            )

    return {"assessments": assessments, "fetched": fetched, "count": len(assessments)}


async def _get_gstin_profile_cached(gstin: str, *, refresh: bool = False) -> tuple[dict, bool]:
    """Return (profile, from_cache)."""
    g = str(gstin or "").strip().upper()
    if not refresh:
        row = await db.get_gstin_portal_cache(g)
        if row and sandbox.portal_cache_is_fresh(row.get("checked_at")):
            if row.get("profile"):
                return row["profile"], True
            if row.get("error_message"):
                # Cached API failure — mark as portal_outage so UI does not say "not found"
                return {
                    "gstin": g,
                    "found": False,
                    "message": row.get("error_message"),
                    "legal_name": None,
                    "status": None,
                    "portal_outage": sandbox.is_portal_api_outage_message(
                        row.get("error_message")
                    ),
                }, True

    try:
        profile = sandbox.search_gstin(g)
        await db.upsert_gstin_portal_cache(g, profile=profile, error_message=None)
        return profile, False
    except ValueError:
        raise
    except Exception as e:
        await db.upsert_gstin_portal_cache(g, profile=None, error_message=str(e))
        raise


async def _enrich_invoices_with_portal_cache(invoices: list[dict]) -> list[dict]:
    """Attach portal_supplier / portal_recipient assessments from cache (no live Sandbox calls)."""
    if not invoices:
        return invoices
    gstins = []
    for inv in invoices:
        for key in ("supplier_gstin", "recipient_gstin"):
            g = (inv.get(key) or "").strip().upper()
            if len(g) == 15:
                gstins.append(g)
    cache = await db.get_gstin_portal_cache_many(gstins)
    for inv in invoices:
        sg = (inv.get("supplier_gstin") or "").strip().upper()
        rg = (inv.get("recipient_gstin") or "").strip().upper()
        inv["portal_supplier"] = None
        inv["portal_recipient"] = None
        if sg and sg in cache:
            row = cache[sg]
            if sandbox.portal_cache_is_fresh(row.get("checked_at")):
                inv["portal_supplier"] = sandbox.assess_portal_party(
                    row.get("profile"),
                    inv.get("supplier_name"),
                    role="supplier",
                    error_message=row.get("error_message") if not row.get("profile") else None,
                )
        if rg and rg in cache:
            row = cache[rg]
            if sandbox.portal_cache_is_fresh(row.get("checked_at")):
                inv["portal_recipient"] = sandbox.assess_portal_party(
                    row.get("profile"),
                    inv.get("recipient_name"),
                    role="recipient",
                    error_message=row.get("error_message") if not row.get("profile") else None,
                )
    return invoices


@app.post("/api/gstr2b/import")
async def api_import_gstr2b(
    request: Request,
    client_phone: str = Query(...),
    return_period: str | None = Query(None, description="YYYY-MM; inferred from data if omitted"),
    reconcile: bool = Query(True),
    _auth: None = Depends(require_dashboard_auth),
):
    """
    Import GSTR-2B for a client (JSON body or multipart file).
    Replaces existing entries for the return period, optionally runs reconciliation.
    """
    import gstr2b as gstr2b_mod

    content_type = (request.headers.get("content-type") or "").lower()
    period = return_period
    entries: list = []

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("file")
            if upload is None:
                raise HTTPException(status_code=400, detail="multipart field 'file' is required")
            raw_name = getattr(upload, "filename", "") or "upload.json"
            raw_bytes = await upload.read()
            text = raw_bytes.decode("utf-8-sig", errors="replace")
            if raw_name.lower().endswith(".csv") or text.lstrip().startswith("supplier_"):
                period_parsed, entries = gstr2b_mod.parse_gstr2b_csv(text)
            else:
                payload = json.loads(text)
                period_parsed, entries = gstr2b_mod.parse_gstr2b_json(payload)
            period = period or period_parsed
            ca_user = effective_ca_user({"ca_user": form.get("ca_user")})
        else:
            body = await request.json()
            if isinstance(body, dict) and body.get("return_period") and not period:
                period = body.get("return_period")
            period_parsed, entries = gstr2b_mod.parse_gstr2b_json(body)
            period = period or period_parsed
            ca_user = effective_ca_user(body if isinstance(body, dict) else None)

        return await _ingest_gstr2b_entries(
            client_phone=client_phone,
            period=period,
            entries=entries,
            reconcile=reconcile,
            ca_user=ca_user,
            source="file_upload",
        )
    except HTTPException:
        raise
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.exception("GSTR-2B import failed:")
        raise HTTPException(status_code=500, detail=str(e))


async def _ingest_gstr2b_entries(
    *,
    client_phone: str,
    period: str | None,
    entries: list,
    reconcile: bool = True,
    ca_user: str = "CA Operator",
    source: str = "file_upload",
) -> dict:
    if not period:
        raise HTTPException(
            status_code=400,
            detail="Could not determine return_period (YYYY-MM). Pass ?return_period=2026-07",
        )
    if not re.match(r"^\d{4}-\d{2}$", str(period)):
        raise HTTPException(status_code=400, detail="return_period must be YYYY-MM")
    if not entries:
        raise HTTPException(status_code=400, detail="No usable B2B invoice rows found in GSTR-2B data")

    inserted = await db.replace_gstr2b_entries(client_phone, period, entries)
    result = {
        "ok": True,
        "return_period": period,
        "imported": len(inserted),
        "ca_user": ca_user,
        "source": source,
    }
    if reconcile:
        recon = await db.reconcile_gstr2b_for_client(client_phone, period)
        result["reconcile"] = recon
    return result


@app.get("/api/clients/{phone}/gst-session")
async def api_gst_session_status(phone: str, _auth: None = Depends(require_dashboard_auth)):
    """Return taxpayer OTP session status (no access token)."""
    client = await db.get_or_create_client(phone)
    session = await db.get_gst_taxpayer_session(phone)
    return {
        "client_phone": phone,
        "gstin": client.get("gstin"),
        "gst_portal_username": client.get("gst_portal_username"),
        "session": session,
        "sandbox_configured": sandbox.configured(),
    }


@app.post("/api/clients/{phone}/gst-session/otp")
async def api_gst_session_request_otp(
    phone: str,
    request: Request,
    _auth: None = Depends(require_dashboard_auth),
):
    """
    Request GST portal OTP for this client's GSTIN.
    Body: { "username": "<gst portal username>" }
    Prerequisite: Enable API Access on gst.gov.in for the user.
    """
    if not sandbox.configured():
        raise HTTPException(
            status_code=503,
            detail="Sandbox not configured. Set SANDBOX_API_KEY and SANDBOX_API_SECRET in .env.",
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    client = await db.get_or_create_client(phone)
    gstin = (client.get("gstin") or "").strip().upper()
    if not gstin or not validate_gstin(gstin):
        raise HTTPException(
            status_code=400,
            detail="Set a valid client GSTIN first (edit Client GSTIN on the dashboard).",
        )
    username = (body.get("username") or client.get("gst_portal_username") or "").strip()
    if not username:
        raise HTTPException(
            status_code=400,
            detail="GST portal username is required (same login used on gst.gov.in).",
        )
    try:
        result = sandbox.request_taxpayer_otp(username, gstin)
        try:
            await db.update_client_gst_portal_username(phone, username)
        except Exception:
            logger.warning("Could not persist gst_portal_username for %s", phone)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.exception("OTP request failed:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/clients/{phone}/gst-session/verify")
async def api_gst_session_verify_otp(
    phone: str,
    request: Request,
    _auth: None = Depends(require_dashboard_auth),
):
    """
    Verify GST portal OTP and store taxpayer session (~6h).
    Body: { "otp": "123456", "username": optional }
    """
    if not sandbox.configured():
        raise HTTPException(
            status_code=503,
            detail="Sandbox not configured. Set SANDBOX_API_KEY and SANDBOX_API_SECRET in .env.",
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")

    client = await db.get_or_create_client(phone)
    gstin = (client.get("gstin") or "").strip().upper()
    if not gstin or not validate_gstin(gstin):
        raise HTTPException(status_code=400, detail="Set a valid client GSTIN first.")

    username = (body.get("username") or client.get("gst_portal_username") or "").strip()
    otp = str(body.get("otp") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="GST portal username is required")
    if not otp:
        raise HTTPException(status_code=400, detail="OTP is required")

    try:
        verified = sandbox.verify_taxpayer_otp(username, gstin, otp)
        session = await db.upsert_gst_taxpayer_session(
            phone,
            gstin=gstin,
            username=username,
            access_token=verified["access_token"],
            token_expiry=verified.get("token_expiry"),
            session_expiry=verified.get("session_expiry"),
        )
        return {"ok": True, "session": session, "message": "GST portal connected. Session valid ~6 hours."}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.exception("OTP verify failed:")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/clients/{phone}/gst-session")
async def api_gst_session_clear(phone: str, _auth: None = Depends(require_dashboard_auth)):
    await db.clear_gst_taxpayer_session(phone)
    return {"ok": True}


@app.post("/api/clients/{phone}/gstr2b/fetch")
async def api_fetch_gstr2b_live(
    phone: str,
    return_period: str = Query(..., description="YYYY-MM"),
    reconcile: bool = Query(True),
    _auth: None = Depends(require_dashboard_auth),
):
    """
    Pull live GSTR-2B for the period using an active taxpayer OTP session,
    then run the same import + reconcile pipeline as file upload.
    """
    import gstr2b as gstr2b_mod

    if not sandbox.configured():
        raise HTTPException(
            status_code=503,
            detail="Sandbox not configured. Set SANDBOX_API_KEY and SANDBOX_API_SECRET in .env.",
        )
    if not re.match(r"^\d{4}-\d{2}$", return_period):
        raise HTTPException(status_code=400, detail="return_period must be YYYY-MM")

    client = await db.get_or_create_client(phone)
    gstin = (client.get("gstin") or "").strip().upper()
    if not gstin or not validate_gstin(gstin):
        raise HTTPException(status_code=400, detail="Set a valid client GSTIN first.")

    session = await db.get_gst_taxpayer_session(phone, include_token=True)
    if not session or not session.get("active") or not session.get("access_token"):
        raise HTTPException(
            status_code=401,
            detail="No active GST portal session. Connect with OTP first.",
        )

    try:
        doc = sandbox.fetch_gstr2b_document(
            return_period,
            taxpayer_access_token=session["access_token"],
        )
        period_parsed, entries = gstr2b_mod.parse_gstr2b_json(doc)
        period = period_parsed or return_period
        result = await _ingest_gstr2b_entries(
            client_phone=phone,
            period=period,
            entries=entries,
            reconcile=reconcile,
            ca_user="CA Dashboard (live 2B)",
            source="portal_otp",
        )
        result["gstin"] = gstin
        return result
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        msg = str(e)
        if "expired" in msg.lower() or "session" in msg.lower():
            await db.clear_gst_taxpayer_session(phone)
            raise HTTPException(status_code=401, detail=msg)
        raise HTTPException(status_code=502, detail=msg)
    except Exception as e:
        logger.exception("Live GSTR-2B fetch failed:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/clients/{phone}/gstr2b/reconcile")
async def api_reconcile_gstr2b(
    phone: str,
    return_period: str = Query(..., description="YYYY-MM"),
    _auth: None = Depends(require_dashboard_auth),
):
    """Re-run books ↔ GSTR-2B matching for an imported period."""
    if not re.match(r"^\d{4}-\d{2}$", return_period):
        raise HTTPException(status_code=400, detail="return_period must be YYYY-MM")
    result = await db.reconcile_gstr2b_for_client(phone, return_period)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Reconcile failed")
    return result


@app.get("/api/clients/{phone}/gstr2b")
async def api_list_gstr2b(
    phone: str,
    return_period: str | None = Query(None),
    _auth: None = Depends(require_dashboard_auth),
):
    """List imported GSTR-2B entries (optionally filtered by period)."""
    entries = await db.get_gstr2b_entries(phone, return_period)
    return {
        "client_phone": phone,
        "return_period": return_period,
        "count": len(entries),
        "entries": entries,
    }


@app.get("/api/metrics")
async def api_get_metrics(
    client_phone: str = Query(...),
    month: str = Query(None),
    _auth: None = Depends(require_dashboard_auth),
):
    """
    Fetch KPI aggregates for a client.
    Pass month=YYYY-MM for one month, or omit / month=all for all invoices.
    """
    try:
        metrics = await db.get_monthly_metrics(client_phone, month)
        return metrics
    except Exception as e:
        logger.exception("Failed to fetch dashboard metrics:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/clients/{phone}/nudge/preview")
async def api_nudge_preview(
    phone: str,
    return_period: str = Query(..., description="YYYY-MM"),
    _auth: None = Depends(require_dashboard_auth),
):
    """Preview WhatsApp month-close nudge text (does not send)."""
    import re as _re
    import nudge as nudge_mod

    if not _re.match(r"^\d{4}-\d{2}$", (return_period or "").strip()):
        raise HTTPException(status_code=400, detail="return_period must be YYYY-MM")
    client = await db.get_or_create_client(phone)
    invoices = await db.get_invoices(client_phone=phone, month=return_period.strip())
    payload = nudge_mod.build_month_nudge_message(
        client_name=client.get("name"),
        return_period=return_period.strip(),
        invoices=invoices,
    )
    payload["client_phone"] = phone
    payload["client_name"] = client.get("name")
    return payload


@app.post("/api/clients/{phone}/nudge")
async def api_nudge_send(
    phone: str,
    request: Request,
    _auth: None = Depends(require_dashboard_auth),
):
    """
    Send (or preview) a WhatsApp nudge for a month.
    Body: { "return_period": "YYYY-MM", "message"?: "...", "send"?: true }
    If send is false, returns preview only.
    """
    import re as _re
    import nudge as nudge_mod

    try:
        body = await request.json()
    except Exception:
        body = {}
    return_period = (body.get("return_period") or "").strip()
    if not _re.match(r"^\d{4}-\d{2}$", return_period):
        raise HTTPException(status_code=400, detail="return_period must be YYYY-MM")

    client = await db.get_or_create_client(phone)
    invoices = await db.get_invoices(client_phone=phone, month=return_period)
    built = nudge_mod.build_month_nudge_message(
        client_name=client.get("name"),
        return_period=return_period,
        invoices=invoices,
    )
    message = (body.get("message") or "").strip() or built["message"]
    send = body.get("send", True)
    result = {
        **built,
        "message": message,
        "client_phone": phone,
        "client_name": client.get("name"),
        "sent": False,
    }
    if not send:
        return result

    wa = await whatsapp.send_text_message(phone, message)
    result["sent"] = True
    result["whatsapp"] = wa
    return result


@app.get("/api/itc/summary")
async def api_get_itc_summary(
    client_phone: str = Query(...),
    months: int = Query(12),
    _auth: None = Depends(require_dashboard_auth),
):
    """Returns month-by-month ITC trend for the last N months — feeds the sparkline chart."""
    try:
        trend = await db.get_itc_monthly_trend(client_phone, num_months=min(months, 24))
        return trend
    except Exception as e:
        logger.exception("Failed to fetch ITC trend summary:")
        raise HTTPException(status_code=500, detail=str(e))


# ── Income Tax Phase 1 (Form 16 → estimate → CA approve → export draft) ───────

def _enrich_itr_return_row(row: dict) -> dict:
    """Parse JSON blobs on an itr_returns row for dashboard consumption."""
    if row.get("estimate_json") and isinstance(row["estimate_json"], str):
        try:
            row["estimate"] = json.loads(row["estimate_json"])
        except (TypeError, json.JSONDecodeError):
            row["estimate"] = None
    if row.get("extracted_json") and isinstance(row["extracted_json"], str):
        try:
            row["extracted"] = json.loads(row["extracted_json"])
        except (TypeError, json.JSONDecodeError):
            row["extracted"] = None
    if row.get("ais_json") and isinstance(row["ais_json"], str):
        try:
            row["ais"] = json.loads(row["ais_json"])
        except (TypeError, json.JSONDecodeError):
            row["ais"] = None
    return row


@app.get("/api/itr/returns")
async def api_list_itr_returns(
    client_phone: str = Query(...),
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    await assert_client_access(client_phone)
    returns = await db.list_itr_returns(client_phone)
    for r in returns:
        _enrich_itr_return_row(r)
        r["documents"] = await db.list_itr_documents(r["id"])
    return returns


@app.post("/api/itr/returns")
async def api_create_itr_return(
    request: Request,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    body = await request.json()
    phone = (body.get("client_phone") or "").strip()
    fy = (body.get("financial_year") or "").strip()
    pan = (body.get("pan") or "").strip().upper() or None
    if not phone or not fy:
        raise HTTPException(status_code=400, detail="client_phone and financial_year are required.")
    if pan and not itr.validate_pan(pan):
        raise HTTPException(status_code=400, detail="Invalid PAN.")
    await assert_client_access(phone)
    await db.get_or_create_client(phone)
    row = await db.get_or_create_itr_return(phone, fy, pan=pan)
    if pan:
        await db.update_client_pan(phone, pan)
    return row


@app.get("/api/itr/returns/{itr_id}")
async def api_get_itr_return(
    itr_id: int,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    row = await db.get_itr_return(itr_id)
    if not row:
        raise HTTPException(status_code=404, detail="ITR return not found.")
    await assert_client_access(row["client_phone"])
    _enrich_itr_return_row(row)
    row["documents"] = await db.list_itr_documents(itr_id)
    return row


@app.put("/api/itr/returns/{itr_id}")
async def api_update_itr_return(
    itr_id: int,
    request: Request,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    row = await db.get_itr_return(itr_id)
    if not row:
        raise HTTPException(status_code=404, detail="ITR return not found.")
    await assert_client_access(row["client_phone"])
    body = await request.json()
    pan = (body.get("pan") or "").strip().upper() if body.get("pan") is not None else None
    if pan and not itr.validate_pan(pan):
        raise HTTPException(status_code=400, detail="Invalid PAN.")

    fields = {}
    for key in (
        "gross_salary", "exemptions", "other_income", "deductions_80c",
        "tds", "advance_tax", "ca_notes", "financial_year", "regime_preferred",
    ):
        if key in body:
            fields[key] = body[key]
    if pan is not None:
        fields["pan"] = pan
        await db.update_client_pan(row["client_phone"], pan)

    money_keys = {"gross_salary", "exemptions", "other_income", "deductions_80c", "tds", "advance_tax"}
    if money_keys & set(fields.keys()):
        merged = {**row, **fields}
        estimate = itr.compute_estimate(
            gross_salary=merged.get("gross_salary") or 0,
            exemptions=merged.get("exemptions") or 0,
            other_income=merged.get("other_income") or 0,
            deductions_80c=merged.get("deductions_80c") or 0,
            tds=merged.get("tds") or 0,
            advance_tax=merged.get("advance_tax") or 0,
        )
        fields["estimate_json"] = json.dumps(estimate)
        fields["regime_preferred"] = fields.get("regime_preferred") or estimate["regime_preferred"]
        if row.get("status") in (None, "draft", "exported"):
            fields["status"] = "needs_review"

    updated = await db.update_itr_return(itr_id, fields)
    if updated:
        _enrich_itr_return_row(updated)
        updated["documents"] = await db.list_itr_documents(itr_id)
    return updated


@app.post("/api/itr/returns/{itr_id}/form16")
async def api_upload_form16(
    itr_id: int,
    file: UploadFile = File(...),
    extract: bool = Query(True),
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """Upload Form 16 (PDF/image) for an ITR return; optionally AI-extract fields."""
    row = await db.get_itr_return(itr_id)
    if not row:
        raise HTTPException(status_code=404, detail="ITR return not found.")
    await assert_client_access(row["client_phone"])

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 15 MB).")

    mime = (file.content_type or "").lower() or "application/pdf"
    if mime not in ("application/pdf", "image/jpeg", "image/png", "image/webp", "image/jpg"):
        name = (file.filename or "").lower()
        if name.endswith(".pdf"):
            mime = "application/pdf"
        elif name.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif name.endswith(".png"):
            mime = "image/png"
        elif name.endswith(".webp"):
            mime = "image/webp"
        else:
            raise HTTPException(status_code=400, detail="Upload PDF or image (jpg/png/webp).")

    media_type = "document" if mime == "application/pdf" else "image"
    rel = await storage.save_file(
        row["client_phone"],
        content,
        media_type,
        mime,
        original_filename=file.filename or "form16.pdf",
    )
    doc = await db.add_itr_document(
        itr_id, rel, doc_type="form16", original_filename=file.filename
    )

    extracted = None
    estimate = None
    updated = row
    if extract:
        try:
            extracted = await itr.extract_form16_fields(rel)
            fields = {
                "gross_salary": extracted.get("gross_salary") or 0,
                "exemptions": extracted.get("exemptions") or 0,
                "other_income": extracted.get("other_income") or 0,
                "deductions_80c": extracted.get("deductions_80c") or 0,
                "tds": extracted.get("tds") or 0,
                "extracted_json": json.dumps(extracted),
                "status": "needs_review",
            }
            pan = (extracted.get("pan") or "").strip().upper()
            if pan and itr.validate_pan(pan):
                fields["pan"] = pan
                await db.update_client_pan(row["client_phone"], pan)
            fy = extracted.get("financial_year")
            if fy:
                fields["financial_year"] = fy
            estimate = itr.compute_estimate(
                gross_salary=fields["gross_salary"],
                exemptions=fields["exemptions"],
                other_income=fields["other_income"],
                deductions_80c=fields["deductions_80c"],
                tds=fields["tds"],
                advance_tax=row.get("advance_tax") or 0,
            )
            fields["estimate_json"] = json.dumps(estimate)
            fields["regime_preferred"] = estimate["regime_preferred"]
            updated = await db.update_itr_return(itr_id, fields)
        except Exception as e:
            logger.exception("Form 16 extraction failed:")
            return {
                "status": "uploaded",
                "document": doc,
                "itr": updated,
                "extract_error": str(e),
                "hint": "File saved. Edit salary / TDS fields manually, then Save estimate.",
            }

    if updated and updated.get("estimate_json") and not estimate:
        try:
            estimate = json.loads(updated["estimate_json"])
        except (TypeError, json.JSONDecodeError):
            pass
    if updated:
        _enrich_itr_return_row(updated)
    return {
        "status": "ok",
        "document": doc,
        "itr": updated,
        "extracted": extracted,
        "estimate": estimate,
    }


@app.post("/api/itr/returns/{itr_id}/ais")
async def api_upload_ais(
    itr_id: int,
    file: UploadFile = File(...),
    password: str = Form(""),
    dob: str = Form(""),
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """
    Upload AIS JSON (plain demo JSON or portal encrypted JSON).
    Encrypted files need password = PAN+DOB (ddmmyyyy) or full AIS Utility password.
    Reconciles against Form 16 / saved ITR fields.
    """
    row = await db.get_itr_return(itr_id)
    if not row:
        raise HTTPException(status_code=404, detail="ITR return not found.")
    await assert_client_access(row["client_phone"])

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 20 MB).")

    name = (file.filename or "ais.json").strip()
    if not name.lower().endswith((".json", ".txt")):
        raise HTTPException(status_code=400, detail="Upload AIS as .json (portal download or plain JSON).")

    try:
        summary, payload = ais_mod.load_ais_bytes(
            content,
            filename=name,
            pan=row.get("pan") or "",
            dob_ddmmyyyy=dob or "",
            password=password or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("AIS parse failed:")
        raise HTTPException(status_code=400, detail=f"Could not read AIS file: {e}") from e

    # Store plaintext JSON (never store the encryption password)
    rel = await storage.save_file(
        row["client_phone"],
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        "document",
        "application/json",
        original_filename=name if name.lower().endswith(".json") else f"{name}.json",
    )
    doc = await db.add_itr_document(
        itr_id, rel, doc_type="ais", original_filename=name
    )

    ais_blob = {"summary": summary, "uploaded_filename": name}
    updated = await db.update_itr_return(
        itr_id,
        {
            "ais_json": json.dumps(ais_blob),
            "status": "needs_review" if row.get("status") in (None, "draft", "exported") else row.get("status"),
        },
    )
    if updated:
        _enrich_itr_return_row(updated)
        updated["documents"] = await db.list_itr_documents(itr_id)

    reconcile = ais_mod.reconcile_ais_vs_return(summary, updated or row)
    return {
        "status": "ok",
        "document": doc,
        "itr": updated,
        "ais": summary,
        "reconcile": reconcile,
        "hint": (
            "No government approval required — you uploaded a file the taxpayer already downloaded. "
            "Review mismatches before filing on incometax.gov.in."
        ),
    }


@app.get("/api/itr/returns/{itr_id}/ais/reconcile")
async def api_reconcile_ais(
    itr_id: int,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """Re-run AIS vs Form 16 / return field comparison using stored AIS summary."""
    row = await db.get_itr_return(itr_id)
    if not row:
        raise HTTPException(status_code=404, detail="ITR return not found.")
    await assert_client_access(row["client_phone"])
    _enrich_itr_return_row(row)
    ais_blob = row.get("ais")
    if not ais_blob or not isinstance(ais_blob, dict):
        raise HTTPException(status_code=404, detail="No AIS uploaded for this return yet.")
    summary = ais_blob.get("summary") or ais_blob
    reconcile = ais_mod.reconcile_ais_vs_return(summary, row)
    return {"status": "ok", "ais": summary, "reconcile": reconcile, "itr": row}


@app.post("/api/itr/returns/{itr_id}/estimate")
async def api_recompute_itr_estimate(
    itr_id: int,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    row = await db.get_itr_return(itr_id)
    if not row:
        raise HTTPException(status_code=404, detail="ITR return not found.")
    await assert_client_access(row["client_phone"])
    estimate = itr.compute_estimate(
        gross_salary=row.get("gross_salary") or 0,
        exemptions=row.get("exemptions") or 0,
        other_income=row.get("other_income") or 0,
        deductions_80c=row.get("deductions_80c") or 0,
        tds=row.get("tds") or 0,
        advance_tax=row.get("advance_tax") or 0,
    )
    updated = await db.update_itr_return(
        itr_id,
        {
            "estimate_json": json.dumps(estimate),
            "regime_preferred": estimate["regime_preferred"],
            "status": "needs_review" if row.get("status") == "draft" else row.get("status"),
        },
    )
    return {"status": "ok", "estimate": estimate, "itr": updated}


@app.post("/api/itr/returns/{itr_id}/approve")
async def api_approve_itr(
    itr_id: int,
    request: Request,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    row = await db.get_itr_return(itr_id)
    if not row:
        raise HTTPException(status_code=404, detail="ITR return not found.")
    await assert_client_access(row["client_phone"])
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    notes = (body.get("ca_notes") or "").strip() or row.get("ca_notes")
    updated = await db.update_itr_return(
        itr_id, {"status": "approved", "ca_notes": notes}
    )
    if updated:
        _enrich_itr_return_row(updated)
        updated["documents"] = await db.list_itr_documents(itr_id)
    return {"status": "ok", "itr": updated}


@app.get("/api/itr/returns/{itr_id}/export")
async def api_export_itr_draft(
    itr_id: int,
    _auth: security.AuthContext = Depends(require_dashboard_auth),
):
    """HTML prep draft for print / Save as PDF — not a government filing."""
    row = await db.get_itr_return(itr_id)
    if not row:
        raise HTTPException(status_code=404, detail="ITR return not found.")
    await assert_client_access(row["client_phone"])

    estimate = None
    if row.get("estimate_json"):
        try:
            estimate = json.loads(row["estimate_json"])
        except (TypeError, json.JSONDecodeError):
            estimate = None
    if not estimate:
        estimate = itr.compute_estimate(
            gross_salary=row.get("gross_salary") or 0,
            exemptions=row.get("exemptions") or 0,
            other_income=row.get("other_income") or 0,
            deductions_80c=row.get("deductions_80c") or 0,
            tds=row.get("tds") or 0,
            advance_tax=row.get("advance_tax") or 0,
        )
        await db.update_itr_return(
            itr_id,
            {
                "estimate_json": json.dumps(estimate),
                "regime_preferred": estimate["regime_preferred"],
            },
        )

    clients = await db.get_clients()
    client = next(
        (c for c in clients if c.get("phone_number") == row["client_phone"]),
        {"phone_number": row["client_phone"], "name": "Client", "pan": row.get("pan")},
    )
    html = itr.build_export_html(client, row, estimate)
    new_status = "exported" if row.get("status") == "approved" else (row.get("status") or "needs_review")
    await db.update_itr_return(itr_id, {"status": new_status})
    return HTMLResponse(content=html)


@app.post("/api/chat")
async def api_chat_assistant(request: Request, _auth: None = Depends(require_dashboard_auth)):
    """
    Agentic GST assistant. Uses tools (rules search, ITC, invoices, metrics).
    Optional body.client_phone scopes tools to the selected dashboard client.
    """
    try:
        body = await request.json()
        message = body.get("message", "").strip()
        client_phone = (body.get("client_phone") or "").strip() or None
        if not message:
            raise HTTPException(status_code=400, detail="Message content cannot be empty.")

        result = await agent.run_gst_agent(
            message=message,
            client_phone=client_phone,
            channel="dashboard",
        )
        return {
            "response": result.get("response", ""),
            "steps": result.get("steps", []),
            "model": result.get("model"),
            "agentic": True,
        }
    except Exception as e:
        logger.exception("Error in chat assistant:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rag/reindex")
async def api_reindex_rag(_auth: None = Depends(require_dashboard_auth)):
    """
    Forces clearing and re-indexing of the RAG reference documents.
    """
    try:
        # Clear existing knowledge base (ChromaDB)
        rag.clear_knowledge_base()
        
        # Seed from reference docs folder
        ref_dir = os.path.join(os.getcwd(), "reference_docs")
        if not os.path.exists(ref_dir):
            raise HTTPException(status_code=404, detail="reference_docs directory not found.")
            
        files = [f for f in os.listdir(ref_dir) if f.endswith(".md") or f.endswith(".txt")]
        total_chunks = 0
        
        for filename in files:
            filepath = os.path.join(ref_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
                
            title = filename
            first_line = content.splitlines()[0] if content.splitlines() else ""
            if first_line.startswith("# "):
                title = first_line.replace("# ", "").strip()
                
            chunks_indexed = await rag.index_document(
                title=title, text=content, source_file=filename
            )
            total_chunks += chunks_indexed
            
        return {"status": "success", "chunks_indexed": total_chunks}
    except Exception as e:
        logger.exception("Error during manual re-indexing:")
        raise HTTPException(status_code=500, detail=str(e))


# ── Simulator Page ───────────────────────────────────────────────────────────
@app.get("/simulator", response_class=HTMLResponse)
async def get_simulator():
    """Serves the interactive WhatsApp Webhook Simulator page."""
    sim_path = os.path.join("static", "simulator.html")
    if not os.path.exists(sim_path):
        raise HTTPException(status_code=404, detail="Simulator page not found.")
    return FileResponse(sim_path)


@app.post("/api/simulator/trigger")
async def api_simulator_trigger(
    background_tasks: BackgroundTasks,
    phone_number: str = Form("919999999999"),
    message_type: str = Form("image"),
    text_body: str = Form(None),
    sample_file: str = Form(None),
    file: UploadFile = File(None),
):
    """
    Simulate an inbound WhatsApp message by building a signed webhook payload
    and posting it to /webhook internally. Supports image upload, sample file
    selection, and text messages.
    """
    import httpx as _httpx
    from datetime import datetime as _dt
    import uuid

    sim_message_id = f"wamid.sim_{uuid.uuid4().hex[:16]}"
    ts = str(int(_dt.now().timestamp()))

    saved_relative_path = None

    if message_type in ("image", "document") and (file or sample_file):
        if file and file.filename:
            # Save uploaded file
            file_bytes = await file.read()
            ext = os.path.splitext(file.filename)[-1] or ".png"
            fname = f"sim_{uuid.uuid4().hex[:8]}{ext}"
            rel_path = f"{phone_number}/{fname}"
            full_path = os.path.join(storage.STORAGE_DIR, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as fh:
                fh.write(file_bytes)
            saved_relative_path = rel_path
            logger.info("Simulator: Saved uploaded file to %s", full_path)
        elif sample_file:
            # Use an existing sample file from the workspace
            workspace_path = sample_file
            if not os.path.isabs(workspace_path):
                workspace_path = os.path.join(os.getcwd(), sample_file)
            if not os.path.exists(workspace_path):
                raise HTTPException(status_code=400, detail=f"Sample file not found: {sample_file}")
            # Copy it into storage so media ID resolves correctly
            fname = f"sim_{uuid.uuid4().hex[:8]}{os.path.splitext(sample_file)[-1] or '.png'}"
            rel_path = f"{phone_number}/{fname}"
            full_path = os.path.join(storage.STORAGE_DIR, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            import shutil
            shutil.copy2(workspace_path, full_path)
            saved_relative_path = rel_path
            logger.info("Simulator: Copied sample file to %s", full_path)

        if not saved_relative_path:
            raise HTTPException(status_code=400, detail="No file provided for image/document simulation.")

        local_media_id = f"local_file:{saved_relative_path}"
        import mimetypes

        mime_type, _ = mimetypes.guess_type(full_path)
        mime_type = mime_type or (
            "application/pdf"
            if (saved_relative_path or "").lower().endswith(".pdf")
            else "image/png"
        )
        doc_filename = os.path.basename(saved_relative_path or "") or "document"
        media_block = {
            "mime_type": mime_type,
            "sha256": "simulated_sha256",
            "id": local_media_id,
        }
        if message_type == "document":
            media_block["filename"] = doc_filename
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "sim_entry_12345",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "contacts": [{"profile": {"name": "Simulator User"}, "wa_id": phone_number}],
                        "messages": [{
                            "from": phone_number,
                            "id": sim_message_id,
                            "timestamp": ts,
                            "type": message_type,
                            message_type: {
                                **media_block,
                            }
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }
    elif message_type == "text" and text_body:
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "sim_entry_12345",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "contacts": [{"profile": {"name": "Simulator User"}, "wa_id": phone_number}],
                        "messages": [{
                            "from": phone_number,
                            "id": sim_message_id,
                            "timestamp": ts,
                            "type": "text",
                            "text": {"body": text_body}
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }
    else:
        raise HTTPException(status_code=400, detail="Invalid simulation parameters.")

    # Compute HMAC signature for the payload
    body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(APP_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

    # Do not clear prior simulated replies — the UI polls incrementally.
    # Use DELETE /api/simulator/messages to reset a conversation.

    # Call /webhook internally using httpx
    try:
        async with _httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                _webhook_internal_url(),
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": f"sha256={sig}"
                }
            )
        return JSONResponse({
            "status": "triggered",
            "message_id": sim_message_id,
            "webhook_status": resp.status_code,
            "message_type": message_type,
            "saved_path": saved_relative_path
        })
    except Exception as e:
        logger.exception("Simulator trigger failed:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/simulator/messages")
async def api_simulator_get_messages(phone_number: str = Query("919999999999"), after: str = Query(None)):
    """Returns all captured simulated outbound messages for a given phone number."""
    msgs = [
        m for m in whatsapp.simulated_outbound_messages
        if m.get("to") == phone_number
    ]
    if after:
        msgs = [m for m in msgs if m.get("timestamp", "") > after]
    return msgs


@app.delete("/api/simulator/messages")
async def api_simulator_clear_messages(phone_number: str = Query("919999999999")):
    """Clears simulated outbound messages for a given phone number."""
    whatsapp.simulated_outbound_messages[:] = [
        m for m in whatsapp.simulated_outbound_messages
        if m.get("to") != phone_number
    ]
    return {"status": "cleared"}


# ── Mount Static Files ────────────────────────────────────────────────────────
# Check if static directory exists, otherwise create it
os.makedirs("static", exist_ok=True)
os.makedirs(storage.STORAGE_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
# NOTE: Raw /storage mount removed for Phase 1 security.
# Use authenticated GET /api/files/{path} (decrypts at rest).

