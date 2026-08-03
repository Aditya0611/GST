"""
whatsapp.py — WhatsApp Cloud API client.

Handles two things:
  1. Downloading media files (images, PDFs) that clients send you
  2. Sending reply messages back to clients

All calls go to Meta's Graph API v21.0.
Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

# In-memory queue to capture outgoing replies for the browser simulator
simulated_outbound_messages = []

# Reusable async HTTP client — connection pooling, timeouts
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0, connect=10.0),
)


def _refresh_credentials() -> tuple[str, str]:
    """Always re-read token/phone id from env so .env updates apply after restart."""
    load_dotenv(override=True)
    token = os.getenv("WHATSAPP_TOKEN", "").strip()
    phone_id = os.getenv("WHATSAPP_PHONE_ID", "").strip()
    if token:
        _client.headers["Authorization"] = f"Bearer {token}"
    else:
        _client.headers.pop("Authorization", None)
    return token, phone_id


# Back-compat for code that reads module-level names
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
_refresh_credentials()


# ── Download Media ────────────────────────────────────────────────────────────
async def download_media(media_id: str) -> tuple[bytes, str]:
    """
    Download a media file from WhatsApp in two steps:
      1. GET /{media_id} → get the temporary download URL
      2. GET the download URL → get the actual file bytes

    WhatsApp media URLs expire in ~5 minutes, so we download immediately.

    Args:
        media_id: The media ID from the webhook payload

    Returns:
        Tuple of (file_bytes, mime_type)

    Raises:
        httpx.HTTPStatusError: If either API call fails
    """
    # Local file mock handling for simulator
    if media_id.startswith("local_file:"):
        local_path = media_id.replace("local_file:", "")
        # Resolve full path
        import storage
        full_path = os.path.join(storage.STORAGE_DIR, local_path)
        if not os.path.exists(full_path):
            full_path = local_path  # fallback to absolute or relative directly
        
        logger.info("Simulator: Reading media file directly from local storage: %s", full_path)
        try:
            with open(full_path, "rb") as f:
                file_bytes = f.read()
            import mimetypes
            mime_type, _ = mimetypes.guess_type(full_path)
            return file_bytes, mime_type or "image/png"
        except Exception as e:
            logger.error("Simulator failed to read local file: %s", e)
            raise

    _refresh_credentials()

    # Step 1: Get the temporary media URL
    logger.info("Fetching media URL for media_id=%s", media_id)
    meta_response = await _client.get(f"{GRAPH_API_BASE}/{media_id}")
    meta_response.raise_for_status()
    media_info = meta_response.json()

    media_url = media_info["url"]
    mime_type = media_info.get("mime_type", "application/octet-stream")

    # Step 2: Download the actual file bytes
    logger.info("Downloading media from URL (mime=%s)", mime_type)
    file_response = await _client.get(media_url)
    file_response.raise_for_status()

    file_bytes = file_response.content
    logger.info("Downloaded %d bytes (mime=%s)", len(file_bytes), mime_type)

    return file_bytes, mime_type


# ── Send Messages ─────────────────────────────────────────────────────────────
async def send_text_message(to: str, body: str) -> dict:
    """
    Send a plain text message to a WhatsApp number.

    Args:
        to: Recipient phone number in international format (e.g. "919876543210")
        body: Message text

    Returns:
        Meta API response as dict
    """
    token, phone_id = _refresh_credentials()
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }

    # Save to simulator log in all cases so simulator receives messages
    from datetime import datetime
    simulated_outbound_messages.append({
        "to": to,
        "body": body,
        "timestamp": datetime.now().isoformat(),
        "type": "text",
        "simulated": True
    })

    if not token or not phone_id:
        logger.error("WhatsApp send aborted: WHATSAPP_TOKEN or WHATSAPP_PHONE_ID missing")
        return {"status": "simulated", "messages": [{"id": f"sim_{int(datetime.now().timestamp())}"}]}

    try:
        response = await _client.post(
            f"{GRAPH_API_BASE}/{phone_id}/messages",
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        logger.info("Sent text message to %s (msg_id=%s)", to, result.get("messages", [{}])[0].get("id"))
        return result
    except Exception as e:
        detail = ""
        if hasattr(e, "response") and e.response is not None:
            detail = f" body={e.response.text[:300]}"
        logger.warning("WhatsApp API send skipped or failed: %s%s (Simulated message preserved)", e, detail)
        return {"status": "simulated", "messages": [{"id": f"sim_{int(datetime.now().timestamp())}"}]}


async def send_reply(to: str, message_id: str, body: str) -> dict:
    """
    Send a reply to a specific message (shows as a quoted reply in WhatsApp).

    Args:
        to: Recipient phone number
        message_id: The wamid of the message being replied to
        body: Reply text

    Returns:
        Meta API response as dict
    """
    token, phone_id = _refresh_credentials()
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
        "context": {"message_id": message_id},
    }

    # Save to simulator log in all cases so simulator receives replies
    from datetime import datetime
    simulated_outbound_messages.append({
        "to": to,
        "body": body,
        "timestamp": datetime.now().isoformat(),
        "type": "reply",
        "reply_to": message_id,
        "simulated": True
    })

    if not token or not phone_id:
        logger.error("WhatsApp reply aborted: WHATSAPP_TOKEN or WHATSAPP_PHONE_ID missing")
        return {"status": "simulated", "messages": [{"id": f"sim_reply_{int(datetime.now().timestamp())}"}]}

    try:
        response = await _client.post(
            f"{GRAPH_API_BASE}/{phone_id}/messages",
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        logger.info("Sent reply to %s (replying to %s)", to, message_id)
        return result
    except Exception as e:
        detail = ""
        if hasattr(e, "response") and e.response is not None:
            detail = f" body={e.response.text[:300]}"
        logger.warning("WhatsApp API send reply skipped or failed: %s%s (Simulated reply preserved)", e, detail)
        return {"status": "simulated", "messages": [{"id": f"sim_reply_{int(datetime.now().timestamp())}"}]}


# ── Mark as Read ──────────────────────────────────────────────────────────────
async def mark_as_read(message_id: str) -> None:
    """
    Mark a message as read (shows blue ticks to the sender).
    Good UX — the client sees you've received their invoice.
    """
    _token, phone_id = _refresh_credentials()
    if not phone_id:
        return

    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }

    try:
        response = await _client.post(
            f"{GRAPH_API_BASE}/{phone_id}/messages",
            json=payload,
        )
        response.raise_for_status()
        logger.debug("Marked message %s as read", message_id)
    except httpx.HTTPStatusError as e:
        # Non-critical — log but don't crash
        logger.warning("Failed to mark message %s as read: %s", message_id, e)
