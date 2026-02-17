"""
Chat permission service — role-based visibility from MongoDB.

MongoDB collection: chat_permissions
Document shape:
    {
        "roleId": 2,                    # matches PostgreSQL roles.id
        "roleName": "manager",          # human-readable label (not used in queries)
        "targetRoleIds": [1, 2, 3],     # which role IDs this role can chat with (empty [] = all)
        "allCompanies": false,          # true = cross-company access
        "createdAt": "...",
        "updatedAt": "..."
    }

Rules:
    - No document for a role  -> default: all users in same company
    - Document with empty targetRoleIds [] -> all roles
    - Document with targetRoleIds [1,2,3] -> only those roles
    - allCompanies = true -> cross-company access
    - Multi-role user -> merge (union of targetRoleIds, OR of allCompanies)
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.mongodb import get_db as get_mongo_db
from app.models.role import UserRole
from app.schemas.permission import MergedPermission


async def get_user_role_ids(
    user_id: int, primary_role_id: int | None, db: AsyncSession
) -> list[int]:
    """Get all role IDs for a user (primary + secondary from user_roles)."""
    role_ids: list[int] = []
    if primary_role_id:
        role_ids.append(primary_role_id)

    result = await db.execute(
        select(UserRole.role_id).where(UserRole.user_id == user_id)
    )
    for rid in result.scalars().all():
        if rid not in role_ids:
            role_ids.append(rid)

    return role_ids


async def get_merged_permission(role_ids: list[int]) -> dict | None:
    """Get merged chat permission for a list of role IDs from MongoDB.

    Returns:
        {
            "targetRoleIds": [1, 2, 3],  # empty [] = all roles
            "allCompanies": False,
        }
        or None if no permissions found (= default: all users, same company)
    """
    if not role_ids:
        return None

    mongo = get_mongo_db()
    permissions = await mongo.chat_permissions.find(
        {"roleId": {"$in": role_ids}}
    ).to_list(length=100)

    if not permissions:
        return None

    merged_targets: set[int] = set()
    all_companies = False
    see_all_roles = False

    for perm in permissions:
        if perm.get("allCompanies"):
            all_companies = True
        targets = perm.get("targetRoleIds", [])
        if not targets:
            see_all_roles = True
        else:
            merged_targets.update(targets)

    result = MergedPermission(
        targetRoleIds=[] if see_all_roles else list(merged_targets),
        allCompanies=all_companies,
    )
    return result.model_dump()
