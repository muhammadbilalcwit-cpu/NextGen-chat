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
"""
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ConversationDocument(BaseModel):
    """Represents a 1:1 conversation document in MongoDB."""
    participants: List[int] = Field(..., min_length=2, max_length=2)
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
    createdAt: datetime
    updatedAt: datetime
