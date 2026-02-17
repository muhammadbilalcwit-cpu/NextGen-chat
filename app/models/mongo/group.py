"""
MongoDB model for the 'conversations' collection (group conversations).

Document shape:
    {
        "_id": ObjectId,
        "participants": [1, 2, 3],
        "isGroup": true,
        "groupName": "My Group",
        "groupAvatar": "https://...",
        "groupAdmin": 1,
        "companyId": 1,
        "memberJoinedAt": { "1": datetime, "2": datetime },
        "createdAt": datetime,
        "updatedAt": datetime,
        ...
    }
"""
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class GroupConversationDocument(BaseModel):
    """Represents a group conversation document in MongoDB."""
    participants: List[int] = Field(..., min_length=1)
    isGroup: bool = True
    groupName: str = Field(..., min_length=1, max_length=100)
    groupAvatar: Optional[str] = None
    groupAdmin: int
    companyId: int
    lastMessage: Optional[str] = None
    lastMessageSenderId: Optional[int] = None
    lastMessageAt: datetime
    deletedFor: List[int] = []
    memberJoinedAt: Dict[str, datetime]
    lastMessageSystemType: Optional[str] = None
    lastMessageTargetUserId: Optional[int] = None
    lastMessageActorUserId: Optional[int] = None
    createdAt: datetime
    updatedAt: datetime
