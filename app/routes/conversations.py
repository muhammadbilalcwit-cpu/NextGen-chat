"""
Conversation routes — list, create, delete conversations.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import CurrentUser, get_current_user
from app.database.postgres import get_db
from app.models.user import User
from app.schemas.conversation import ConversationResponse
from app.schemas.common import DeleteResponse
from app.services import chat_service
from app.services.online_service import is_user_online
from app.services.permission_service import get_user_role_ids, get_merged_permission

router = APIRouter(prefix="/chat", tags=["conversations"])


async def _enrich_conversation(conv: dict, current_user: CurrentUser, db: AsyncSession) -> dict | None:
    """Add otherUser info and unreadCount to a 1:1 conversation. Returns None if other user not found."""
    other_user_ids = [p for p in conv["participants"] if p != current_user.id]
    if not other_user_ids:
        return None

    result = await db.execute(
        select(User).where(User.id == other_user_ids[0])
    )
    other = result.scalar_one_or_none()
    if not other:
        return None  # Skip conversations with non-existent users

    conv["otherUser"] = {
        "id": other.id,
        "firstname": other.firstname,
        "lastname": other.lastname,
        "email": other.email,
        "profilePicture": other.profile_picture,
        "isOnline": await is_user_online(other.id, other.company_id),
    }

    conv["unreadCount"] = await chat_service.get_conversation_unread_count(
        conv["_id"], current_user.id, is_group=False
    )
    return conv


@router.get("/conversations", response_model=list[ConversationResponse])
async def get_conversations(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all 1:1 conversations for the current user."""
    conversations = await chat_service.get_user_conversations(current_user.id)

    enriched = []
    for conv in conversations:
        enriched_conv = await _enrich_conversation(conv, current_user, db)
        if enriched_conv:  # Skip conversations where other user wasn't found
            enriched.append(enriched_conv)

    return enriched


@router.post("/conversations/{user_id}", response_model=ConversationResponse)
async def get_or_create_conversation(
    user_id: int,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get or create a 1:1 conversation with another user."""
    # Get permission from MongoDB to determine cross-company access
    my_role_ids = await get_user_role_ids(current_user.id, current_user.role_id, db)
    permission = await get_merged_permission(my_role_ids)
    all_companies = permission["allCompanies"] if permission else False

    # Verify the other user exists (and is in same company unless cross-company access)
    query = select(User).where(
        User.id == user_id,
        User.is_active == True,  # noqa: E712
        User.deleted_at.is_(None),
    )
    if not all_companies:
        query = query.where(User.company_id == current_user.company_id)

    result = await db.execute(query)
    other_user = result.scalar_one_or_none()
    if not other_user:
        raise HTTPException(status_code=404, detail="User not found or not in your company")

    conv = await chat_service.get_or_create_conversation(current_user.id, user_id)

    conv["otherUser"] = {
        "id": other_user.id,
        "firstname": other_user.firstname,
        "lastname": other_user.lastname,
        "email": other_user.email,
        "profilePicture": other_user.profile_picture,
        "isOnline": await is_user_online(other_user.id, other_user.company_id),
    }

    return conv


@router.delete("/conversations/{conversation_id}", response_model=DeleteResponse)
async def delete_conversation(
    conversation_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Soft-delete a conversation (hide for current user)."""
    deleted = await chat_service.soft_delete_conversation(conversation_id, current_user.id)
    return {"deleted": deleted}
