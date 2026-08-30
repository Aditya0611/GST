"""
storage.py — File storage abstraction.

Saves downloaded WhatsApp media to local disk, organized by sender phone number.
Designed to be swapped out for S3 in production without changing the calling code.

Directory structure on disk:
    storage/
    └── 919876543210/          ← client's phone number (without '+')
        ├── 2026-06/           ← year-month folder
        │   ├── invoice_1717372800_a1b2c3.pdf
        │   └── photo_1717372801_d4e5f6.jpg
        └── ...
"""

import os
import time
import logging
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from dotenv import load_dotenv

import security

load_dotenv()

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "./storage"))
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_dir(path: Path) -> None:
    """Create directory tree if it doesn't exist."""
    path.mkdir(parents=True, exist_ok=True)


def _build_filename(original_name: str | None, media_type: str, extension: str) -> str:
    """
    Build a unique, descriptive filename.

    Examples:
        photo_1717372800_a1b2c3.jpg
        document_1717372801_d4e5f6.pdf
    """
    timestamp = int(time.time())
    # Use last 6 chars of the unix timestamp in hex for uniqueness within the same second
    unique = format(hash(f"{timestamp}{original_name}") & 0xFFFFFF, "06x")

    prefix_map = {
        "image": "photo",
        "document": "document",
        "audio": "audio",
        "video": "video",
    }
    prefix = prefix_map.get(media_type, "file")

    return f"{prefix}_{timestamp}_{unique}.{extension}"


async def save_file(
    phone_number: str,
    file_bytes: bytes,
    media_type: str,
    mime_type: str,
    original_filename: str | None = None,
) -> str:
    """
    Save a downloaded file to local disk.

    Args:
        phone_number: Client's WhatsApp number (e.g. "919876543210")
        file_bytes: Raw bytes of the downloaded file
        media_type: WhatsApp media type — "image", "document", "audio", "video"
        mime_type: MIME type like "image/jpeg", "application/pdf"
        original_filename: Original filename if provided by WhatsApp (documents only)

    Returns:
        Relative path to the saved file (e.g. "919876543210/2026-06/document_1717372800_a1b2c3.pdf")
    """
    # Determine file extension from MIME type
    extension_map = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.ms-excel": "xls",
        "text/csv": "csv",
        "audio/ogg": "ogg",
        "audio/mpeg": "mp3",
        "video/mp4": "mp4",
    }
    extension = extension_map.get(mime_type, "bin")

    # Build the path: storage/<phone>/<year-month>/<filename>
    year_month = time.strftime("%Y-%m")
    client_dir = STORAGE_DIR / phone_number / year_month
    _ensure_dir(client_dir)

    filename = _build_filename(original_filename, media_type, extension)
    file_path = client_dir / filename

    # Encrypt at rest when ENCRYPTION_KEY is configured
    to_write = security.encrypt_bytes(file_bytes)
    file_path.write_bytes(to_write)
    relative_path = f"{phone_number}/{year_month}/{filename}"

    file_size_kb = len(file_bytes) / 1024
    logger.info(
        "Saved file: %s (%.1f KB, type=%s, encrypted=%s)",
        relative_path,
        file_size_kb,
        mime_type,
        security.encryption_enabled(),
    )

    return relative_path


def resolve_path(relative_or_absolute: str) -> Path:
    """Resolve a storage-relative or absolute path under STORAGE_DIR when possible."""
    raw = (relative_or_absolute or "").strip().replace("\\", "/")
    p = Path(relative_or_absolute)
    if p.is_absolute():
        return p
    # Avoid storage/storage/... when callers pass "./storage/..." or already-resolved paths
    storage_name = STORAGE_DIR.resolve().name  # usually "storage"
    parts = Path(raw).parts
    if parts and parts[0] in (storage_name, ".", ".."):
        # "./storage/phone/..." or "storage/phone/..."
        cleaned = Path(*[x for x in parts if x not in (".",)])
        try:
            cleaned_resolved = cleaned if cleaned.is_absolute() else (Path.cwd() / cleaned)
            storage_resolved = STORAGE_DIR.resolve()
            if str(cleaned_resolved.resolve()).startswith(str(storage_resolved)):
                return cleaned_resolved.resolve()
        except OSError:
            pass
        if parts[0] == storage_name:
            return STORAGE_DIR / Path(*parts[1:])
    return STORAGE_DIR / relative_or_absolute


def read_file_bytes(relative_or_absolute: str) -> bytes:
    """Read file bytes and decrypt if encrypted at rest."""
    path = resolve_path(relative_or_absolute)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return security.decrypt_bytes(path.read_bytes())


@contextmanager
def plaintext_temp_file(relative_or_absolute: str):
    """
    Yield a temporary plaintext file path for processors that need a filesystem path
    (Pillow, Gemini upload). Deletes the temp file on exit.
    """
    path = resolve_path(relative_or_absolute)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    data = security.decrypt_bytes(path.read_bytes())
    suffix = path.suffix or ".bin"
    fd, tmp_name = tempfile.mkstemp(suffix=suffix, prefix="taxova_")
    os.close(fd)
    try:
        Path(tmp_name).write_bytes(data)
        yield tmp_name
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


async def save_json(
    phone_number: str,
    invoice_path_str: str,
    data: dict,
) -> str:
    """
    Save invoice extraction metadata (JSON) next to the saved invoice file.
    Encrypted at rest when ENCRYPTION_KEY is set.
    """
    invoice_path = STORAGE_DIR / invoice_path_str
    json_path = invoice_path.with_name(f"{invoice_path.stem}_extracted.json")

    raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    json_path.write_bytes(security.encrypt_bytes(raw))

    relative_json_path = f"{phone_number}/{invoice_path.parent.name}/{json_path.name}"
    logger.info(
        "Saved extraction metadata: %s (encrypted=%s)",
        relative_json_path,
        security.encryption_enabled(),
    )
    return relative_json_path

