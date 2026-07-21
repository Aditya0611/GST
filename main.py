"""
main.py — GST Autopilot WhatsApp Webhook & CA Dashboard Server

Endpoints:
  GET  /webhook  → Meta verification
  POST /webhook  → WhatsApp message ingestion & client mapping
  GET  /dashboard → CA Admin review console
  GET  /api/*    → REST APIs for CA dashboard operations
"""

import os
import hmac
import hashlib
import json
import logging
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Query, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

import whatsapp
import storage
import db
import gstr
import rag
from processor import process_invoice, evaluate_itc_eligibility, validate_gstin

# ── Load config ───────────────────────────────────────────────────────────────
load_dotenv()

WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("APP_SECRET", "")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)-12s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webhook")

# ── Message deduplication ─────────────────────────────────────────────────────
# In-memory set of processed message IDs to handle WhatsApp's "at-least-once" delivery.
_processed_messages: set[str] = set()
MAX_DEDUP_SIZE = 10_000  # Prevent unbounded memory growth


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 GST Autopilot webhook server starting...")
    logger.info("   Verify token configured: %s", bool(WEBHOOK_VERIFY_TOKEN))
    logger.info("   App secret configured:   %s", bool(APP_SECRET))
    
    # Initialize the database schema on start
    await db.init_db()
    
    # Check if knowledge base is empty and trigger startup seed
    try:
        chunks = await db.get_all_kb_chunks()
        if not chunks:
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
                    await rag.index_document(title=title, text=content)
                logger.info("Startup seed indexing completed successfully.")
    except Exception as e:
        logger.warning("Startup seed indexing failed: %s", e)
        
    yield
    logger.info("👋 Webhook server shutting down")


app = FastAPI(
    title="GST Autopilot — WhatsApp Webhook & CA Dashboard",
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
        logger.warning("APP_SECRET not set — skipping signature verification (NOT safe for production!)")
        return True

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

        # Deduplication check
        if message_id in _processed_messages:
            logger.info("⏭️  Skipping duplicate message %s from %s", message_id, sender)
            continue

        # Track this message (with bounded set size)
        if len(_processed_messages) >= MAX_DEDUP_SIZE:
            _processed_messages.clear()
            logger.info("Cleared dedup cache (reached %d entries)", MAX_DEDUP_SIZE)
        _processed_messages.add(message_id)

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
                await _handle_text_message(msg, sender, message_id)
            elif msg_type == "audio":
                await _handle_audio_message(msg, sender, message_id)
            else:
                logger.info("Unhandled message type: %s", msg_type)
        except Exception:
            logger.exception("Error processing message %s from %s", message_id, sender)

    return {"status": "ok"}


# ── Message Handlers ──────────────────────────────────────────────────────────
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

    # Send immediate acknowledgment reply
    await whatsapp.send_reply(
        to=sender,
        message_id=message_id,
        body=(
            f"✅ Got your {file_label}! "
            f"We are analyzing the invoice details right now.\n\n"
            f"We'll send you a summary of the extracted data in a moment. ⏳"
        ),
    )

    # Schedule background processing task
    background_tasks.add_task(
        _background_process_invoice,
        sender=sender,
        message_id=message_id,
        saved_path=saved_path,
        file_label=file_label,
    )


async def _background_process_invoice(
    sender: str, message_id: str, saved_path: str, file_label: str
) -> None:
    """Run AI extraction, save to DB, and send a summary reply back on WhatsApp."""
    try:
        # Build the full absolute path of the saved file
        full_path = os.path.join(storage.STORAGE_DIR, saved_path)
        logger.info("Background task: Processing invoice at %s", full_path)

        # Run AI invoice processing
        result = await process_invoice(full_path)

        # Save extracted JSON next to the invoice file (backup)
        await storage.save_json(
            phone_number=sender,
            invoice_path_str=saved_path,
            data=result.model_dump(),
        )

        # Save extracted invoice details to SQLite/Postgres DB
        invoice_id = await db.save_invoice(
            client_phone=sender,
            file_path=saved_path,
            result=result.model_dump()
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
                "Our team has been notified and will review it manually."
            ),
        )


async def _handle_text_message(msg: dict, sender: str, message_id: str) -> None:
    """Handle a text message — acknowledge and guide."""
    text_body = msg.get("text", {}).get("body", "").strip().lower()
    logger.info("💬 Text from %s: %s", sender, text_body[:100])

    await whatsapp.mark_as_read(message_id)

    # Parse bot commands
    if "summary" in text_body:
        year_month = datetime.now().strftime("%Y-%m")
        parts = text_body.split()
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
            f"🤖 _Powered by GST Autopilot_"
        )
        await whatsapp.send_reply(to=sender, message_id=message_id, body=summary_msg)

    elif "status" in text_body or "filing" in text_body:
        year_month = datetime.now().strftime("%Y-%m")
        metrics = await db.get_monthly_metrics(sender, year_month)
        
        status_msg = (
            f"📅 *GST Filing Status ({year_month})*\n\n"
            f"• *GSTR-1:* In Preparation (Pending CA approval of {metrics['pending_review']} invoices)\n"
            f"• *GSTR-3B:* Open (Due next month)\n\n"
            f"Please send all remaining invoice photos or PDFs directly in this chat! 📄"
        )
        await whatsapp.send_reply(to=sender, message_id=message_id, body=status_msg)

    else:
        # Search the knowledge base for matching chunks
        try:
            matches = await rag.search_knowledge_base(text_body, limit=1)
            if matches and matches[0]["similarity"] > 0.4:
                # Highly relevant query; generate and return RAG answer
                answer = await rag.answer_query(text_body)
                reply_body = f"{answer}\n\n🤖 _GST AI Compliance Assistant_"
                await whatsapp.send_reply(to=sender, message_id=message_id, body=reply_body)
                return
        except Exception as e:
            logger.error("RAG search failed in WhatsApp webhook: %s", e)

        # Fallback welcome / guide response
        await whatsapp.send_reply(
            to=sender,
            message_id=message_id,
            body=(
                "👋 Hi! I am your *GST Autopilot* bot.\n\n"
                "To file your GST, just send us:\n"
                "📄 *Invoices* (photos or PDFs)\n"
                "🏦 *Bank statements*\n\n"
                "Or use these commands:\n"
                "📊 *'summary'* - Get monthly totals & tax estimates\n"
                "📅 *'status'* - View current filing timeline status\n\n"
                "💡 You can also ask me any questions about GST rules (e.g., 'Can I claim ITC on outdoor catering?')"
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
        "version": "0.2.0",
    }


# ── Dashboard API Endpoints ───────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def get_landing():
    """Serves the GST Autopilot landing page."""
    landing_path = os.path.join("static", "index.html")
    if not os.path.exists(landing_path):
        return HTMLResponse("<h1>Landing page not found.</h1>", status_code=404)
    return FileResponse(landing_path)


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


@app.post("/api/ca/link")
async def api_link_ca(request: Request):
    """Validates a CA invite code and links the CA to the client session."""
    try:
        body = await request.json()
        invite_code = str(body.get("invite_code", "")).strip()

        if not invite_code or len(invite_code) != 6 or not invite_code.isdigit():
            raise HTTPException(status_code=400, detail="Invite code must be exactly 6 digits.")

        # TODO: Replace with real CA lookup in your DB.
        # For now, any 6-digit code starting with a non-zero digit is accepted
        # so you can test the flow end-to-end.
        if invite_code[0] == "0":
            raise HTTPException(status_code=404, detail="Invite code not found. Please verify the code with your CA.")

        logger.info("CA linked with invite code: %s", invite_code)
        return {"status": "success", "invite_code": invite_code}
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

        if len(gstin) != 15:
            raise HTTPException(status_code=400, detail="GSTIN must be exactly 15 alphanumeric characters.")

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


@app.get("/api/clients")
async def api_get_clients():
    """Returns list of all GST Autopilot client profiles."""
    try:
        clients = await db.get_clients()
        return clients
    except Exception as e:
        logger.exception("Failed to get clients:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/invoices")
async def api_get_invoices(
    client_phone: str = Query(None),
    status: str = Query(None),
    month: str = Query(None)
):
    """Fetch invoices based on status, client_phone, or month filters."""
    try:
        invoices = await db.get_invoices(client_phone, status, month)
        return invoices
    except Exception as e:
        logger.exception("Failed to get invoices:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/invoices/{invoice_id}")
async def api_get_invoice_detail(invoice_id: int):
    """Fetch full details, line items, and audit trail of a specific invoice."""
    try:
        detail = await db.get_invoice_detail(invoice_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return detail
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get invoice detail:")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/invoices/{invoice_id}")
async def api_update_invoice(invoice_id: int, request: Request):
    """Updates invoice metadata fields and records CA modifications."""
    try:
        body = await request.json()
        ca_user = body.get("ca_user", "CA Operator")
        fields_to_update = body.get("fields", {})

        # Get existing invoice to check for category change
        existing_invoice = await db.get_invoice_detail(invoice_id)
        if not existing_invoice:
            raise HTTPException(status_code=404, detail="Invoice not found.")

        # ── Auto re-evaluate ITC when category changes ──────────────────────────
        if "business_category" in fields_to_update:
            new_category = fields_to_update["business_category"]
            old_category = existing_invoice.get("business_category")
            
            if new_category != old_category:
                new_rec_gstin = fields_to_update.get("recipient_gstin") or existing_invoice.get("recipient_gstin", "")
                is_recipient_registered = validate_gstin(new_rec_gstin)
                is_eligible, reason = await evaluate_itc_eligibility(new_category, is_recipient_registered)
                fields_to_update["is_itc_eligible"] = is_eligible
                fields_to_update["itc_ineligibility_reason"] = reason or ""

        success = await db.update_invoice(invoice_id, fields_to_update, ca_user)
        if not success:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        
        updated_detail = await db.get_invoice_detail(invoice_id)
        return {"status": "success", "invoice": updated_detail}
    except Exception as e:
        logger.exception("Failed to update invoice:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/invoices/{invoice_id}/approve")
async def api_approve_invoice(invoice_id: int, request: Request):
    """Marks invoice as verified and approved."""
    try:
        body = await request.json()
        ca_user = body.get("ca_user", "CA Operator")

        success = await db.approve_invoice(invoice_id, ca_user)
        if not success:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return {"status": "success"}
    except Exception as e:
        logger.exception("Failed to approve invoice:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstr/export")
async def api_export_gstr(
    client_phone: str = Query(...),
    month: str = Query(...),
    type: str = Query(...)  # 'GSTR1' or 'GSTR3B'
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


@app.get("/api/metrics")
async def api_get_metrics(client_phone: str = Query(...), month: str = Query(...)):
    """Fetch monthly KPI aggregates for client switcher."""
    try:
        metrics = await db.get_monthly_metrics(client_phone, month)
        return metrics
    except Exception as e:
        logger.exception("Failed to fetch dashboard metrics:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/itc/summary")
async def api_get_itc_summary(client_phone: str = Query(...), months: int = Query(12)):
    """Returns month-by-month ITC trend for the last N months — feeds the sparkline chart."""
    try:
        trend = await db.get_itc_monthly_trend(client_phone, num_months=min(months, 24))
        return trend
    except Exception as e:
        logger.exception("Failed to fetch ITC trend summary:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def api_chat_assistant(request: Request):
    """
    RAG Chat assistant endpoint. Answers compliance and general GST queries.
    """
    try:
        body = await request.json()
        message = body.get("message", "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="Message content cannot be empty.")
            
        answer = await rag.answer_query(message)
        return {"response": answer}
    except Exception as e:
        logger.exception("Error in chat assistant:")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rag/reindex")
async def api_reindex_rag():
    """
    Forces clearing and re-indexing of the RAG reference documents.
    """
    try:
        # Clear existing knowledge base
        await db.clear_knowledge_base()
        
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
                
            chunks_indexed = await rag.index_document(title=title, text=content)
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
                                "mime_type": "image/png",
                                "sha256": "simulated_sha256",
                                "id": local_media_id
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

    # Clear old messages for this number so poll returns fresh results
    whatsapp.simulated_outbound_messages[:] = [
        m for m in whatsapp.simulated_outbound_messages
        if m.get("to") != phone_number
    ]

    # Call /webhook internally using httpx
    try:
        async with _httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "http://127.0.0.1:8000/webhook",
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
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/storage", StaticFiles(directory=storage.STORAGE_DIR), name="storage")

