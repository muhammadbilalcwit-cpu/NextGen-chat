"""
Group chat routes — create, update, manage members, leave, delete.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import CurrentUser, get_current_user
from app.config import settings
from app.database.postgres import get_db
from app.models.user import User
from app.schemas.group import (
    AddMembersRequest,
    CreateGroupRequest,
    GroupResponse,
    LeaveGroupRequest,
    LeaveGroupResponse,
    UpdateGroupRequest,
)
from app.schemas.common import DeleteResponse
from app.schemas.message import MarkReadResponse, PaginatedMessages
from app.services import chat_service
from app.services.online_service import get_online_users
from app.websocket.manager import emit_to_user, emit_to_users

router = APIRouter(prefix="/chat/groups", tags=["groups"])


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_joined_at(group: dict, user_id: int) -> datetime | None:
    """Parse memberJoinedAt isoformat string back to datetime for DB queries."""
    joined_at_str = (group.get("memberJoinedAt") or {}).get(str(user_id))
    if joined_at_str and isinstance(joined_at_str, str):
        return datetime.fromisoformat(joined_at_str)
    return joined_at_str  # already datetime or None


async def _get_user_names(user_ids: list[int], db: AsyncSession) -> dict[int, str]:
    """Batch lookup display names from PostgreSQL. Returns {user_id: 'First Last'}."""
    if not user_ids:
        return {}
    result = await db.execute(select(User).where(User.id.in_(user_ids)))
    name_map: dict[int, str] = {}
    for u in result.scalars().all():
        name_map[u.id] = f"{u.firstname or ''} {u.lastname or ''}".strip() or u.email
    return name_map


async def _enrich_groups_with_members(groups: list[dict], db: AsyncSession) -> None:
    """Add member details (name, picture) to each group for frontend display.
    Also resolves lastMessageActorName/lastMessageTargetName for group list preview."""
    all_ids: set[int] = set()
    for g in groups:
        all_ids.update(g.get("participants", []))
        # Include system message actor/target IDs for name resolution (handles former members)
        if g.get("lastMessageActorUserId"):
            all_ids.add(g["lastMessageActorUserId"])
        if g.get("lastMessageTargetUserId"):
            all_ids.add(g["lastMessageTargetUserId"])

    if not all_ids:
        return

    result = await db.execute(select(User).where(User.id.in_(all_ids)))
    user_map: dict[int, dict] = {}
    for u in result.scalars().all():
        user_map[u.id] = {
            "id": u.id,
            "firstname": u.firstname,
            "lastname": u.lastname,
            "profilePicture": u.profile_picture,
        }

    for g in groups:
        g["members"] = [
            user_map.get(pid, {"id": pid, "firstname": None, "lastname": None, "profilePicture": None})
            for pid in g.get("participants", [])
        ]
        # Set lastMessageActorName/lastMessageTargetName for group list preview
        actor_id = g.get("lastMessageActorUserId")
        target_id = g.get("lastMessageTargetUserId")
        if actor_id and actor_id in user_map:
            u = user_map[actor_id]
            g["lastMessageActorName"] = f"{u['firstname'] or ''} {u['lastname'] or ''}".strip() or None
        if target_id and target_id in user_map:
            u = user_map[target_id]
            g["lastMessageTargetName"] = f"{u['firstname'] or ''} {u['lastname'] or ''}".strip() or None


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=list[GroupResponse])
async def get_groups(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all groups the current user is in."""
    groups = await chat_service.get_user_groups(current_user.id)
    for group in groups:
        joined_at = _parse_joined_at(group, current_user.id)
        group["unreadCount"] = await chat_service.get_conversation_unread_count(
            group["_id"], current_user.id, is_group=True, member_joined_at=joined_at
        )

    await _enrich_groups_with_members(groups, db)
    return groups


@router.post("", response_model=GroupResponse)
async def create_group(
    body: CreateGroupRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new group chat."""

    group = await chat_service.create_group(
        name=body.name.strip(),
        admin_id=current_user.id,
        member_ids=body.memberIds,
        company_id=current_user.company_id,
        avatar=body.avatar,
    )

    # Enrich group with member details (so frontend can display names)
    await _enrich_groups_with_members([group], db)

    # Create system message for group creation (targetUserId must be non-null for frontend rendering)
    sys_msg = await chat_service.save_system_message(
        group["_id"], "group_created", current_user.id, current_user.id,
        actor_name=current_user.name, target_name=current_user.name,
    )

    # Notify added members (not the creator)
    for member_id in body.memberIds:
        if member_id != current_user.id:
            await emit_to_user(member_id, "chat:group_member_added", {
                "groupId": group["_id"],
                "group": group,
            })

    # Emit system message to all members (exclude creator — they get the group via API response)
    await emit_to_users(
        group["participants"],
        "chat:group_system_message",
        {"groupId": group["_id"], "message": sys_msg},
        exclude_user=current_user.id,
    )

    return group


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get group details."""
    group = await chat_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if current_user.id not in group["participants"]:
        raise HTTPException(status_code=403, detail="Not a member")
    await _enrich_groups_with_members([group], db)
    return group


@router.patch("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: str,
    body: UpdateGroupRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Update group name or avatar (admin only)."""
    group = await chat_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["groupAdmin"] != current_user.id:
        raise HTTPException(status_code=403, detail="Only admin can update group")

    updates = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.avatar is not None:
        updates["avatar"] = body.avatar

    updated = await chat_service.update_group(group_id, updates)

    # Notify all members
    await emit_to_users(
        updated["participants"],
        "chat:group_updated",
        {"group": updated},
        exclude_user=current_user.id,
    )

    return updated


@router.post("/{group_id}/members", response_model=GroupResponse)
async def add_members(
    group_id: str,
    body: AddMembersRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add members to a group (admin only)."""
    group = await chat_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["groupAdmin"] != current_user.id:
        raise HTTPException(status_code=403, detail="Only admin can add members")

    # Filter out existing members
    new_ids = [m for m in body.memberIds if m not in group["participants"]]
    if not new_ids:
        return group

    updated = await chat_service.add_group_members(group_id, new_ids, current_user.id)

    # Enrich with member details
    await _enrich_groups_with_members([updated], db)

    # Batch lookup target names for system messages
    target_names = await _get_user_names(new_ids, db)

    # Create and emit system messages for each new member
    for member_id in new_ids:
        sys_msg = await chat_service.save_system_message(
            group_id, "member_added", current_user.id, member_id,
            actor_name=current_user.name, target_name=target_names.get(member_id),
        )
        await emit_to_users(
            updated["participants"],
            "chat:group_system_message",
            {"groupId": group_id, "message": sys_msg},
        )

    # Notify new members individually
    for member_id in new_ids:
        await emit_to_user(member_id, "chat:group_member_added", {
            "groupId": group_id,
            "group": updated,
        })

    # Notify existing members about new additions
    await emit_to_users(
        group["participants"],
        "chat:group_members_added",
        {
            "groupId": group_id,
            "newMemberIds": new_ids,
            "group": updated,
        },
        exclude_user=current_user.id,
    )

    return updated


@router.delete("/{group_id}/members/{member_id}", response_model=GroupResponse)
async def remove_member(
    group_id: str,
    member_id: int,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a member from a group (admin only)."""
    group = await chat_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["groupAdmin"] != current_user.id:
        raise HTTPException(status_code=403, detail="Only admin can remove members")
    if member_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot remove yourself, use leave")

    # Lookup target name before removing (1 query)
    target_names = await _get_user_names([member_id], db)

    updated = await chat_service.remove_group_member(group_id, member_id, current_user.id)

    # Create and emit system message
    sys_msg = await chat_service.save_system_message(
        group_id, "member_removed", current_user.id, member_id,
        actor_name=current_user.name, target_name=target_names.get(member_id),
    )
    if updated:
        await emit_to_users(
            updated["participants"],
            "chat:group_system_message",
            {"groupId": group_id, "message": sys_msg},
        )

    # Notify removed member
    await emit_to_user(member_id, "chat:group_member_removed", {
        "groupId": group_id,
    })

    # Notify remaining members
    if updated:
        await emit_to_users(
            updated["participants"],
            "chat:group_member_left",
            {
                "groupId": group_id,
                "leftUserId": member_id,
            },
            exclude_user=current_user.id,
        )

    return updated


@router.post("/{group_id}/leave", response_model=LeaveGroupResponse)
async def leave_group(
    group_id: str,
    body: LeaveGroupRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Leave a group. Admin must specify new admin."""
    group = await chat_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if current_user.id not in group["participants"]:
        raise HTTPException(status_code=403, detail="Not a member")

    # Admin must specify new admin if there are other members
    if group["groupAdmin"] == current_user.id and len(group["participants"]) > 1:
        if not body.newAdminId:
            raise HTTPException(status_code=400, detail="Must specify new admin before leaving")

    was_admin = group["groupAdmin"] == current_user.id
    updated = await chat_service.leave_group(group_id, current_user.id, body.newAdminId)

    # Notify remaining members (if group wasn't deleted)
    if updated and updated.get("participants"):
        # Create "member_left" system message
        sys_msg = await chat_service.save_system_message(
            group_id, "member_left", current_user.id, current_user.id,
            actor_name=current_user.name, target_name=current_user.name,
        )
        await emit_to_users(
            updated["participants"],
            "chat:group_system_message",
            {"groupId": group_id, "message": sys_msg},
        )

        # If admin changed, create "admin_changed" system message
        if was_admin and updated.get("groupAdmin"):
            new_admin_names = await _get_user_names([updated["groupAdmin"]], db)
            admin_msg = await chat_service.save_system_message(
                group_id, "admin_changed", current_user.id, updated["groupAdmin"],
                actor_name=current_user.name,
                target_name=new_admin_names.get(updated["groupAdmin"]),
            )
            await emit_to_users(
                updated["participants"],
                "chat:group_system_message",
                {"groupId": group_id, "message": admin_msg},
            )

        # Notify remaining members about the departure
        await emit_to_users(
            updated["participants"],
            "chat:group_member_left",
            {
                "groupId": group_id,
                "leftUserId": current_user.id,
                "newAdminId": updated["groupAdmin"] if was_admin else None,
            },
        )

    return {"left": True}


@router.delete("/{group_id}", response_model=DeleteResponse)
async def delete_group(
    group_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete a group (admin only)."""
    group = await chat_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["groupAdmin"] != current_user.id:
        raise HTTPException(status_code=403, detail="Only admin can delete group")

    members = group["participants"]
    deleted = await chat_service.delete_group(group_id)

    if deleted:
        # Notify all members
        for member_id in members:
            if member_id != current_user.id:
                await emit_to_user(member_id, "chat:group_member_removed", {
                    "groupId": group_id,
                })

    return {"deleted": deleted}


@router.get("/{group_id}/messages", response_model=PaginatedMessages)
async def get_group_messages(
    group_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(settings.DEFAULT_PAGE_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get paginated messages for a group."""
    group = await chat_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if current_user.id not in group["participants"]:
        raise HTTPException(status_code=403, detail="Not a member")

    joined_at = _parse_joined_at(group, current_user.id)
    result = await chat_service.get_messages(
        group_id,
        current_user.id,
        page=page,
        limit=limit,
        is_group=True,
        member_joined_at=joined_at,
    )

    # Collect all unique user IDs from messages (senders, system message actors/targets)
    user_ids: set[int] = set()
    for msg in result.get("messages", []):
        if msg.get("senderId"):
            user_ids.add(msg["senderId"])
        if msg.get("actorUserId"):
            user_ids.add(msg["actorUserId"])
        if msg.get("targetUserId"):
            user_ids.add(msg["targetUserId"])

    # Single batched PostgreSQL query for all user names
    if user_ids:
        db_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        senders_map = {}
        for u in db_result.scalars().all():
            senders_map[str(u.id)] = {
                "id": u.id,
                "firstname": u.firstname,
                "lastname": u.lastname,
                "profilePicture": u.profile_picture,
            }
        result["messageSenders"] = senders_map

    return result


@router.post("/{group_id}/read", response_model=MarkReadResponse)
async def mark_group_read(
    group_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Mark group messages as read."""
    result = await chat_service.mark_messages_read(
        group_id, current_user.id, is_group=True
    )

    # Notify only the message senders (not all participants)
    if result["markedCount"] > 0:
        for sender_id in result.get("senderIds", []):
            await emit_to_user(sender_id, "chat:group_messages_read", {
                "groupId": group_id,
                "readByUserId": current_user.id,
                "messageIds": result["messageIds"],
            })

    return {"markedCount": result["markedCount"]}
