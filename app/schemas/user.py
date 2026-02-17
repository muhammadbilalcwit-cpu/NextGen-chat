"""
User schemas — response models for chat users.
"""
from typing import Optional

from pydantic import BaseModel


class ChatUser(BaseModel):
    id: int
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    email: str
    profilePicture: Optional[str] = None
    isOnline: bool = False


class GroupMember(BaseModel):
    """Member details enriched at route level for group display."""
    id: int
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    profilePicture: Optional[str] = None


class MessageSender(BaseModel):
    """Sender details enriched at route level for group messages."""
    id: int
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    profilePicture: Optional[str] = None


class UserBrief(BaseModel):
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    profilePicture: Optional[str] = None


class MessageInfoUser(BaseModel):
    userId: int
    timestamp: Optional[str] = None
    user: Optional[UserBrief] = None


class PendingUser(BaseModel):
    userId: int
    user: Optional[UserBrief] = None
