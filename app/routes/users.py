"""
User routes — list chatable users filtered by role-based permissions from MongoDB.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select, or_
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import CurrentUser, get_current_user
from app.database.postgres import get_db
from app.models.user import User
from app.models.role import UserRole
from app.schemas.user import ChatUser
from app.services.online_service import get_online_users
from app.services.permission_service import get_user_role_ids, get_merged_permission

router = APIRouter(prefix="/chat", tags=["users"])


@router.get("/users", response_model=list[ChatUser])
async def get_chatable_users(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all users that the current user can chat with, based on MongoDB permissions."""

    # Step 1: Get all role IDs for the requesting user (primary + secondary)
    my_role_ids = await get_user_role_ids(current_user.id, current_user.role_id, db)

    # Step 2: Get merged permission from MongoDB
    permission = await get_merged_permission(my_role_ids)

    # Step 3: Build query based on permission
    query = (
        select(User)
        .where(
            User.id != current_user.id,
            User.is_active == True,  # noqa: E712
            User.deleted_at.is_(None),
        )
    )

    if permission:
        # Permission exists — apply targetRoleIds and allCompanies
        target_role_ids = permission["targetRoleIds"]
        all_companies = permission["allCompanies"]

        if not all_companies:
            query = query.where(User.company_id == current_user.company_id)

        # Filter by target role IDs (empty [] = all roles, no filter needed)
        if target_role_ids:
            # User's primary role OR any secondary role must be in target list
            SecondaryRole = aliased(UserRole, name="secondary_role")
            query = (
                query
                .distinct()
                .outerjoin(SecondaryRole, SecondaryRole.user_id == User.id)
                .where(
                    or_(
                        User.role_id.in_(target_role_ids),
                        SecondaryRole.role_id.in_(target_role_ids),
                    )
                )
            )
    else:
        # No permission document → default: all users in same company
        query = query.where(User.company_id == current_user.company_id)

    query = query.order_by(User.firstname)

    result = await db.execute(query)
    users = result.scalars().all()

    # Get online user IDs from Redis — check all relevant companies
    all_company_ids = {u.company_id for u in users if u.company_id}
    all_company_ids.add(current_user.company_id)

    online_ids: set[str] = set()
    for cid in all_company_ids:
        online_ids.update(await get_online_users(cid))

    return [
        {
            "id": u.id,
            "firstname": u.firstname,
            "lastname": u.lastname,
            "email": u.email,
            "profilePicture": u.profile_picture,
            "isOnline": str(u.id) in online_ids,
        }
        for u in users
    ]
