"""
Group schemas — request/response models for group chats.
"""
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .user import GroupMember


# ─── Request Schemas ──────────────────────────────────────────────────────────

class CreateGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    memberIds: List[int] = Field(..., min_length=1)
    avatar: Optional[str] = None


class UpdateGroupRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    avatar: Optional[str] = None


class AddMembersRequest(BaseModel):
    memberIds: List[int] = Field(..., min_length=1)


class LeaveGroupRequest(BaseModel):
    newAdminId: Optional[int] = None


# ─── Response Schemas (validated before returning to client) ──────────────────

class GroupResponse(BaseModel):
    """Validated response for group conversations returned from API."""
    id: str = Field(..., alias="_id")
    participants: List[int]
    isGroup: bool = True
    groupName: str
    groupAvatar: Optional[str] = None
    groupAdmin: int
    companyId: Optional[int] = None
    lastMessage: Optional[str] = None
    lastMessageSenderId: Optional[int] = None
    lastMessageAt: Optional[str] = None
    deletedFor: List[int] = []
    memberJoinedAt: Dict[str, str] = {}
    lastMessageSystemType: Optional[str] = None
    lastMessageTargetUserId: Optional[int] = None
    lastMessageActorUserId: Optional[int] = None
    lastMessageActorName: Optional[str] = None
    lastMessageTargetName: Optional[str] = None
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None
    unreadCount: int = 0
    members: List[GroupMember] = []

    model_config = {"populate_by_name": True}


class LeaveGroupResponse(BaseModel):
    left: bool


class GroupAvatarResponse(BaseModel):
    avatarUrl: str
    group: GroupResponse
