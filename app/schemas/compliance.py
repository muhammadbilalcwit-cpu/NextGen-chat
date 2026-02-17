"""
Compliance schemas — response models for policies and audit logs.
"""
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ─── Response Schemas (validated before returning to client) ──────────────────

class CompliancePolicyResponse(BaseModel):
    """Validated response for compliance policies."""
    id: str = Field(..., alias="_id")
    userId: int
    scope: str
    scopeIds: List[int] = []
    permissions: List[str]
    grantedBy: int
    isActive: bool
    expiresAt: Optional[str] = None
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None

    model_config = {"populate_by_name": True}


class PaginatedPolicies(BaseModel):
    policies: List[CompliancePolicyResponse]
    total: int
    hasMore: bool


class ComplianceAuditLogResponse(BaseModel):
    """Validated response for audit log entries."""
    id: str = Field(..., alias="_id")
    officerId: int
    action: str
    targetType: str
    targetId: str
    scope: str
    metadata: Dict = {}
    ip: str = ""
    userAgent: str = ""
    timestamp: Optional[str] = None

    model_config = {"populate_by_name": True}


class PaginatedAuditLogs(BaseModel):
    logs: List[ComplianceAuditLogResponse]
    total: int
    hasMore: bool
