"""
MongoDB model for the 'messages' collection.

Document shape:
    {
        "_id": ObjectId,
        "conversationId": ObjectId,
        "senderId": 1,
        "content": "Hello",
        "status": "sent",
        "isEncrypted": true,
        "encryptedContent": "base64...",
        "contentIv": "base64...",
        "contentTag": "base64...",
        "attachment": { "type": "image", "url": "...", ... },
        "mentions": [{ "userId": 2, "offset": 0, "length": 5 }],
        "createdAt": datetime,
        "updatedAt": datetime,
        ...
    }
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


# ─── Sub-document models (embedded in MessageDocument) ────────────────────────

class AttachmentSubDoc(BaseModel):
    """Embedded attachment within a message document."""
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


class MentionSubDoc(BaseModel):
    """Embedded mention within a message document."""
    userId: int
    offset: int
    length: int


class DeliveryEntrySubDoc(BaseModel):
    """Embedded delivery/read entry within a message document."""
    userId: int
    timestamp: datetime


# ─── Message document ────────────────────────────────────────────────────────

class MessageDocument(BaseModel):
    """Represents a regular message document in MongoDB."""
    conversationId: str
    senderId: int
    recipientId: Optional[int] = None
    content: str = ""
    status: str = "sent"
    deliveredAt: Optional[datetime] = None
    readAt: Optional[datetime] = None
    isDeleted: bool = False
    deletedFor: List[int] = []
    deletedAt: Optional[datetime] = None
    isGroupMessage: bool = False
    deliveredTo: List[DeliveryEntrySubDoc] = []
    readBy: List[DeliveryEntrySubDoc] = []
    isSystemMessage: bool = False
    systemMessageType: Optional[str] = None
    targetUserId: Optional[int] = None
    actorUserId: Optional[int] = None
    attachment: Optional[AttachmentSubDoc] = None
    mentions: List[MentionSubDoc] = []
    mentionsAll: bool = False
    createdAt: datetime
    updatedAt: datetime


class SystemMessageDocument(BaseModel):
    """Represents a system message document in MongoDB (member added/removed/left etc.)."""
    conversationId: str
    senderId: int
    recipientId: None = None
    content: str = ""
    status: str = "sent"
    deliveredAt: None = None
    readAt: None = None
    isDeleted: bool = False
    deletedFor: List[int] = []
    deletedAt: None = None
    isGroupMessage: bool = True
    deliveredTo: List[DeliveryEntrySubDoc] = []
    readBy: List[DeliveryEntrySubDoc] = []
    isSystemMessage: bool = True
    systemMessageType: str
    targetUserId: Optional[int] = None
    actorUserId: int
    actorName: Optional[str] = None
    targetName: Optional[str] = None
    attachment: None = None
    mentions: list = []
    mentionsAll: bool = False
    createdAt: datetime
    updatedAt: datetime
