"""
MongoDB models for compliance collections.

Collections:
    compliance_policies:    Scoped access policies for compliance officers
    compliance_audit_logs:  Immutable audit trail of compliance access
"""
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel


class CompliancePolicyDocument(BaseModel):
    """Represents a compliance policy document in MongoDB."""
    userId: int
    scope: str
    scopeIds: List[int] = []
    permissions: List[str]
    grantedBy: int
    isActive: bool = True
    expiresAt: Optional[datetime] = None
    createdAt: datetime
    updatedAt: datetime


class ComplianceAuditLogDocument(BaseModel):
    """Represents an audit log entry document in MongoDB."""
    officerId: int
    action: str
    targetType: str
    targetId: str
    scope: str
    metadata: Dict = {}
    ip: str = ""
    userAgent: str = ""
    timestamp: datetime
