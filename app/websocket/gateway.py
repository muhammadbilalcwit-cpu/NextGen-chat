"""
Socket.IO event handlers — matches NestJS gateway event names exactly.

Events:
  Client → Server: chat:send, chat:typing, chat:read,
                    chat:group_send, chat:group_typing, chat:group_read
  Server → Client: chat:receive, chat:typing, chat:message_confirmed,
                    chat:status_updated, chat:message_deleted,
                    chat:group_message, chat:group_typing, etc.
"""
import socketio
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.postgres import async_session
from app.auth.jwt import validate_ws_token
from app.websocket.manager import (
    init_manager,
    connect_user,
    disconnect_user,
    get_user_info,
    emit_to_user,
    emit_to_users,
    is_user_connected_locally,
    get_all_connected_users,
)
from app.services import chat_service
from app.services.online_service import is_user_online
from app.services.rabbitmq_service import publish_direct_message, publish_group_message
from app.services.chat_service import deliver_pending_messages


def create_sio_server() -> socketio.AsyncServer:
    """Create and configure Socket.IO async server."""
    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        logger=False,
        engineio_logger=False,
    )

    init_manager(sio)
    _register_events(sio)
    return sio


def _register_events(sio: socketio.AsyncServer):
    """Register all Socket.IO event handlers."""

    @sio.event
    async def connect(sid, environ, auth):
        """Authenticate WebSocket connection via JWT token."""
        token = None

        # Try auth dict first (Socket.IO v3+ sends auth data)
        if auth and isinstance(auth, dict):
            token = auth.get("token")

        # Fallback: check query params
        if not token:
            query = environ.get("QUERY_STRING", "")
            for param in query.split("&"):
                if param.startswith("token="):
                    token = param.split("=", 1)[1]
                    break

        # Fallback: check cookies
        if not token:
            cookies = environ.get("HTTP_COOKIE", "")
            from app.config import settings
            for cookie in cookies.split(";"):
                cookie = cookie.strip()
                if cookie.startswith(f"{settings.COOKIE_NAME}="):
                    token = cookie.split("=", 1)[1]
                    break

        if not token:
            raise socketio.exceptions.ConnectionRefusedError("No token provided")

        async with async_session() as db:
            user = await validate_ws_token(token, db)

        if not user:
            raise socketio.exceptions.ConnectionRefusedError("Invalid token")

        # Store user info in session
        await sio.save_session(sid, {
            "user_id": user.id,
            "company_id": user.company_id,
            "name": user.name,
        })

        # Register connection
        await connect_user(user.id, user.company_id, sid)

        # Deliver pending messages and notify senders (double ticks)
        delivery = await deliver_pending_messages(user.id, user.company_id)
        for item in delivery.get("direct", []):
            await emit_to_user(item["senderId"], "chat:status_updated", {
                "conversationId": item["conversationId"],
                "status": "delivered",
                "messageIds": item["messageIds"],
            })
        for item in delivery.get("groups", []):
            await emit_to_user(item["senderId"], "chat:group_message_delivered", {
                "groupId": item["groupId"],
                "messageId": item["messageId"],
                "deliveredToUserId": user.id,
            })

        # Notify all connected users that this user is online
        # (covers cross-company super_admins who can see users from other companies)
        all_connected = get_all_connected_users()
        await emit_to_users(
            list(all_connected.keys()),
            "user:online",
            {"userId": user.id},
            exclude_user=user.id,
        )

        print(f"WS: User {user.id} ({user.name}) connected [sid={sid}]")

    @sio.event
    async def disconnect(sid):
        """Handle socket disconnection."""
        result = await disconnect_user(sid)
        if not result:
            return

        user_id, company_id = result

        # Only emit offline if user has no more connections
        if not is_user_connected_locally(user_id):
            all_connected = get_all_connected_users()
            await emit_to_users(
                list(all_connected.keys()),
                "user:offline",
                {"userId": user_id},
            )

        print(f"WS: User {user_id} disconnected [sid={sid}]")

    # ─── Direct Messages ──────────────────────────────────────────────────

    @sio.on("chat:send")
    async def handle_send(sid, data):
        """Handle sending a direct message — publishes to RabbitMQ."""
        session = await sio.get_session(sid)
        if not session:
            return {"success": False, "error": "Not authenticated"}

        temp_id = data.get("tempId")
        recipient_id = data.get("recipientId")
        content = data.get("content", "")
        attachment = data.get("attachment")
        mentions = data.get("mentions")

        if not recipient_id:
            return {"success": False, "error": "recipientId required"}
        if not content and not attachment:
            return {"success": False, "error": "Content or attachment required"}

        # Publish to RabbitMQ for async processing
        await publish_direct_message({
            "tempId": temp_id,
            "senderId": session["user_id"],
            "recipientId": int(recipient_id),
            "content": content,
            "senderCompanyId": session["company_id"],
            "attachment": attachment,
            "mentions": mentions,
        })

        return {"success": True}

    @sio.on("chat:typing")
    async def handle_typing(sid, data):
        """Forward typing indicator to recipient."""
        session = await sio.get_session(sid)
        if not session:
            return

        recipient_id = data.get("recipientId")
        is_typing = data.get("isTyping", False)

        if recipient_id:
            await emit_to_user(int(recipient_id), "chat:typing", {
                "senderId": session["user_id"],
                "isTyping": is_typing,
            })

    @sio.on("chat:read")
    async def handle_read(sid, data):
        """Mark messages as read in a 1:1 conversation."""
        session = await sio.get_session(sid)
        if not session:
            return {"success": False}

        conversation_id = data.get("conversationId")
        if not conversation_id:
            return {"success": False}

        result = await chat_service.mark_messages_read(
            conversation_id, session["user_id"], is_group=False
        )

        # Notify each sender that their messages were read
        if result["markedCount"] > 0:
            for sender_id in result.get("senderIds", []):
                await emit_to_user(sender_id, "chat:status_updated", {
                    "conversationId": conversation_id,
                    "status": "read",
                    "messageIds": result["messageIds"],
                    "readBy": session["user_id"],
                })

        return {"success": True}

    # ─── Group Messages ───────────────────────────────────────────────────

    @sio.on("chat:group_send")
    async def handle_group_send(sid, data):
        """Handle sending a group message — publishes to RabbitMQ."""
        session = await sio.get_session(sid)
        if not session:
            return {"success": False, "error": "Not authenticated"}

        temp_id = data.get("tempId")
        group_id = data.get("groupId")
        content = data.get("content", "")
        attachment = data.get("attachment")
        mentions = data.get("mentions")
        mentions_all = data.get("mentionsAll", False)

        if not group_id:
            return {"success": False, "error": "groupId required"}
        if not content and not attachment:
            return {"success": False, "error": "Content or attachment required"}

        await publish_group_message({
            "tempId": temp_id,
            "senderId": session["user_id"],
            "groupId": group_id,
            "content": content,
            "senderCompanyId": session["company_id"],
            "attachment": attachment,
            "mentions": mentions,
            "mentionsAll": mentions_all,
        })

        return {"success": True}

    @sio.on("chat:group_typing")
    async def handle_group_typing(sid, data):
        """Forward typing indicator to group members."""
        session = await sio.get_session(sid)
        if not session:
            return

        group_id = data.get("groupId")
        is_typing = data.get("isTyping", False)

        if not group_id:
            return

        group = await chat_service.get_group(group_id)
        if not group:
            return

        # Emit to all group members except sender
        await emit_to_users(
            group["participants"],
            "chat:group_typing",
            {
                "groupId": group_id,
                "senderId": session["user_id"],
                "isTyping": is_typing,
            },
            exclude_user=session["user_id"],
        )

    @sio.on("chat:group_read")
    async def handle_group_read(sid, data):
        """Mark group messages as read."""
        session = await sio.get_session(sid)
        if not session:
            return {"success": False}

        group_id = data.get("groupId")
        if not group_id:
            return {"success": False}

        result = await chat_service.mark_messages_read(
            group_id, session["user_id"], is_group=True
        )

        # Notify only the message senders (not all participants)
        if result["markedCount"] > 0:
            for sender_id in result.get("senderIds", []):
                await emit_to_user(sender_id, "chat:group_messages_read", {
                    "groupId": group_id,
                    "readByUserId": session["user_id"],
                    "messageIds": result["messageIds"],
                })

        return {"success": True}


# ─── RabbitMQ Consumer Handlers ───────────────────────────────────────────────
# These are called by RabbitMQ when messages are consumed from the queue.

async def process_direct_message(payload: dict):
    """
    Process a direct message from RabbitMQ queue.
    1. Save to MongoDB
    2. Send confirmation to sender
    3. Deliver to recipient if online
    """
    temp_id = payload["tempId"]
    sender_id = payload["senderId"]
    recipient_id = payload["recipientId"]
    content = payload.get("content", "")
    company_id = payload["senderCompanyId"]
    attachment = payload.get("attachment")
    mentions = payload.get("mentions")

    # Get or create conversation
    conv = await chat_service.get_or_create_conversation(sender_id, recipient_id)

    # Save message
    message = await chat_service.save_message(
        conversation_id=conv["_id"],
        sender_id=sender_id,
        recipient_id=recipient_id,
        content=content,
        attachment=attachment,
        mentions=mentions,
    )

    # Confirm to sender (tempId → real messageId)
    await emit_to_user(sender_id, "chat:message_confirmed", {
        "tempId": temp_id,
        "messageId": message["_id"],
        "conversationId": conv["_id"],
    })

    # Enrich conversation with otherUser info for each side
    from sqlalchemy import select
    from app.models.user import User
    async with async_session() as db:
        result = await db.execute(
            select(User).where(User.id.in_([sender_id, recipient_id]))
        )
        user_map = {u.id: u for u in result.scalars().all()}

    sender_user = user_map.get(sender_id)
    recipient_user = user_map.get(recipient_id)

    def _user_info(u):
        if not u:
            return None
        return {
            "id": u.id,
            "firstname": u.firstname,
            "lastname": u.lastname,
            "email": u.email,
            "profilePicture": u.profile_picture,
            "isOnline": True,
        }

    # Sender sees recipient as otherUser
    sender_conv = {**conv, "otherUser": _user_info(recipient_user)}
    # Recipient sees sender as otherUser
    recipient_conv = {**conv, "otherUser": _user_info(sender_user)}

    # Deliver to sender's other sessions too
    await emit_to_user(sender_id, "chat:receive", {
        "message": message,
        "conversation": sender_conv,
    })

    recipient_company_id = recipient_user.company_id if recipient_user else None
    recipient_online = await is_user_online(recipient_id, recipient_company_id) if recipient_company_id else False
    if recipient_online:
        # Deliver and mark as delivered
        await emit_to_user(recipient_id, "chat:receive", {
            "message": message,
            "conversation": recipient_conv,
        })
        delivered_ids = await chat_service.mark_messages_delivered(
            conv["_id"], recipient_id, is_group=False
        )
        if delivered_ids:
            await emit_to_user(sender_id, "chat:status_updated", {
                "conversationId": conv["_id"],
                "status": "delivered",
                "messageIds": delivered_ids,
            })


async def process_group_message(payload: dict):
    """
    Process a group message from RabbitMQ queue.
    1. Save to MongoDB
    2. Send confirmation to sender
    3. Deliver to online group members
    """
    temp_id = payload["tempId"]
    sender_id = payload["senderId"]
    group_id = payload["groupId"]
    content = payload.get("content", "")
    company_id = payload["senderCompanyId"]
    attachment = payload.get("attachment")
    mentions = payload.get("mentions")
    mentions_all = payload.get("mentionsAll", False)

    group = await chat_service.get_group(group_id)
    if not group:
        print(f"RabbitMQ: Group {group_id} not found, skipping message")
        return

    # Save message
    message = await chat_service.save_message(
        conversation_id=group_id,
        sender_id=sender_id,
        recipient_id=None,
        content=content,
        attachment=attachment,
        mentions=mentions,
        mentions_all=mentions_all,
        is_group=True,
        group_members=group["participants"],
    )

    # Confirm to sender
    await emit_to_user(sender_id, "chat:message_confirmed", {
        "tempId": temp_id,
        "messageId": message["_id"],
        "conversationId": group_id,
    })

    # Batch lookup all member company_ids from PostgreSQL
    from sqlalchemy import select
    from app.models.user import User
    other_member_ids = [mid for mid in group["participants"] if mid != sender_id]
    member_company_map: dict[int, int] = {}
    if other_member_ids:
        async with async_session() as db:
            result = await db.execute(
                select(User.id, User.company_id).where(User.id.in_(other_member_ids))
            )
            for row in result.all():
                member_company_map[row[0]] = row[1]

    # Deliver to all group members
    for member_id in group["participants"]:
        if member_id == sender_id:
            # Send to sender's other sessions
            await emit_to_user(sender_id, "chat:group_message", {
                "message": message,
                "conversation": group,
            })
            continue

        member_cid = member_company_map.get(member_id)
        member_online = await is_user_online(member_id, member_cid) if member_cid else False
        if member_online:
            await emit_to_user(member_id, "chat:group_message", {
                "message": message,
                "conversation": group,
            })
            # Mark delivered for this member
            await chat_service.mark_messages_delivered(
                group_id, member_id, is_group=True
            )
            await emit_to_user(sender_id, "chat:group_message_delivered", {
                "groupId": group_id,
                "messageId": message["_id"],
                "deliveredToUserId": member_id,
            })
