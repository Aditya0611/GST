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

from fastapi import FastAPI, Request, Response, Query, HTTPException, BackgroundTasks, UploadFile, File, Form, Depends, Security
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
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
from processor import process_invoice, evaluate_itc_eligibility, validate_gstin
import re
import sandbox

# ── Load config ───────────────────────────────────────────────────────────────
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("APP_SECRET", "")
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "")
WHATSAPP_DISPLAY_NUMBER = "".join(
    ch for ch in os.getenv("WHATSAPP_DISPLAY_NUMBER", "") if ch.isdigit()
)


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


async def require_dashboard_auth(api_key: str | None = Security(_api_key_header)) -> None:
    """
    Protect CA dashboard REST APIs with X-API-Key.
    If DASHBOARD_API_KEY is unset, requests are allowed (local/dev only — logged at startup).
    """
    if not DASHBOARD_API_KEY:
        return
    if not api_key or not hmac.compare_digest(api_key, DASHBOARD_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")


# ── Message deduplication (persisted in DB; see db.processed_messages) ────────


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Taxova.ai webhook server starting...")
    logger.info("   Verify token configured: %s", bool(WEBHOOK_VERIFY_TOKEN))
    logger.info("   App secret configured:   %s", bool(APP_SECRET))
    logger.info("   Dashboard API key set:   %s", bool(DASHBOARD_API_KEY))
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
    
    # Initialize the database schema on start
    await db.init_db()
    
    # Check if knowledge base is empty and trigger startup seed
    try:
        chunk_count = rag.collection.count()
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
                    await rag.index_document(title=title, text=content)
                logger.info("Startup seed indexing completed successfully.")
    except Exception as e:
        logger.warning("Startup seed indexing failed: %s", e)
        
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


async def _handle_text_message(msg: dict, sender: str, message_id: str) -> None:
    """Handle a text message via the GST agent (with lightweight command shortcuts)."""
    text_body = msg.get("text", {}).get("body", "").strip()
    text_lower = text_body.lower()
    logger.info("💬 Text from %s: %s", sender, text_body[:100])

    await whatsapp.mark_as_read(message_id)

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
                "Send invoice photos/PDFs, or ask things like:\n"
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
        "version": "0.2.0",
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


@app.get("/api/clients")
async def api_get_clients(_auth: None = Depends(require_dashboard_auth)):
    """Returns list of all Taxova.ai client profiles."""
    try:
        clients = await db.get_clients()
        return clients
    except Exception as e:
        logger.exception("Failed to get clients:")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/clients/{phone}")
async def api_update_client(
    phone: str, request: Request, _auth: None = Depends(require_dashboard_auth)
):
    """Update client GSTIN / name so sales & ITC can be classified."""
    try:
        body = await request.json()
        gstin = (body.get("gstin") or "").strip().upper()
        name = (body.get("name") or "").strip() or None

        if gstin and not validate_gstin(gstin):
            raise HTTPException(
                status_code=400,
                detail="Invalid GSTIN. Must be a valid 15-character Indian GSTIN.",
            )

        clients = await db.get_clients()
        if not any(c.get("phone_number") == phone for c in clients):
            await db.get_or_create_client(phone, name=name or "Client")

        await db.update_client_profile(
            phone,
            gstin,
            registered=bool(gstin),
            name=name,
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
        ca_user = body.get("ca_user", "CA Operator")

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
    _auth: None = Depends(require_dashboard_auth),
):
    """Fetch invoices based on status, client_phone, or month filters."""
    try:
        invoices = await db.get_invoices(client_phone, status, month)
        return await _enrich_invoices_with_portal_cache(invoices)
    except Exception as e:
        logger.exception("Failed to get invoices:")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/invoices/{invoice_id}")
async def api_get_invoice_detail(invoice_id: int, _auth: None = Depends(require_dashboard_auth)):
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
async def api_update_invoice(
    invoice_id: int, request: Request, _auth: None = Depends(require_dashboard_auth)
):
    """Updates invoice metadata fields and records CA modifications."""
    try:
        body = await request.json()
        ca_user = body.get("ca_user", "CA Operator")
        fields_to_update = body.get("fields", {})

        # Get existing invoice to check for category change
        existing_invoice = await db.get_invoice_detail(invoice_id)
        if not existing_invoice:
            raise HTTPException(status_code=404, detail="Invoice not found.")

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
        success = await db.update_invoice(invoice_id, fields_to_update, ca_user)
        if not success:
            raise HTTPException(status_code=404, detail="Invoice not found.")

        if cat_changed or rec_changed:
            await db.apply_line_itc_evaluation(invoice_id, ca_user=ca_user)

        updated_detail = await db.get_invoice_detail(invoice_id)
        return {"status": "success", "invoice": updated_detail}
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
        ca_user = body.get("ca_user", "CA Operator")

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
    invoice_id: int, request: Request, _auth: None = Depends(require_dashboard_auth)
):
    """Marks invoice as verified and approved (HITL final gate for GSTR)."""
    try:
        body = await request.json()
        ca_user = body.get("ca_user", "CA Operator")

        success = await db.approve_invoice(invoice_id, ca_user)
        if not success:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return {"status": "success", "review_status": "approved"}
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
        ca_user = body.get("ca_user", "CA Operator")
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
        ca_user = body.get("ca_user", "CA Operator")
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
        ca_user = body.get("ca_user", "CA Operator")
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
            detail="Sandbox not configured. Set SANDBOX_API_KEY and SANDBOX_API_SECRET in .env.",
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
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected Sandbox GSTIN lookup error:")
        raise HTTPException(status_code=500, detail=str(e))


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
            detail="Sandbox not configured. Set SANDBOX_API_KEY and SANDBOX_API_SECRET in .env.",
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
                # Cached hard failure — still treat as not found-ish for queue
                return {
                    "gstin": g,
                    "found": False,
                    "message": row.get("error_message"),
                    "legal_name": None,
                    "status": None,
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
            ca_user = str(form.get("ca_user") or "CA Operator")
        else:
            body = await request.json()
            if isinstance(body, dict) and body.get("return_period") and not period:
                period = body.get("return_period")
            period_parsed, entries = gstr2b_mod.parse_gstr2b_json(body)
            period = period or period_parsed
            ca_user = (body.get("ca_user") if isinstance(body, dict) else None) or "CA Operator"

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

