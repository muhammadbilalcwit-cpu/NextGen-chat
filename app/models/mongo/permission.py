"""
MongoDB model for the 'chat_permissions' collection.

Document shape:
    {
        "roleId": 2,
        "roleName": "manager",
        "targetRoleIds": [1, 2, 3],
        "allCompanies": false,
        "createdAt": "...",
        "updatedAt": "..."
    }
"""
from typing import List, Optional

from pydantic import BaseModel


class ChatPermissionDocument(BaseModel):
    """Represents a chat permission document in MongoDB."""
    roleId: int
    roleName: Optional[str] = None
    targetRoleIds: List[int] = []
    allCompanies: bool = False
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None
