"""
Common schemas — enums, pagination, shared response types.
"""
from enum import Enum

from pydantic import BaseModel, Field

from app.config import settings


class MessageStatus(str, Enum):
    sent = "sent"
    delivered = "delivered"
    read = "read"


class AttachmentType(str, Enum):
    image = "image"
    video = "video"
    document = "document"
    voice = "voice"


class SystemMessageType(str, Enum):
    member_added = "member_added"
    member_removed = "member_removed"
    member_left = "member_left"
    admin_changed = "admin_changed"
    group_name_changed = "group_name_changed"
    group_avatar_changed = "group_avatar_changed"
    group_created = "group_created"


class ComplianceScope(str, Enum):
    """Compliance policy scope hierarchy."""
    global_ = "global"
    company = "company"
    department = "department"
    user = "user"


class CompliancePermission(str, Enum):
    """Compliance officer permissions."""
    read_messages = "read_messages"
    export_messages = "export_messages"
    search_messages = "search_messages"


class PaginationParams(BaseModel):
    page: int = Field(1, ge=1)
    limit: int = Field(settings.DEFAULT_PAGE_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT)


class DeleteResponse(BaseModel):
    deleted: bool


class DeactivateResponse(BaseModel):
    deactivated: bool
