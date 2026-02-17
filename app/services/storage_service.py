"""
Storage service — handles file uploads.

STORAGE_MODE=0: S3 URL provided by frontend (just validate & pass through)
STORAGE_MODE=1: Save file to local server folder
"""
import os
import uuid
import aiofiles
from datetime import datetime
from fastapi import UploadFile, HTTPException

from app.config import settings

def detect_attachment_type(mime_type: str) -> str:
    """Detect attachment type from MIME type (reads allowed types from config)."""
    # Strip codec parameters (e.g. "audio/webm;codecs=opus" → "audio/webm")
    base_mime = mime_type.split(";")[0].strip()
    for atype, mimes in settings.allowed_mime_types.items():
        if base_mime in mimes:
            return atype
    raise HTTPException(status_code=400, detail=f"Unsupported file type: {mime_type}")


def validate_size(size: int, attachment_type: str) -> None:
    """Validate file size against env limits."""
    limit = settings.attachment_limits.get(attachment_type)
    if limit and size > limit:
        limit_mb = limit / (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max {attachment_type} size: {limit_mb:.0f}MB",
        )


def validate_s3_url(url: str) -> dict:
    """
    STORAGE_MODE=0: Frontend uploaded to S3, just validate the URL shape.
    Returns attachment metadata from the URL.
    """
    if not url or not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="Invalid S3 URL")
    return {"url": url}


async def save_local_file(file: UploadFile) -> dict:
    """
    STORAGE_MODE=1: Save uploaded file to local server folder.
    Returns attachment metadata.
    """
    content = await file.read()
    size = len(content)
    mime_type = file.content_type or "application/octet-stream"
    attachment_type = detect_attachment_type(mime_type)
    validate_size(size, attachment_type)

    # Build path: uploads/{type}/{date}/{uuid}_{filename}
    date_folder = datetime.utcnow().strftime("%Y-%m-%d")
    folder = os.path.join(settings.UPLOAD_DIR, attachment_type, date_folder)
    os.makedirs(folder, exist_ok=True)

    ext = os.path.splitext(file.filename or "file")[1]
    safe_name = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(folder, safe_name)

    async with aiofiles.open(file_path, "wb") as f:
        await f.write(content)

    # Absolute URL so frontend can use directly (Avatar, MessageAttachment, etc.)
    url_path = f"{settings.PUBLIC_URL}/uploads/{attachment_type}/{date_folder}/{safe_name}"

    return {
        "type": attachment_type,
        "url": url_path,
        "filename": safe_name,
        "originalFilename": file.filename or "file",
        "size": size,
        "mimeType": mime_type,
    }
