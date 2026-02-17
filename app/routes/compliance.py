"""
Compliance routes — enterprise audited access to chat messages.

Authorization model:
  Policy-as-authorization — an active compliance_policies document in MongoDB
  IS the authorization. No separate role needed. Super admin creates a policy
  for a user, and the policy controls what that user can access.

Endpoints:
  Super Admin (policy management):
    POST   /chat/compliance/policies                    Create policy for a user
    GET    /chat/compliance/policies                    List all policies
    GET    /chat/compliance/policies/{user_id}          Get user's policy
    PUT    /chat/compliance/policies/{user_id}          Update user's policy
    DELETE /chat/compliance/policies/{user_id}          Deactivate user's policy

  Policy Holder (scoped access):
    GET    /chat/compliance/users                         List users within policy scope
    GET    /chat/compliance/conversations/{id}/messages    Read conversation messages
    GET    /chat/compliance/users/{id}/messages             Read user's messages

  Super Admin (audit):
    GET    /chat/compliance/audit-logs                   View audit trail
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.common import CompliancePermission

from app.auth.jwt import CurrentUser, get_current_user
from app.database.postgres import get_db
from app.schemas.common import DeactivateResponse
from app.schemas.compliance import (
    CompliancePolicyResponse,
    PaginatedAuditLogs,
    PaginatedPolicies,
)
from app.schemas.message import PaginatedMessages
from app.schemas.user import ChatUser
from app.services import compliance_service


router = APIRouter(prefix="/chat/compliance", tags=["compliance"])


# ─── Request Schemas ──────────────────────────────────────────────────────────

class CreatePolicyRequest(BaseModel):
    userId: int = Field(..., description="User ID to grant compliance policy to")
    scope: str = Field(..., description="global | company | department | user")
    scopeIds: list[int] = Field(default=[], description="Company/department/user IDs for scope")
    permissions: list[str] = Field(
        default=[CompliancePermission.read_messages.value],
        description="Allowed actions: read_messages, export_messages, search_messages",
    )


class UpdatePolicyRequest(BaseModel):
    scope: str | None = None
    scopeIds: list[int] | None = None
    permissions: list[str] | None = None
    expiresAt: str | None = None
    isActive: bool | None = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _require_super_admin(current_user: CurrentUser, pg: AsyncSession):
    """Raise 403 if user is not a super_admin."""
    if not await compliance_service.is_super_admin(current_user.id, current_user.role_id, pg):
        raise HTTPException(status_code=403, detail="Only super admins can manage compliance policies")


# ─── Policy Management (Super Admin) ─────────────────────────────────────────

@router.post("/policies", response_model=CompliancePolicyResponse)
async def create_policy(
    body: CreatePolicyRequest,
    current_user: CurrentUser = Depends(get_current_user),
    pg: AsyncSession = Depends(get_db),
):
    """Create a compliance policy for a user. Only super admins."""
    await _require_super_admin(current_user, pg)

    try:
        policy = await compliance_service.create_policy(
            officer_user_id=body.userId,
            scope=body.scope,
            scope_ids=body.scopeIds,
            permissions=body.permissions,
            granted_by=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return policy


@router.get("/policies", response_model=PaginatedPolicies)
async def list_policies(
    page: int = Query(1, ge=1),
    limit: int = Query(settings.DEFAULT_PAGE_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT),
    current_user: CurrentUser = Depends(get_current_user),
    pg: AsyncSession = Depends(get_db),
):
    """List all compliance policies. Only super admins."""
    await _require_super_admin(current_user, pg)
    return await compliance_service.list_policies(page=page, limit=limit)


@router.get("/policies/{user_id}", response_model=CompliancePolicyResponse)
async def get_policy(
    user_id: int,
    current_user: CurrentUser = Depends(get_current_user),
    pg: AsyncSession = Depends(get_db),
):
    """Get a specific officer's active policy. Only super admins."""
    await _require_super_admin(current_user, pg)
    policy = await compliance_service.get_policy(user_id)
    if not policy:
        raise HTTPException(status_code=404, detail="No active policy found for this user")
    return policy


@router.put("/policies/{user_id}", response_model=CompliancePolicyResponse)
async def update_policy(
    user_id: int,
    body: UpdatePolicyRequest,
    current_user: CurrentUser = Depends(get_current_user),
    pg: AsyncSession = Depends(get_db),
):
    """Update an officer's active policy. Only super admins."""
    await _require_super_admin(current_user, pg)

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        policy = await compliance_service.update_policy(
            officer_user_id=user_id,
            updates=updates,
            updated_by=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not policy:
        raise HTTPException(status_code=404, detail="No active policy found for this user")
    return policy


@router.delete("/policies/{user_id}", response_model=DeactivateResponse)
async def deactivate_policy(
    user_id: int,
    current_user: CurrentUser = Depends(get_current_user),
    pg: AsyncSession = Depends(get_db),
):
    """Deactivate an officer's policy. Only super admins."""
    await _require_super_admin(current_user, pg)
    success = await compliance_service.deactivate_policy(user_id, current_user.id)
    if not success:
        raise HTTPException(status_code=404, detail="No active policy found for this user")
    return {"deactivated": True}


# ─── Scoped User List (Policy-Based) ──────────────────────────────────────────

@router.get("/users", response_model=list[ChatUser])
async def get_scoped_users(
    current_user: CurrentUser = Depends(get_current_user),
    pg: AsyncSession = Depends(get_db),
):
    """Get users within the officer's compliance policy scope."""
    users = await compliance_service.get_scoped_users(current_user.id, pg)
    if not users:
        raise HTTPException(status_code=403, detail="No active compliance policy found")
    return users


# ─── Message Access (Policy-Based) ────────────────────────────────────────────

@router.get("/conversations/{conversation_id}/messages", response_model=PaginatedMessages)
async def read_conversation_messages(
    conversation_id: str,
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(settings.DEFAULT_PAGE_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT),
    current_user: CurrentUser = Depends(get_current_user),
    pg: AsyncSession = Depends(get_db),
):
    """Read decrypted messages from a conversation (policy-based access, audited)."""
    try:
        return await compliance_service.get_conversation_messages(
            conversation_id=conversation_id,
            officer_id=current_user.id,
            page=page,
            limit=limit,
            pg=pg,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/users/{user_id}/messages", response_model=PaginatedMessages)
async def read_user_messages(
    user_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(settings.DEFAULT_PAGE_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT),
    current_user: CurrentUser = Depends(get_current_user),
    pg: AsyncSession = Depends(get_db),
):
    """Read all decrypted messages for a user (policy-based access, audited)."""
    try:
        return await compliance_service.get_user_messages(
            target_user_id=user_id,
            officer_id=current_user.id,
            page=page,
            limit=limit,
            pg=pg,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


# ─── Audit Logs (Super Admin) ────────────────────────────────────────────────

@router.get("/audit-logs", response_model=PaginatedAuditLogs)
async def get_audit_logs(
    page: int = Query(1, ge=1),
    limit: int = Query(settings.DEFAULT_PAGE_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT),
    officer_id: int | None = Query(None, description="Filter by officer ID"),
    current_user: CurrentUser = Depends(get_current_user),
    pg: AsyncSession = Depends(get_db),
):
    """View compliance audit trail. Only super admins."""
    await _require_super_admin(current_user, pg)
    return await compliance_service.get_audit_logs(
        page=page, limit=limit, officer_id=officer_id,
    )
