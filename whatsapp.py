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

load_dotenv()

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

# Reusable async HTTP client — connection pooling, timeouts
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0, connect=10.0),
    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
)


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
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }

    response = await _client.post(
        f"{GRAPH_API_BASE}/{WHATSAPP_PHONE_ID}/messages",
        json=payload,
    )
    response.raise_for_status()
    result = response.json()

    logger.info("Sent text message to %s (msg_id=%s)", to, result.get("messages", [{}])[0].get("id"))
    return result


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
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
        "context": {"message_id": message_id},
    }

    response = await _client.post(
        f"{GRAPH_API_BASE}/{WHATSAPP_PHONE_ID}/messages",
        json=payload,
    )
    response.raise_for_status()
    result = response.json()

    logger.info("Sent reply to %s (replying to %s)", to, message_id)
    return result


# ── Mark as Read ──────────────────────────────────────────────────────────────
async def mark_as_read(message_id: str) -> None:
    """
    Mark a message as read (shows blue ticks to the sender).
    Good UX — the client sees you've received their invoice.
    """
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }

    try:
        response = await _client.post(
            f"{GRAPH_API_BASE}/{WHATSAPP_PHONE_ID}/messages",
            json=payload,
        )
        response.raise_for_status()
        logger.debug("Marked message %s as read", message_id)
    except httpx.HTTPStatusError as e:
        # Non-critical — log but don't crash
        logger.warning("Failed to mark message %s as read: %s", message_id, e)
