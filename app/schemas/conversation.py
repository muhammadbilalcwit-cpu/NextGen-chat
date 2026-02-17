"""
Conversation schemas — response models for 1:1 conversations.
"""
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .user import ChatUser


# ─── Response Schema (validated before returning to client) ───────────────────

class ConversationResponse(BaseModel):
    """Validated response for 1:1 conversations returned from API."""
    id: str = Field(..., alias="_id")
    participants: List[int]
    isGroup: bool
    lastMessage: Optional[str] = None
    lastMessageSenderId: Optional[int] = None
    lastMessageAt: Optional[str] = None
    deletedFor: List[int] = []
    groupName: Optional[str] = None
    groupAvatar: Optional[str] = None
    groupAdmin: Optional[int] = None
    companyId: Optional[int] = None
    memberJoinedAt: Dict[str, str] = {}
    lastMessageSystemType: Optional[str] = None
    lastMessageTargetUserId: Optional[int] = None
    lastMessageActorUserId: Optional[int] = None
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None
    otherUser: Optional[ChatUser] = None
    unreadCount: int = 0

    model_config = {"populate_by_name": True}