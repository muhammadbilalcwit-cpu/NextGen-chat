"""
Upload routes — file attachments (local storage or S3 URL passthrough).
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from app.auth.jwt import CurrentUser, get_current_user
from app.config import settings
from app.schemas.group import GroupAvatarResponse
from app.schemas.message import AttachmentUploadResponse, VoiceNoteUploadResponse
from app.services.storage_service import (
    save_local_file,
    validate_s3_url,
    detect_attachment_type,
    validate_size,
)

router = APIRouter(prefix="/chat", tags=["upload"])


@router.post("/attachments", response_model=AttachmentUploadResponse)
async def upload_attachment(
    file: UploadFile = File(None),
    url: str = Form(None),
    type: str = Form(None),
    filename: str = Form(None),
    originalFilename: str = Form(None),
    size: int = Form(None),
    mimeType: str = Form(None),
    thumbnailUrl: str = Form(None),
    duration: float = Form(None),
    width: int = Form(None),
    height: int = Form(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Upload a file attachment.
    STORAGE_MODE=0: Frontend sends S3 URL + metadata
    STORAGE_MODE=1: Frontend uploads file, saved locally
    """
    if settings.STORAGE_MODE == 0:
        # S3 mode — frontend already uploaded, just validate URL
        if not url:
            raise HTTPException(status_code=400, detail="URL required in S3 mode")
        validate_s3_url(url)
        validate_size(size or 0, type or "document")

        return {
            "type": type or "document",
            "url": url,
            "thumbnailUrl": thumbnailUrl,
            "filename": filename or "file",
            "originalFilename": originalFilename or filename or "file",
            "size": size or 0,
            "mimeType": mimeType or "application/octet-stream",
            "duration": duration,
            "width": width,
            "height": height,
        }
    else:
        # Local storage mode — save file to disk
        if not file:
            raise HTTPException(status_code=400, detail="File required in local storage mode")

        result = await save_local_file(file)

        # Add optional metadata
        if thumbnailUrl:
            result["thumbnailUrl"] = thumbnailUrl
        if duration is not None:
            result["duration"] = duration
        if width is not None:
            result["width"] = width
        if height is not None:
            result["height"] = height

        return result


@router.post("/attachments/voice", response_model=VoiceNoteUploadResponse)
async def upload_voice(
    file: UploadFile = File(...),
    duration: float = Form(None),
    waveform: str = Form(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Upload a voice note."""
    if settings.STORAGE_MODE == 1:
        result = await save_local_file(file)
        result["type"] = "voice"
        if duration is not None:
            result["duration"] = duration
        if waveform:
            import json
            try:
                result["waveform"] = json.loads(waveform)
            except (json.JSONDecodeError, TypeError):
                pass
        return result
    else:
        raise HTTPException(
            status_code=400,
            detail="Voice upload requires local storage mode. Use S3 URL for STORAGE_MODE=0.",
        )


@router.post("/groups/{group_id}/avatar", response_model=GroupAvatarResponse)
async def upload_group_avatar(
    group_id: str,
    file: UploadFile = File(None),
    url: str = Form(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Upload or set group avatar."""
    from app.services import chat_service
    from app.websocket.manager import emit_to_users

    group = await chat_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["groupAdmin"] != current_user.id:
        raise HTTPException(status_code=403, detail="Only admin can change avatar")

    if settings.STORAGE_MODE == 0:
        if not url:
            raise HTTPException(status_code=400, detail="URL required")
        avatar_url = url
    else:
        if not file:
            raise HTTPException(status_code=400, detail="File required")
        result = await save_local_file(file)
        avatar_url = result["url"]

    updated = await chat_service.update_group(group_id, {"avatar": avatar_url})

    await emit_to_users(
        updated["participants"],
        "chat:group_updated",
        {"group": updated},
        exclude_user=current_user.id,
    )

    return {"avatarUrl": avatar_url, "group": updated}
