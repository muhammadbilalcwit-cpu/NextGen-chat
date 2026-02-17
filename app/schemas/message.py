"""
Message schemas — response models for messages, attachments, delivery info.
"""
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .user import MessageInfoUser, MessageSender, PendingUser


# ─── Sub-document Schemas ─────────────────────────────────────────────────────

class Attachment(BaseModel):
    type: str
    url: str
    filename: Optional[str] = None
    originalFilename: Optional[str] = None
    size: Optional[int] = None
    mimeType: Optional[str] = None
    thumbnailUrl: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    waveform: Optional[List[float]] = None


class Mention(BaseModel):
    userId: int
    offset: Optional[int] = None
    length: Optional[int] = None


class DeliveryInfo(BaseModel):
    userId: int
    timestamp: Optional[str] = None


# ─── Response Schemas (validated before returning to client) ──────────────────

class MessageResponse(BaseModel):
    """Validated response for messages returned from API / Socket.IO."""
    id: str = Field(..., alias="_id")
    conversationId: str
    senderId: int
    recipientId: Optional[int] = None
    content: str
    status: str = "sent"
    deliveredAt: Optional[str] = None
    readAt: Optional[str] = None
    isDeleted: bool = False
    deletedFor: List[int] = []
    deletedAt: Optional[str] = None
    isGroupMessage: bool = False
    deliveredTo: List[DeliveryInfo] = []
    readBy: List[DeliveryInfo] = []
    isSystemMessage: bool = False
    systemMessageType: Optional[str] = None
    targetUserId: Optional[int] = None
    actorUserId: Optional[int] = None
    actorName: Optional[str] = None
    targetName: Optional[str] = None
    attachment: Optional[Attachment] = None
    mentions: List[Mention] = []
    mentionsAll: bool = False
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None

    model_config = {"populate_by_name": True}


class ConversationInfo(BaseModel):
    groupName: str
    groupAvatar: Optional[str] = None
    isGroup: bool = True


class PaginatedMessages(BaseModel):
    messages: List[MessageResponse]
    total: int
    hasMore: bool
    messageSenders: Optional[Dict[str, MessageSender]] = None
    conversationInfo: Optional[Dict[str, ConversationInfo]] = None


class MarkReadResponse(BaseModel):
    markedCount: int


class MessageDeleteResponse(BaseModel):
    deleted: bool
    forEveryone: Optional[bool] = None


class MessageInfoResponse(BaseModel):
    deliveredTo: List[MessageInfoUser]
    readBy: List[MessageInfoUser]
    pending: List[PendingUser]


class UnreadCountResponse(BaseModel):
    count: int
    direct: int
    groups: int


class AttachmentUploadResponse(BaseModel):
    type: str
    url: str
    filename: str
    originalFilename: str
    size: int
    mimeType: str
    thumbnailUrl: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    waveform: Optional[List[float]] = None


class VoiceNoteUploadResponse(BaseModel):
    type: str = "voice"
    url: str
    filename: str
    originalFilename: str
    size: int
    mimeType: str
    duration: Optional[float] = None
    waveform: Optional[List[float]] = None
