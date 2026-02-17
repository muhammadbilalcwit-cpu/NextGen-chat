"""
Chat permission schemas — response models for chat permissions.
"""
from typing import List

from pydantic import BaseModel


class MergedPermission(BaseModel):
    """Validated merged permission result."""
    targetRoleIds: List[int] = []
    allCompanies: bool = False
