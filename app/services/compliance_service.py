"""
Enterprise compliance service — audited access to encrypted chat messages.

Authorization model: Policy-as-authorization (IAM pattern)
  The compliance_policies document IS the authorization. No separate role needed.
  Super admin creates a policy → user can access messages within that scope.

Two MongoDB collections:
  compliance_policies:    Scoped access policies (who can audit whose messages)
  compliance_audit_logs:  Immutable audit trail of every compliance access

Policy scope hierarchy:
  global     → All messages across all companies
  company    → All messages within specific companies
  department → All messages within specific departments
  user       → Only messages involving specific user IDs

Flow:
  1. Super admin creates a scoped policy in MongoDB via POST /chat/compliance/policies
  2. User reads messages → FastAPI checks policy scope → decrypts → logs access
  3. No policy = No access (even super_admins need explicit policy)
"""
from datetime import datetime, timezone
from bson import ObjectId

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.mongodb import get_db
from app.models.user import User
from app.models.role import Role, UserRole
from app.services.encryption_service import decrypt_content
from app.models.mongo import CompliancePolicyDocument, ComplianceAuditLogDocument
from app.schemas.compliance import CompliancePolicyResponse, ComplianceAuditLogResponse
from app.schemas.message import MessageResponse


# ─── Constants (derived from enums — single source of truth in schemas/common.py)

from app.schemas.common import ComplianceScope, CompliancePermission

VALID_SCOPES = tuple(e.value for e in ComplianceScope)
VALID_PERMISSIONS = tuple(e.value for e in CompliancePermission)


def _validate_response(data: dict, schema_cls):
    """Validate serialized data against a response schema."""
    return schema_cls.model_validate(data).model_dump(by_alias=True)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _oid(id_str: str) -> ObjectId:
    return ObjectId(id_str)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(doc: dict) -> dict:
    """Convert MongoDB doc for JSON (ObjectId → str, datetime → ISO)."""
    if doc is None:
        return None
    result = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            result[k] = str(v)
        elif isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            result[k] = v.isoformat()
        elif isinstance(v, dict):
            result[k] = _serialize(v)
        elif isinstance(v, list):
            result[k] = [
                _serialize(i) if isinstance(i, dict)
                else (str(i) if isinstance(i, ObjectId) else i)
                for i in v
            ]
        else:
            result[k] = v
    return result


def _decrypt_message(msg: dict) -> dict:
    """Decrypt a message's content if encrypted at rest."""
    if msg.get("isEncrypted") and msg.get("encryptedContent"):
        try:
            conv_id = msg.get("conversationId", "")
            if isinstance(conv_id, ObjectId):
                conv_id = str(conv_id)
            msg["content"] = decrypt_content(
                msg["encryptedContent"],
                msg["contentIv"],
                msg["contentTag"],
                conv_id,
            )
        except Exception:
            msg["content"] = "[Decryption failed]"
        for key in ("encryptedContent", "contentIv", "contentTag"):
            msg.pop(key, None)
    return msg


async def _build_message_senders(messages: list[dict], pg: AsyncSession) -> dict[str, dict]:
    """Collect all sender/recipient IDs from messages and return a user info map."""
    user_ids = set()
    for msg in messages:
        if msg.get("senderId"):
            user_ids.add(msg["senderId"])
        if msg.get("recipientId"):
            user_ids.add(msg["recipientId"])

    if not user_ids:
        return {}

    users = await _get_users_by_ids(list(user_ids), pg)
    return {
        str(u.id): {
            "id": u.id,
            "firstname": u.firstname,
            "lastname": u.lastname,
            "profilePicture": u.profile_picture,
        }
        for u in users
    }


# ─── Role Checks (PostgreSQL) ────────────────────────────────────────────────

async def _get_all_role_ids(user_id: int, primary_role_id: int | None, pg: AsyncSession) -> list[int]:
    """Get primary + secondary role IDs for a user."""
    role_ids = [primary_role_id] if primary_role_id else []
    result = await pg.execute(
        select(UserRole.role_id).where(UserRole.user_id == user_id)
    )
    for rid in result.scalars().all():
        if rid not in role_ids:
            role_ids.append(rid)
    return role_ids


async def _get_role_slugs(role_ids: list[int], pg: AsyncSession) -> set[str]:
    """Get role slugs for a list of role IDs."""
    if not role_ids:
        return set()
    result = await pg.execute(
        select(Role.slug).where(Role.id.in_(role_ids))
    )
    return set(result.scalars().all())


async def is_super_admin(user_id: int, role_id: int | None, pg: AsyncSession) -> bool:
    """Check if user has super_admin role (primary or secondary)."""
    role_ids = await _get_all_role_ids(user_id, role_id, pg)
    slugs = await _get_role_slugs(role_ids, pg)
    return "super_admin" in slugs


# ─── Scoped User List ────────────────────────────────────────────────────────

async def get_scoped_users(officer_id: int, pg: AsyncSession) -> list[dict]:
    """
    Get users that fall within the officer's compliance policy scope.
    Returns list of user dicts matching ChatUser schema.
    """
    policy = await get_policy(officer_id)
    if not policy:
        return []

    scope = policy["scope"]
    scope_ids = policy.get("scopeIds", [])

    # Build query filter based on scope
    base = select(User).where(User.is_active == True, User.deleted_at.is_(None))  # noqa: E712

    if scope == "global":
        query = base
    elif scope == "company":
        query = base.where(User.company_id.in_(scope_ids))
    elif scope == "department":
        query = base.where(User.department_id.in_(scope_ids))
    elif scope == "user":
        query = base.where(User.id.in_(scope_ids))
    else:
        return []

    result = await pg.execute(query.order_by(User.firstname, User.lastname))
    users = result.scalars().all()

    return [
        {
            "id": u.id,
            "firstname": u.firstname,
            "lastname": u.lastname,
            "email": u.email,
            "profilePicture": u.profile_picture,
            "isOnline": False,
        }
        for u in users
    ]


# ─── Policy CRUD ──────────────────────────────────────────────────────────────

async def create_policy(
    officer_user_id: int,
    scope: str,
    scope_ids: list[int],
    permissions: list[str],
    granted_by: int,
) -> dict:
    """Create a compliance policy for an officer. One active policy per officer."""
    db = get_db()

    existing = await db.compliance_policies.find_one({"userId": officer_user_id, "isActive": True})
    if existing:
        raise ValueError("Officer already has an active policy. Update or deactivate it first.")

    if scope not in VALID_SCOPES:
        raise ValueError(f"Invalid scope: {scope}. Must be one of {VALID_SCOPES}")

    if scope != "global" and not scope_ids:
        raise ValueError(f"scopeIds required for scope '{scope}'")

    invalid_perms = set(permissions) - set(VALID_PERMISSIONS)
    if invalid_perms:
        raise ValueError(f"Invalid permissions: {invalid_perms}. Must be from {VALID_PERMISSIONS}")

    now = _now()
    validated = CompliancePolicyDocument(
        userId=officer_user_id,
        scope=scope,
        scopeIds=scope_ids if scope != "global" else [],
        permissions=permissions or list(VALID_PERMISSIONS),
        grantedBy=granted_by,
        createdAt=now,
        updatedAt=now,
    )
    doc = validated.model_dump()

    result = await db.compliance_policies.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _validate_response(_serialize(doc), CompliancePolicyResponse)


async def get_policy(officer_user_id: int) -> dict | None:
    """Get the active compliance policy for an officer."""
    db = get_db()
    doc = await db.compliance_policies.find_one({
        "userId": officer_user_id,
        "isActive": True,
    })
    if not doc:
        return None

    # Auto-expire if past expiresAt
    if doc.get("expiresAt") and doc["expiresAt"] < _now():
        await db.compliance_policies.update_one(
            {"_id": doc["_id"]},
            {"$set": {"isActive": False, "updatedAt": _now()}},
        )
        return None

    return _validate_response(_serialize(doc), CompliancePolicyResponse)


async def list_policies(page: int = 1, limit: int = settings.DEFAULT_PAGE_LIMIT) -> dict:
    """List all compliance policies (active and inactive)."""
    db = get_db()
    skip = (page - 1) * limit

    total = await db.compliance_policies.count_documents({})
    cursor = db.compliance_policies.find({}).sort("createdAt", -1).skip(skip).limit(limit)

    policies = []
    async for doc in cursor:
        policies.append(_validate_response(_serialize(doc), CompliancePolicyResponse))

    return {
        "policies": policies,
        "total": total,
        "hasMore": (skip + limit) < total,
    }


async def update_policy(
    officer_user_id: int,
    updates: dict,
    updated_by: int,
) -> dict | None:
    """Update an officer's active compliance policy."""
    db = get_db()

    allowed_fields = {}
    if "scope" in updates:
        if updates["scope"] not in VALID_SCOPES:
            raise ValueError(f"Invalid scope: {updates['scope']}")
        allowed_fields["scope"] = updates["scope"]

    if "scopeIds" in updates:
        allowed_fields["scopeIds"] = updates["scopeIds"]

    if "permissions" in updates:
        invalid = set(updates["permissions"]) - set(VALID_PERMISSIONS)
        if invalid:
            raise ValueError(f"Invalid permissions: {invalid}")
        allowed_fields["permissions"] = updates["permissions"]

    if "expiresAt" in updates:
        allowed_fields["expiresAt"] = updates["expiresAt"]

    if "isActive" in updates:
        allowed_fields["isActive"] = updates["isActive"]

    if not allowed_fields:
        return await get_policy(officer_user_id)

    allowed_fields["updatedAt"] = _now()
    allowed_fields["grantedBy"] = updated_by

    result = await db.compliance_policies.update_one(
        {"userId": officer_user_id, "isActive": True},
        {"$set": allowed_fields},
    )
    if result.matched_count == 0:
        return None

    return await get_policy(officer_user_id)


async def deactivate_policy(officer_user_id: int, deactivated_by: int) -> bool:
    """Deactivate an officer's compliance policy."""
    db = get_db()
    result = await db.compliance_policies.update_one(
        {"userId": officer_user_id, "isActive": True},
        {"$set": {"isActive": False, "updatedAt": _now(), "grantedBy": deactivated_by}},
    )
    return result.modified_count > 0


# ─── Scope Validation ─────────────────────────────────────────────────────────

async def _get_conversation_participant_ids(conversation_id: str) -> list[int]:
    """Get participant user IDs for a conversation."""
    db = get_db()
    conv = await db.conversations.find_one({"_id": _oid(conversation_id)})
    if not conv:
        return []
    return conv.get("participants", [])


async def _get_users_by_ids(user_ids: list[int], pg: AsyncSession) -> list[User]:
    """Load users from PostgreSQL by IDs."""
    if not user_ids:
        return []
    result = await pg.execute(select(User).where(User.id.in_(user_ids)))
    return list(result.scalars().all())


async def validate_scope(
    policy: dict,
    target_type: str,
    target_id: str | int,
    pg: AsyncSession,
) -> tuple[bool, str]:
    """
    Validate officer's policy scope against the target.
    Returns (allowed: bool, reason: str).
    """
    scope = policy["scope"]
    scope_ids = policy.get("scopeIds", [])

    if scope == "global":
        return True, "global access"

    # Get the users involved in the target
    if target_type == "conversation":
        participant_ids = await _get_conversation_participant_ids(str(target_id))
        if not participant_ids:
            return False, "Conversation not found"
        users = await _get_users_by_ids(participant_ids, pg)
    elif target_type == "user":
        users = await _get_users_by_ids([int(target_id)], pg)
        if not users:
            return False, "User not found"
    else:
        return False, f"Unknown target type: {target_type}"

    if scope == "company":
        for user in users:
            if user.company_id not in scope_ids:
                return False, f"User {user.id} belongs to company {user.company_id}, outside allowed companies"
        return True, "company scope matched"

    elif scope == "department":
        for user in users:
            if user.department_id not in scope_ids:
                return False, f"User {user.id} belongs to department {user.department_id}, outside allowed departments"
        return True, "department scope matched"

    elif scope == "user":
        if target_type == "user":
            if int(target_id) not in scope_ids:
                return False, f"User {target_id} not in allowed user list"
            return True, "user scope matched"
        else:
            # Conversation: at least one participant must be in scope
            if not any(u.id in scope_ids for u in users):
                return False, "No participant in conversation is in allowed user list"
            return True, "user scope matched"

    return False, f"Unknown scope: {scope}"


# ─── Audit Logging ────────────────────────────────────────────────────────────

async def log_access(
    officer_id: int,
    action: str,
    target_type: str,
    target_id: str | int,
    scope: str,
    ip: str | None = None,
    user_agent: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Write an immutable audit log entry."""
    db = get_db()
    validated = ComplianceAuditLogDocument(
        officerId=officer_id,
        action=action,
        targetType=target_type,
        targetId=str(target_id),
        scope=scope,
        metadata=metadata or {},
        ip=ip or "",
        userAgent=user_agent or "",
        timestamp=_now(),
    )
    await db.compliance_audit_logs.insert_one(validated.model_dump())


async def get_audit_logs(
    page: int = 1,
    limit: int = settings.DEFAULT_PAGE_LIMIT,
    officer_id: int | None = None,
) -> dict:
    """Get paginated audit logs, optionally filtered by officer."""
    db = get_db()
    skip = (page - 1) * limit

    query = {}
    if officer_id is not None:
        query["officerId"] = officer_id

    total = await db.compliance_audit_logs.count_documents(query)
    cursor = db.compliance_audit_logs.find(query).sort("timestamp", -1).skip(skip).limit(limit)

    logs = []
    async for doc in cursor:
        logs.append(_validate_response(_serialize(doc), ComplianceAuditLogResponse))

    return {
        "logs": logs,
        "total": total,
        "hasMore": (skip + limit) < total,
    }


# ─── Message Access (Decrypted + Audited) ────────────────────────────────────

async def get_conversation_messages(
    conversation_id: str,
    officer_id: int,
    page: int = 1,
    limit: int = settings.DEFAULT_PAGE_LIMIT,
    pg: AsyncSession = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """
    Get decrypted messages for a conversation (compliance access).
    Validates policy scope and logs the access.
    """
    policy = await get_policy(officer_id)
    if not policy:
        raise PermissionError("No active compliance policy found")

    if "read_messages" not in policy.get("permissions", []):
        raise PermissionError("Policy does not include read_messages permission")

    allowed, reason = await validate_scope(policy, "conversation", conversation_id, pg)
    if not allowed:
        raise PermissionError(f"Access denied: {reason}")

    db = get_db()
    skip = (page - 1) * limit

    query = {"conversationId": _oid(conversation_id)}
    total = await db.messages.count_documents(query)
    cursor = db.messages.find(query).sort("createdAt", -1).skip(skip).limit(limit)

    messages = []
    async for msg in cursor:
        serialized = _decrypt_message(_serialize(msg))
        messages.append(_validate_response(serialized, MessageResponse))
    messages.reverse()

    await log_access(
        officer_id=officer_id,
        action="read_messages",
        target_type="conversation",
        target_id=conversation_id,
        scope=policy["scope"],
        ip=ip,
        user_agent=user_agent,
        metadata={"page": page, "limit": limit, "messageCount": len(messages)},
    )

    senders = await _build_message_senders(messages, pg)

    return {
        "messages": messages,
        "total": total,
        "hasMore": (skip + limit) < total,
        "messageSenders": senders,
    }


async def _get_user_group_conversation_ids(user_id: int) -> list[str]:
    """Find all group conversation IDs where the user is a participant."""
    db = get_db()
    cursor = db.conversations.find(
        {"isGroup": True, "participants": user_id},
        {"_id": 1},
    )
    conv_ids = []
    async for doc in cursor:
        conv_ids.append(str(doc["_id"]))
    return conv_ids


async def _build_conversation_info(conversation_ids: list[str]) -> dict[str, dict]:
    """Build a map of conversationId → {groupName, groupAvatar} for group conversations."""
    if not conversation_ids:
        return {}
    db = get_db()
    oids = [_oid(cid) for cid in conversation_ids]
    cursor = db.conversations.find(
        {"_id": {"$in": oids}, "isGroup": True},
        {"_id": 1, "groupName": 1, "groupAvatar": 1},
    )
    info = {}
    async for doc in cursor:
        info[str(doc["_id"])] = {
            "groupName": doc.get("groupName", "Unknown Group"),
            "groupAvatar": doc.get("groupAvatar"),
            "isGroup": True,
        }
    return info


async def get_user_messages(
    target_user_id: int,
    officer_id: int,
    page: int = 1,
    limit: int = settings.DEFAULT_PAGE_LIMIT,
    pg: AsyncSession = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """
    Get all decrypted messages sent by or to a specific user (compliance access).
    Includes DMs and full group conversations the user participates in.
    Validates policy scope and logs the access.
    """
    policy = await get_policy(officer_id)
    if not policy:
        raise PermissionError("No active compliance policy found")

    if "read_messages" not in policy.get("permissions", []):
        raise PermissionError("Policy does not include read_messages permission")

    allowed, reason = await validate_scope(policy, "user", target_user_id, pg)
    if not allowed:
        raise PermissionError(f"Access denied: {reason}")

    db = get_db()
    skip = (page - 1) * limit

    # Find group conversations the user participates in
    group_conv_ids = await _get_user_group_conversation_ids(target_user_id)
    group_conv_oids = [_oid(cid) for cid in group_conv_ids]

    # Query: DMs (sender/recipient) + all messages in user's group conversations
    or_conditions = [
        {"senderId": target_user_id, "isGroupMessage": {"$ne": True}},
        {"recipientId": target_user_id, "isGroupMessage": {"$ne": True}},
    ]
    if group_conv_oids:
        or_conditions.append(
            {"conversationId": {"$in": group_conv_oids}, "isGroupMessage": True}
        )

    query = {"$or": or_conditions}
    total = await db.messages.count_documents(query)
    cursor = db.messages.find(query).sort("createdAt", -1).skip(skip).limit(limit)

    messages = []
    async for msg in cursor:
        serialized = _decrypt_message(_serialize(msg))
        messages.append(_validate_response(serialized, MessageResponse))
    messages.reverse()

    await log_access(
        officer_id=officer_id,
        action="read_messages",
        target_type="user",
        target_id=target_user_id,
        scope=policy["scope"],
        ip=ip,
        user_agent=user_agent,
        metadata={"page": page, "limit": limit, "messageCount": len(messages)},
    )

    senders = await _build_message_senders(messages, pg)

    # Build conversation info for group messages on this page
    group_ids_on_page = set()
    for msg in messages:
        if msg.get("isGroupMessage"):
            group_ids_on_page.add(msg["conversationId"])
    conv_info = await _build_conversation_info(list(group_ids_on_page))

    return {
        "messages": messages,
        "total": total,
        "hasMore": (skip + limit) < total,
        "messageSenders": senders,
        "conversationInfo": conv_info,
    }