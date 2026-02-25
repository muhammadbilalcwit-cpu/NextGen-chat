"""
MongoDB model for the 'conversations' collection (1:1 conversations).

Document shape:
    {
        "_id": ObjectId,
        "participants": [1, 2],
        "isGroup": false,
        "lastMessage": "Hello",
        "lastMessageSenderId": 1,
        "lastMessageAt": datetime,
        "deletedFor": [],
        "createdAt": datetime,
        "updatedAt": datetime,
        ...
    }

Support chat conversations have additional fields:
    {
        "isSupportChat": true,
        "supportStatus": "waiting" | "active" | "resolved",
        "supportMetadata": {
            "customerId": 123,
            "companyId": 5,
            "preferredAgentId": 11,
            "source": "widget",
            "waitingSince": datetime,
            "acceptedAt": datetime | null,
            "resolvedAt": datetime | null,
        }
    }
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SupportMetadata(BaseModel):
    """Metadata for support chat conversations."""
    customerId: int
    companyId: int
    preferredAgentId: Optional[int] = None
    source: str = "widget"
    waitingSince: Optional[datetime] = None
    acceptedAt: Optional[datetime] = None
    resolvedAt: Optional[datetime] = None


class ConversationDocument(BaseModel):
    """Represents a 1:1 conversation document in MongoDB."""
    participants: List[int] = Field(..., min_length=1)
    isGroup: bool = False
    lastMessage: Optional[str] = None
    lastMessageSenderId: Optional[int] = None
    lastMessageAt: Optional[datetime] = None
    deletedFor: List[int] = []
    groupName: Optional[str] = None
    groupAvatar: Optional[str] = None
    groupAdmin: Optional[int] = None
    companyId: Optional[int] = None
    memberJoinedAt: Dict[str, datetime] = {}
    lastMessageSystemType: Optional[str] = None
    lastMessageTargetUserId: Optional[int] = None
    lastMessageActorUserId: Optional[int] = None
    # Support chat fields
    isSupportChat: bool = False
    supportStatus: Optional[str] = None  # "waiting" | "active" | "resolved"
    supportMetadata: Optional[SupportMetadata] = None
    createdAt: datetime
    updatedAt: datetime
