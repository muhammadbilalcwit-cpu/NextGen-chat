"""
Core chat business logic — conversations, messages, pending delivery.

All chat data lives in MongoDB. PostgreSQL is read-only (users, sessions).
"""
from datetime import datetime, timezone
from bson import ObjectId

from app.config import settings
from app.database.mongodb import get_db
from app.services.online_service import is_user_online
from app.services.encryption_service import (
    is_encryption_enabled,
    encrypt_content,
    decrypt_content,
)
from app.models.mongo import (
    ConversationDocument,
    GroupConversationDocument,
    MessageDocument,
    SystemMessageDocument,
)
from app.schemas.conversation import ConversationResponse
from app.schemas.group import GroupResponse
from app.schemas.message import MessageResponse


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _oid(id_str: str) -> ObjectId:
    """Convert string to ObjectId."""
    return ObjectId(id_str)


def _json_safe(obj):
    """Recursively make an object JSON-serializable (ObjectId→str, datetime→isoformat)."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(item) for item in obj]
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        # MongoDB returns naive datetimes (always UTC) — ensure UTC suffix for frontend
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.isoformat()
    return obj


def _serialize(doc: dict) -> dict:
    """Convert MongoDB doc for JSON response (ObjectId → str, datetime → isoformat)."""
    if doc is None:
        return None
    return _json_safe(doc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


ATTACHMENT_EMOJI = {
    "image": "\U0001f4f7",
    "video": "\U0001f3a5",
    "document": "\U0001f4c4",
    "voice": "\U0001f3a4",
}


def _decrypt_message(msg: dict) -> dict:
    """Decrypt a message's content if it was encrypted at rest."""
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
        # Remove encrypted fields from response
        for key in ("encryptedContent", "contentIv", "contentTag"):
            msg.pop(key, None)
    return msg


def _validate_response(data: dict, schema_cls):
    """Validate serialized data against a response schema. Returns validated dict."""
    return schema_cls.model_validate(data).model_dump(by_alias=True)


def _last_message_preview(
    content: str | None,
    attachment: dict | None,
    encrypted: bool = False,
) -> str:
    """Build lastMessage preview text for conversation.

    When ``encrypted`` is True, text content is replaced with a generic
    indicator so that no plaintext leaks into the conversation document.
    Attachment type labels (e.g. "Image", "Voice") are kept because they
    contain no user content.
    """
    if attachment:
        emoji = ATTACHMENT_EMOJI.get(attachment.get("type", ""), "")
        label = attachment.get("type", "Attachment").capitalize()
        prefix = f"{emoji} {label}"
        if content and not encrypted:
            return f"{prefix}: {content}"
        return prefix
    if encrypted:
        return "\U0001f512 Message"
    return content or ""


# ─── Conversations ─────────────────────────────────────────────────────────────

async def get_or_create_conversation(user_id: int, other_user_id: int) -> dict:
    """Get existing 1:1 conversation or create a new one."""
    db = get_db()
    participants = sorted([user_id, other_user_id])

    conv = await db.conversations.find_one({
        "participants": participants,
        "isGroup": {"$ne": True},
        "isSupportChat": {"$ne": True},
    })

    if conv:
        # Restore if soft-deleted
        if user_id in (conv.get("deletedFor") or []):
            await db.conversations.update_one(
                {"_id": conv["_id"]},
                {"$pull": {"deletedFor": user_id}},
            )
            conv["deletedFor"] = [u for u in (conv.get("deletedFor") or []) if u != user_id]
        return _validate_response(_serialize(conv), ConversationResponse)

    now = _now()
    validated = ConversationDocument(
        participants=participants,
        createdAt=now,
        updatedAt=now,
    )
    doc = validated.model_dump()
    result = await db.conversations.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _validate_response(_serialize(doc), ConversationResponse)


async def get_user_conversations(user_id: int) -> list[dict]:
    """Get all 1:1 conversations for a user (excluding groups and soft-deleted)."""
    db = get_db()
    cursor = db.conversations.find({
        "participants": user_id,
        "isGroup": {"$ne": True},
        "isSupportChat": {"$ne": True},
        "deletedFor": {"$ne": user_id},
    }).sort("lastMessageAt", -1)

    conversations = []
    async for conv in cursor:
        conversations.append(_validate_response(_serialize(conv), ConversationResponse))
    return conversations


async def soft_delete_conversation(conversation_id: str, user_id: int) -> bool:
    """Soft-delete conversation for a user (they can still receive new messages)."""
    db = get_db()
    result = await db.conversations.update_one(
        {"_id": _oid(conversation_id), "participants": user_id},
        {"$addToSet": {"deletedFor": user_id}},
    )
    return result.modified_count > 0


# ─── Messages ──────────────────────────────────────────────────────────────────

async def save_message(
    conversation_id: str,
    sender_id: int,
    recipient_id: int | None,
    content: str,
    attachment: dict | None = None,
    mentions: list[dict] | None = None,
    mentions_all: bool = False,
    is_group: bool = False,
    group_members: list[int] | None = None,
    sender_is_customer: bool | None = None,
) -> dict:
    """Save a new message to MongoDB and update conversation."""
    db = get_db()
    now = _now()

    validated = MessageDocument(
        conversationId=conversation_id,
        senderId=sender_id,
        recipientId=recipient_id,
        content=content or "",
        isGroupMessage=is_group,
        attachment=attachment,
        mentions=mentions or [],
        mentionsAll=mentions_all,
        createdAt=now,
        updatedAt=now,
    )
    msg = validated.model_dump()
    # Convert conversationId to ObjectId for MongoDB
    msg["conversationId"] = _oid(conversation_id)

    # Store sender role for support chats (avoids customer.id / user.id collision
    # when determining message ownership on the frontend)
    if sender_is_customer is not None:
        msg["senderIsCustomer"] = sender_is_customer

    # Encrypt content at rest (AES-256-GCM)
    if is_encryption_enabled() and content:
        encrypted = encrypt_content(content, conversation_id)
        msg["encryptedContent"] = encrypted["ciphertext"]
        msg["contentIv"] = encrypted["iv"]
        msg["contentTag"] = encrypted["tag"]
        msg["isEncrypted"] = True
        msg["content"] = ""  # Clear plaintext from MongoDB
    else:
        msg["isEncrypted"] = False

    result = await db.messages.insert_one(msg)
    msg["_id"] = result.inserted_id

    # Restore plaintext for the return value (used by Socket.IO emit)
    if msg.get("isEncrypted"):
        msg["content"] = content or ""
        for key in ("encryptedContent", "contentIv", "contentTag"):
            msg.pop(key, None)

    # Update conversation's last message
    # Note: lastMessage preview always uses plaintext — encryption-at-rest
    # protects the messages collection, not the conversation preview.
    preview = _last_message_preview(content, attachment)
    await db.conversations.update_one(
        {"_id": _oid(conversation_id)},
        {
            "$set": {
                "lastMessage": preview,
                "lastMessageSenderId": sender_id,
                "lastMessageAt": now,
                "updatedAt": now,
                "lastMessageSystemType": None,
                "lastMessageTargetUserId": None,
                "lastMessageActorUserId": None,
            },
            # Restore for all participants if soft-deleted
            "$pull": {"deletedFor": {"$exists": True}},
        },
    )
    # Fix: $pull with $exists doesn't work as intended, clear deletedFor
    await db.conversations.update_one(
        {"_id": _oid(conversation_id)},
        {"$set": {"deletedFor": []}},
    )

    return _validate_response(_serialize(msg), MessageResponse)


async def save_system_message(
    conversation_id: str,
    system_type: str,
    actor_id: int,
    target_id: int | None,
    actor_name: str | None = None,
    target_name: str | None = None,
) -> dict:
    """Save a system message (member added/removed/left etc.)."""
    db = get_db()
    now = _now()

    validated = SystemMessageDocument(
        conversationId=conversation_id,
        senderId=actor_id,
        systemMessageType=system_type,
        targetUserId=target_id,
        actorUserId=actor_id,
        actorName=actor_name,
        targetName=target_name,
        createdAt=now,
        updatedAt=now,
    )
    msg = validated.model_dump()
    msg["conversationId"] = _oid(conversation_id)

    result = await db.messages.insert_one(msg)
    msg["_id"] = result.inserted_id

    # Update conversation
    await db.conversations.update_one(
        {"_id": _oid(conversation_id)},
        {
            "$set": {
                "lastMessage": "",
                "lastMessageSenderId": actor_id,
                "lastMessageAt": now,
                "lastMessageSystemType": system_type,
                "lastMessageTargetUserId": target_id,
                "lastMessageActorUserId": actor_id,
                "lastMessageActorName": actor_name,
                "lastMessageTargetName": target_name,
                "updatedAt": now,
            }
        },
    )

    return _validate_response(_serialize(msg), MessageResponse)


async def get_messages(
    conversation_id: str,
    user_id: int,
    page: int = 1,
    limit: int = settings.DEFAULT_PAGE_LIMIT,
    is_group: bool = False,
    member_joined_at: datetime | None = None,
) -> dict:
    """Get paginated messages for a conversation."""
    db = get_db()
    skip = (page - 1) * limit

    query = {
        "conversationId": _oid(conversation_id),
        "deletedFor": {"$ne": user_id},
    }

    # For groups, only show messages after user joined
    if is_group and member_joined_at:
        query["createdAt"] = {"$gte": member_joined_at}

    total = await db.messages.count_documents(query)

    cursor = db.messages.find(query).sort("createdAt", -1).skip(skip).limit(limit)
    messages = []
    async for msg in cursor:
        serialized = _decrypt_message(_serialize(msg))
        messages.append(_validate_response(serialized, MessageResponse))

    messages.reverse()  # Oldest first

    return {
        "messages": messages,
        "total": total,
        "hasMore": (skip + limit) < total,
    }


async def mark_messages_delivered(
    conversation_id: str,
    recipient_id: int,
    is_group: bool = False,
    recipient_is_customer: bool | None = None,
) -> list[str]:
    """Mark all sent messages in conversation as delivered for recipient.

    For support chats, pass ``recipient_is_customer`` so the query uses
    ``senderIsCustomer`` instead of ``recipientId``, avoiding the
    customer.id / user.id collision.
    """
    db = get_db()
    now = _now()

    if is_group:
        # Group: add to deliveredTo array
        query = {
            "conversationId": _oid(conversation_id),
            "senderId": {"$ne": recipient_id},
            "isGroupMessage": True,
            "deliveredTo.userId": {"$ne": recipient_id},
        }
        cursor = db.messages.find(query, {"_id": 1})
        message_ids = [str(msg["_id"]) async for msg in cursor]

        if message_ids:
            await db.messages.update_many(
                {"_id": {"$in": [_oid(mid) for mid in message_ids]}},
                {"$push": {"deliveredTo": {"userId": recipient_id, "timestamp": now}}},
            )
        return message_ids
    else:
        # 1:1: set status to delivered
        if recipient_is_customer is not None:
            # Support chat: mark messages from the OTHER role as delivered.
            # If recipient is the customer, mark agent messages (senderIsCustomer=False).
            # If recipient is the agent, mark customer messages (senderIsCustomer=True).
            query = {
                "conversationId": _oid(conversation_id),
                "senderIsCustomer": not recipient_is_customer,
                "status": "sent",
            }
        else:
            # Regular 1:1 chat
            query = {
                "conversationId": _oid(conversation_id),
                "recipientId": recipient_id,
                "status": "sent",
            }
        cursor = db.messages.find(query, {"_id": 1})
        message_ids = [str(msg["_id"]) async for msg in cursor]

        if message_ids:
            await db.messages.update_many(
                {"_id": {"$in": [_oid(mid) for mid in message_ids]}},
                {"$set": {"status": "delivered", "deliveredAt": now}},
            )
        return message_ids


async def mark_messages_read(
    conversation_id: str,
    user_id: int,
    is_group: bool = False,
    reader_is_customer: bool | None = None,
) -> dict:
    """Mark messages as read in a conversation.

    For support chats, pass ``reader_is_customer`` so the query uses the
    ``senderIsCustomer`` field instead of ``recipientId``.  This avoids the
    customer.id / user.id collision where both IDs can be the same integer.
    """
    db = get_db()
    now = _now()

    if is_group:
        # Group: add to readBy
        query = {
            "conversationId": _oid(conversation_id),
            "senderId": {"$ne": user_id},
            "isGroupMessage": True,
            "readBy.userId": {"$ne": user_id},
        }
        cursor = db.messages.find(query, {"_id": 1, "senderId": 1})
        message_ids = []
        sender_ids = set()
        async for msg in cursor:
            message_ids.append(str(msg["_id"]))
            sender_ids.add(msg["senderId"])

        if message_ids:
            await db.messages.update_many(
                {"_id": {"$in": [_oid(mid) for mid in message_ids]}},
                {"$push": {"readBy": {"userId": user_id, "timestamp": now}}},
            )
        return {"markedCount": len(message_ids), "messageIds": message_ids, "senderIds": list(sender_ids)}
    else:
        # 1:1: find IDs + senders first, then update
        if reader_is_customer is not None:
            # Support chat: use senderIsCustomer to filter by role, avoiding
            # the customer.id == user.id collision that breaks recipientId matching.
            query = {
                "conversationId": _oid(conversation_id),
                "senderIsCustomer": not reader_is_customer,
                "status": {"$in": ["sent", "delivered"]},
            }
        else:
            # Regular 1:1 chat: use recipientId (no ID collision between employees)
            query = {
                "conversationId": _oid(conversation_id),
                "recipientId": user_id,
                "status": {"$in": ["sent", "delivered"]},
            }
        cursor = db.messages.find(query, {"_id": 1, "senderId": 1})
        message_ids = []
        sender_ids = set()
        async for msg in cursor:
            message_ids.append(str(msg["_id"]))
            sender_ids.add(msg["senderId"])

        if message_ids:
            await db.messages.update_many(
                {"_id": {"$in": [_oid(mid) for mid in message_ids]}},
                {"$set": {"status": "read", "readAt": now}},
            )
        return {
            "markedCount": len(message_ids),
            "messageIds": message_ids,
            "senderIds": list(sender_ids),
        }


async def delete_message(
    message_id: str,
    user_id: int,
    for_everyone: bool = False,
) -> dict:
    """Delete a message — for self or for everyone."""
    db = get_db()

    msg = await db.messages.find_one({"_id": _oid(message_id)})
    if not msg:
        return {"deleted": False}

    if for_everyone:
        if msg["senderId"] != user_id:
            return {"deleted": False, "error": "Can only delete own messages for everyone"}

        # WhatsApp-style: block "delete for everyone" after time window
        from app.config import settings
        created_at = msg.get("createdAt")
        if created_at:
            if not isinstance(created_at, datetime):
                created_at = datetime.fromisoformat(str(created_at))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            elapsed_hours = (_now() - created_at).total_seconds() / 3600
            if elapsed_hours > settings.DELETE_FOR_EVERYONE_HOURS:
                return {
                    "deleted": False,
                    "error": f"Cannot delete for everyone after {settings.DELETE_FOR_EVERYONE_HOURS} hours",
                }

        await db.messages.update_one(
            {"_id": _oid(message_id)},
            {
                "$set": {
                    "isDeleted": True,
                    "content": "",
                    "attachment": None,
                    "deletedAt": _now(),
                }
            },
        )
        return {
            "deleted": True,
            "forEveryone": True,
            "conversationId": str(msg["conversationId"]),
            "isGroupMessage": msg.get("isGroupMessage", False),
        }
    else:
        await db.messages.update_one(
            {"_id": _oid(message_id)},
            {"$addToSet": {"deletedFor": user_id}},
        )
        return {"deleted": True, "forEveryone": False}


async def get_message_info(message_id: str, group_members: list[int]) -> dict:
    """Get delivery/read info for a group message."""
    db = get_db()
    msg = await db.messages.find_one({"_id": _oid(message_id)})
    if not msg:
        return {"deliveredTo": [], "readBy": [], "pending": []}

    delivered_ids = {d["userId"] for d in (msg.get("deliveredTo") or [])}
    read_ids = {r["userId"] for r in (msg.get("readBy") or [])}
    all_ids = set(group_members) - {msg["senderId"]}
    pending_ids = all_ids - delivered_ids - read_ids

    return {
        "deliveredTo": _json_safe(msg.get("deliveredTo", [])),
        "readBy": _json_safe(msg.get("readBy", [])),
        "pending": list(pending_ids),
    }


# ─── Pending Delivery (offline → online) ──────────────────────────────────────

async def get_pending_messages(user_id: int, company_id: int) -> list[dict]:
    """
    Get messages that need to be delivered to a user who just came online.
    For 1:1: status='sent' and recipientId=user_id
    For groups: user not in deliveredTo for group conversations they're in
    """
    db = get_db()
    pending = []

    # 1:1 pending
    cursor = db.messages.find({
        "recipientId": user_id,
        "status": "sent",
        "isGroupMessage": {"$ne": True},
        # Exclude own support-chat messages where customer.id == user.id
        "senderIsCustomer": {"$ne": False},
    }).sort("createdAt", 1)
    async for msg in cursor:
        serialized = _decrypt_message(_serialize(msg))
        pending.append(_validate_response(serialized, MessageResponse))

    # Group pending: find groups user is in, then undelivered messages
    group_cursor = db.conversations.find({
        "participants": user_id,
        "isGroup": True,
    })
    async for group in group_cursor:
        joined_at = (group.get("memberJoinedAt") or {}).get(str(user_id))
        msg_query = {
            "conversationId": group["_id"],
            "isGroupMessage": True,
            "senderId": {"$ne": user_id},
            "deliveredTo.userId": {"$ne": user_id},
        }
        if joined_at:
            msg_query["createdAt"] = {"$gte": joined_at}

        async for msg in db.messages.find(msg_query).sort("createdAt", 1):
            serialized = _decrypt_message(_serialize(msg))
            pending.append(_validate_response(serialized, MessageResponse))

    return pending


async def deliver_pending_messages(user_id: int, company_id: int) -> dict:
    """
    Mark pending messages as delivered when user comes online.
    Returns delivery info for sender notifications:
      { "direct": [ {senderId, conversationId, messageIds} ], "groups": [ {senderId, groupId, messageId} ] }
    """
    db = get_db()
    now = _now()

    # ── 1:1: find pending messages, group by sender+conversation, then update ──
    direct_notifications = []
    dm_query = {
        "recipientId": user_id,
        "status": "sent",
        "isGroupMessage": {"$ne": True},
        # Exclude own messages in support chats where customer.id == user.id.
        # senderIsCustomer=False means the agent sent it — skip those.
        # For regular chat the field doesn't exist, so $ne:False still matches.
        "senderIsCustomer": {"$ne": False},
    }
    cursor = db.messages.find(dm_query, {"_id": 1, "senderId": 1, "conversationId": 1})
    # Group by (senderId, conversationId)
    by_sender_conv: dict[tuple[int, str], list[str]] = {}
    async for msg in cursor:
        key = (msg["senderId"], str(msg["conversationId"]))
        by_sender_conv.setdefault(key, []).append(str(msg["_id"]))

    if by_sender_conv:
        all_ids = [_oid(mid) for ids in by_sender_conv.values() for mid in ids]
        await db.messages.update_many(
            {"_id": {"$in": all_ids}},
            {"$set": {"status": "delivered", "deliveredAt": now}},
        )
        for (sender_id, conv_id), msg_ids in by_sender_conv.items():
            direct_notifications.append({
                "senderId": sender_id,
                "conversationId": conv_id,
                "messageIds": msg_ids,
            })

    # ── Groups: add to deliveredTo ──
    group_notifications = []
    group_cursor = db.conversations.find({
        "participants": user_id,
        "isGroup": True,
    })
    async for group in group_cursor:
        joined_at = (group.get("memberJoinedAt") or {}).get(str(user_id))
        msg_query = {
            "conversationId": group["_id"],
            "isGroupMessage": True,
            "senderId": {"$ne": user_id},
            "deliveredTo.userId": {"$ne": user_id},
        }
        if joined_at:
            msg_query["createdAt"] = {"$gte": joined_at}

        group_msgs = db.messages.find(msg_query, {"_id": 1, "senderId": 1})
        group_msg_list = []
        async for msg in group_msgs:
            group_msg_list.append({"messageId": str(msg["_id"]), "senderId": msg["senderId"]})

        if group_msg_list:
            all_group_ids = [_oid(m["messageId"]) for m in group_msg_list]
            await db.messages.update_many(
                {"_id": {"$in": all_group_ids}},
                {"$push": {"deliveredTo": {"userId": user_id, "timestamp": now}}},
            )
            for m in group_msg_list:
                group_notifications.append({
                    "senderId": m["senderId"],
                    "groupId": str(group["_id"]),
                    "messageId": m["messageId"],
                })

    return {"direct": direct_notifications, "groups": group_notifications}


# ─── Unread Count ──────────────────────────────────────────────────────────────

async def get_unread_count(user_id: int, company_id: int | None = None) -> dict:
    """Get total unread message counts for a user."""
    db = get_db()
    direct = 0
    groups = 0

    # Find ALL support conversations where this user_id appears in participants.
    # This catches both real support conversations (user is agent) AND collisions
    # (customer.id matches user.id). We exclude ALL of these from the direct count.
    all_support_conv_ids = []
    async for conv in db.conversations.find(
        {"participants": user_id, "isSupportChat": True}, {"_id": 1}
    ):
        all_support_conv_ids.append(conv["_id"])

    # For the support unread badge, only count conversations for this user's company.
    # This prevents showing another company's support unreads due to ID collision.
    if company_id is not None:
        own_support_conv_ids = []
        async for conv in db.conversations.find(
            {"participants": user_id, "isSupportChat": True, "supportMetadata.companyId": company_id},
            {"_id": 1},
        ):
            own_support_conv_ids.append(conv["_id"])
    else:
        own_support_conv_ids = all_support_conv_ids

    # 1:1 unread: messages where status != 'read' and recipientId == user_id
    # Exclude ALL support conversations (both own-company and collision ones)
    direct_filter: dict = {
        "recipientId": user_id,
        "senderId": {"$ne": user_id},
        "status": {"$ne": "read"},
        "isGroupMessage": {"$ne": True},
        "deletedFor": {"$ne": user_id},
    }
    if all_support_conv_ids:
        direct_filter["conversationId"] = {"$nin": all_support_conv_ids}

    direct = await db.messages.count_documents(direct_filter)

    # Group unread: messages where user not in readBy
    group_cursor = db.conversations.find({
        "participants": user_id,
        "isGroup": True,
        "deletedFor": {"$ne": user_id},
    })
    async for group in group_cursor:
        joined_at = (group.get("memberJoinedAt") or {}).get(str(user_id))
        query = {
            "conversationId": group["_id"],
            "isGroupMessage": True,
            "senderId": {"$ne": user_id},
            "readBy.userId": {"$ne": user_id},
            "deletedFor": {"$ne": user_id},
        }
        if joined_at:
            query["createdAt"] = {"$gte": joined_at}
        groups += await db.messages.count_documents(query)

    # Support chat unread: only count from own-company support conversations
    support = 0
    if own_support_conv_ids:
        support = await db.messages.count_documents({
            "conversationId": {"$in": own_support_conv_ids},
            "senderIsCustomer": True,
            "status": {"$ne": "read"},
        })

    return {
        "count": direct + groups + support,
        "direct": direct,
        "groups": groups,
        "support": support,
    }


async def get_conversation_unread_count(
    conversation_id: str,
    user_id: int,
    is_group: bool = False,
    member_joined_at: datetime | None = None,
    is_support: bool = False,
) -> int:
    """Get unread count for a specific conversation."""
    db = get_db()

    if is_group:
        query = {
            "conversationId": _oid(conversation_id),
            "isGroupMessage": True,
            "senderId": {"$ne": user_id},
            "readBy.userId": {"$ne": user_id},
            "deletedFor": {"$ne": user_id},
        }
        if member_joined_at:
            query["createdAt"] = {"$gte": member_joined_at}
        return await db.messages.count_documents(query)
    else:
        if is_support:
            # Support chat: use senderIsCustomer to avoid customer.id / user.id collision
            return await db.messages.count_documents({
                "conversationId": _oid(conversation_id),
                "senderIsCustomer": True,
                "status": {"$ne": "read"},
                "deletedFor": {"$ne": user_id},
            })
        return await db.messages.count_documents({
            "conversationId": _oid(conversation_id),
            "recipientId": user_id,
            "senderId": {"$ne": user_id},
            "status": {"$ne": "read"},
            "deletedFor": {"$ne": user_id},
        })


# ─── Groups ───────────────────────────────────────────────────────────────────

async def create_group(
    name: str,
    admin_id: int,
    member_ids: list[int],
    company_id: int,
    avatar: str | None = None,
) -> dict:
    """Create a new group conversation."""
    db = get_db()
    now = _now()

    all_members = list(set([admin_id] + member_ids))
    member_joined_at = {str(uid): now for uid in all_members}

    validated = GroupConversationDocument(
        participants=all_members,
        groupName=name,
        groupAvatar=avatar,
        groupAdmin=admin_id,
        companyId=company_id,
        lastMessageAt=now,
        memberJoinedAt=member_joined_at,
        lastMessageSystemType="group_created",
        lastMessageTargetUserId=admin_id,
        lastMessageActorUserId=admin_id,
        createdAt=now,
        updatedAt=now,
    )
    doc = validated.model_dump()
    result = await db.conversations.insert_one(doc)
    doc["_id"] = result.inserted_id

    return _validate_response(_serialize(doc), GroupResponse)


async def get_group(group_id: str) -> dict | None:
    """Get a group conversation by ID."""
    db = get_db()
    group = await db.conversations.find_one({
        "_id": _oid(group_id),
        "isGroup": True,
    })
    if not group:
        return None
    return _validate_response(_serialize(group), GroupResponse)


async def get_user_groups(user_id: int) -> list[dict]:
    """Get all groups a user is part of."""
    db = get_db()
    cursor = db.conversations.find({
        "participants": user_id,
        "isGroup": True,
        "deletedFor": {"$ne": user_id},
    }).sort("lastMessageAt", -1)

    groups = []
    async for group in cursor:
        groups.append(_validate_response(_serialize(group), GroupResponse))
    return groups


async def update_group(group_id: str, updates: dict) -> dict | None:
    """Update group name or avatar."""
    db = get_db()
    allowed = {}
    if "name" in updates:
        allowed["groupName"] = updates["name"]
    if "avatar" in updates:
        allowed["groupAvatar"] = updates["avatar"]

    if not allowed:
        return await get_group(group_id)

    allowed["updatedAt"] = _now()
    await db.conversations.update_one(
        {"_id": _oid(group_id), "isGroup": True},
        {"$set": allowed},
    )
    return await get_group(group_id)


async def add_group_members(group_id: str, member_ids: list[int], actor_id: int) -> dict | None:
    """Add members to a group."""
    db = get_db()
    now = _now()

    # Add to participants and set joinedAt
    member_joined_updates = {f"memberJoinedAt.{uid}": now for uid in member_ids}
    await db.conversations.update_one(
        {"_id": _oid(group_id), "isGroup": True},
        {
            "$addToSet": {"participants": {"$each": member_ids}},
            "$set": {**member_joined_updates, "updatedAt": now},
        },
    )

    return await get_group(group_id)


async def remove_group_member(group_id: str, member_id: int, actor_id: int) -> dict | None:
    """Remove a member from a group."""
    db = get_db()
    now = _now()

    await db.conversations.update_one(
        {"_id": _oid(group_id), "isGroup": True},
        {
            "$pull": {"participants": member_id},
            "$unset": {f"memberJoinedAt.{member_id}": ""},
            "$set": {"updatedAt": now},
        },
    )

    return await get_group(group_id)


async def leave_group(group_id: str, user_id: int, new_admin_id: int | None = None) -> dict | None:
    """Member leaves a group. If admin, must specify new admin."""
    db = get_db()
    now = _now()
    group = await get_group(group_id)
    if not group:
        return None

    remaining = [p for p in group["participants"] if p != user_id]

    # If no participants left, delete the group
    if not remaining:
        await db.conversations.delete_one({"_id": _oid(group_id)})
        await db.messages.delete_many({"conversationId": _oid(group_id)})
        return None

    updates = {
        "updatedAt": now,
    }

    # If admin is leaving, transfer admin role
    if group["groupAdmin"] == user_id:
        if not new_admin_id:
            new_admin_id = remaining[0]
        if new_admin_id:
            updates["groupAdmin"] = new_admin_id

    await db.conversations.update_one(
        {"_id": _oid(group_id), "isGroup": True},
        {
            "$pull": {"participants": user_id},
            "$unset": {f"memberJoinedAt.{user_id}": ""},
            "$set": updates,
        },
    )

    return await get_group(group_id)


async def delete_group(group_id: str) -> bool:
    """Delete a group and all its messages (admin only)."""
    db = get_db()
    result = await db.conversations.delete_one({
        "_id": _oid(group_id),
        "isGroup": True,
    })
    if result.deleted_count:
        await db.messages.delete_many({"conversationId": _oid(group_id)})
        return True
    return False
