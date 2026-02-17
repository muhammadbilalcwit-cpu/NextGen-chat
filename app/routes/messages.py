"""
Message routes — get messages, mark read, delete.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import CurrentUser, get_current_user
from app.config import settings
from app.database.postgres import get_db
from app.models.user import User
from app.schemas.message import (
    MarkReadResponse,
    MessageDeleteResponse,
    MessageInfoResponse,
    PaginatedMessages,
    UnreadCountResponse,
)
from app.services import chat_service
from app.websocket.manager import emit_to_user, emit_to_users

router = APIRouter(prefix="/chat", tags=["messages"])


@router.get("/conversations/{conversation_id}/messages", response_model=PaginatedMessages)
async def get_messages(
    conversation_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(settings.DEFAULT_PAGE_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get paginated messages for a conversation."""
    return await chat_service.get_messages(
        conversation_id,
        current_user.id,
        page=page,
        limit=limit,
    )


@router.post("/conversations/{conversation_id}/read", response_model=MarkReadResponse)
async def mark_read(
    conversation_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Mark all messages as read in a conversation."""
    result = await chat_service.mark_messages_read(
        conversation_id, current_user.id, is_group=False
    )

    # Notify sender about read status
    if result["markedCount"] > 0:
        from app.database.mongodb import get_db
        from bson import ObjectId
        db = get_db()
        conv = await db.conversations.find_one({"_id": ObjectId(conversation_id)})
        if conv:
            other_users = [p for p in conv["participants"] if p != current_user.id]
            for uid in other_users:
                await emit_to_user(uid, "chat:status_updated", {
                    "conversationId": conversation_id,
                    "status": "read",
                    "messageIds": result.get("messageIds", []),
                    "readBy": current_user.id,
                })

    return {"markedCount": result["markedCount"]}


@router.delete("/messages/{message_id}", response_model=MessageDeleteResponse)
async def delete_message(
    message_id: str,
    forEveryone: bool = Query(False),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete a message — for self or for everyone."""
    result = await chat_service.delete_message(
        message_id, current_user.id, for_everyone=forEveryone
    )

    if not result.get("deleted"):
        raise HTTPException(status_code=400, detail=result.get("error", "Could not delete"))

    # Notify participants if deleted for everyone
    if result.get("forEveryone"):
        conv_id = result.get("conversationId")
        is_group = result.get("isGroupMessage", False)

        if conv_id:
            from app.database.mongodb import get_db
            from bson import ObjectId
            db = get_db()
            conv = await db.conversations.find_one({"_id": ObjectId(conv_id)})
            if conv:
                event = "chat:group_message_deleted" if is_group else "chat:message_deleted"
                data = {"messageId": message_id}
                if is_group:
                    data["groupId"] = conv_id
                else:
                    data["conversationId"] = conv_id

                await emit_to_users(
                    conv["participants"],
                    event,
                    data,
                    exclude_user=current_user.id,
                )

    return {"deleted": True, "forEveryone": result.get("forEveryone", False)}


@router.get("/messages/{message_id}/info", response_model=MessageInfoResponse)
async def get_message_info(
    message_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get delivery/read info for a group message."""
    from app.database.mongodb import get_db as get_mongo
    from bson import ObjectId
    mongo = get_mongo()

    msg = await mongo.messages.find_one({"_id": ObjectId(message_id)})
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    # Only sender can view message info (matches NestJS behavior)
    if msg.get("senderId") != current_user.id:
        raise HTTPException(status_code=403, detail="Only the sender can view message info")

    conv = await mongo.conversations.find_one({"_id": msg["conversationId"]})
    if not conv or current_user.id not in conv.get("participants", []):
        raise HTTPException(status_code=403, detail="Not a participant")

    info = await chat_service.get_message_info(
        message_id, conv.get("participants", [])
    )

    # Collect all user IDs that need enrichment
    user_ids = set()
    for d in info["deliveredTo"]:
        user_ids.add(d["userId"])
    for r in info["readBy"]:
        user_ids.add(r["userId"])
    for uid in info["pending"]:
        user_ids.add(uid)

    # Query PostgreSQL for user details
    user_map: dict[int, dict] = {}
    if user_ids:
        result = await db.execute(
            select(User).where(User.id.in_(user_ids))
        )
        for u in result.scalars().all():
            user_map[u.id] = {
                "firstname": u.firstname,
                "lastname": u.lastname,
                "profilePicture": u.profile_picture,
            }

    # Enrich deliveredTo with user info
    enriched_delivered = []
    for d in info["deliveredTo"]:
        enriched_delivered.append({
            "userId": d["userId"],
            "timestamp": d.get("timestamp"),
            "user": user_map.get(d["userId"]),
        })

    # Enrich readBy with user info
    enriched_read = []
    for r in info["readBy"]:
        enriched_read.append({
            "userId": r["userId"],
            "timestamp": r.get("timestamp"),
            "user": user_map.get(r["userId"]),
        })

    # Enrich pending with user info
    enriched_pending = []
    for uid in info["pending"]:
        enriched_pending.append({
            "userId": uid,
            "user": user_map.get(uid),
        })

    return {
        "deliveredTo": enriched_delivered,
        "readBy": enriched_read,
        "pending": enriched_pending,
    }


@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get total unread message counts."""
    return await chat_service.get_unread_count(current_user.id)
