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
from app.database.postgres import async_session
from app.auth.jwt import validate_ws_token
from app.websocket.manager import (
    init_manager,
    connect_user,
    disconnect_user,
    get_user_info,
    get_sid_identity,
    emit_to_user,
    emit_to_customer,
    emit_to_users,
    is_user_connected_locally,
    is_customer_connected_locally,
    get_all_connected_users,
)
from app.services import chat_service
from app.services.online_service import is_user_online, get_online_users
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

        # Fallback: check cookies (employee session_token or customer customerToken)
        if not token:
            cookies = environ.get("HTTP_COOKIE", "")
            from app.config import settings
            for cookie_name in [settings.COOKIE_NAME, "customerToken"]:
                for cookie in cookies.split(";"):
                    cookie = cookie.strip()
                    if cookie.startswith(f"{cookie_name}="):
                        token = cookie.split("=", 1)[1]
                        break
                if token:
                    break

        if not token:
            raise socketio.exceptions.ConnectionRefusedError("No token provided")

        # Validate token — returns CurrentUser or CurrentCustomer
        from app.auth.jwt import CurrentCustomer

        async with async_session() as db:
            identity = await validate_ws_token(token, db)

        if not identity:
            raise socketio.exceptions.ConnectionRefusedError("Invalid token")

        is_customer = isinstance(identity, CurrentCustomer)

        # Store identity info in session
        await sio.save_session(sid, {
            "user_id": identity.id,
            "company_id": identity.company_id,
            "name": identity.name,
            "is_customer": is_customer,
        })

        # Register connection (separate socket tracking for customers vs employees)
        await connect_user(identity.id, identity.company_id, sid, is_customer=is_customer)

        if not is_customer:
            # Deliver pending messages and notify senders (double ticks)
            # Skip for customers on widget — they receive messages via real-time events only
            delivery = await deliver_pending_messages(identity.id, identity.company_id)
            for item in delivery.get("direct", []):
                # Determine if sender is a customer by checking conversation metadata
                sender_id = item["senderId"]
                conv_id = item["conversationId"]
                emit_fn = emit_to_user  # default: sender is an employee
                try:
                    from app.database.mongodb import get_db as get_mongo_db
                    from bson import ObjectId
                    mongo = get_mongo_db()
                    conv_doc = await mongo.conversations.find_one(
                        {"_id": ObjectId(conv_id) if not isinstance(conv_id, ObjectId) else conv_id},
                        {"isSupportChat": 1, "supportMetadata.customerId": 1},
                    )
                    if conv_doc and conv_doc.get("isSupportChat"):
                        cust_id = conv_doc.get("supportMetadata", {}).get("customerId")
                        if cust_id and sender_id == cust_id:
                            emit_fn = emit_to_customer
                except Exception:
                    pass
                await emit_fn(sender_id, "chat:status_updated", {
                    "conversationId": item["conversationId"],
                    "status": "delivered",
                    "messageIds": item["messageIds"],
                })
            for item in delivery.get("groups", []):
                await emit_to_user(item["senderId"], "chat:group_message_delivered", {
                    "groupId": item["groupId"],
                    "messageId": item["messageId"],
                    "deliveredToUserId": identity.id,
                })

        # Notify relevant parties that this entity is online
        if is_customer:
            # Customer came online — notify all connected employees
            # get_all_connected_users() returns _user_sockets (employees only)
            all_connected = get_all_connected_users()
            if all_connected:
                await emit_to_users(
                    list(all_connected.keys()),
                    "customer:online",
                    {"customerId": identity.id, "companyId": identity.company_id},
                )
        else:
            all_connected = get_all_connected_users()
            await emit_to_users(
                list(all_connected.keys()),
                "user:online",
                {"userId": identity.id},
                exclude_user=identity.id,
            )

            # Send the current online user list to the newly connected user
            # so their UI shows accurate online status from the start
            online_ids = await get_online_users(identity.company_id)
            online_list = [int(uid) for uid in online_ids if uid != str(identity.id)]
            if online_list:
                await sio.emit("users:online_list", {"userIds": online_list}, to=sid)

        label = "Customer" if is_customer else "User"
        print(f"WS: {label} {identity.id} ({identity.name}) connected [sid={sid}]")

    @sio.event
    async def disconnect(sid):
        """Handle socket disconnection."""
        # Check identity BEFORE disconnect removes it
        identity = get_sid_identity(sid)
        result = await disconnect_user(sid)
        if not result:
            return

        user_id, company_id = result
        is_customer = identity[2] if identity else False

        if is_customer:
            label = "Customer"
            # Emit customer offline to all employees if no more connections
            if not is_customer_connected_locally(user_id):
                all_connected = get_all_connected_users()
                if all_connected:
                    await emit_to_users(
                        list(all_connected.keys()),
                        "customer:offline",
                        {"customerId": user_id, "companyId": company_id},
                    )
        else:
            label = "User"
            # Only emit offline for employees, and only if no more connections
            if not is_user_connected_locally(user_id):
                all_connected = get_all_connected_users()
                await emit_to_users(
                    list(all_connected.keys()),
                    "user:offline",
                    {"userId": user_id},
                )

                # Agent went fully offline — reassign their active support conversations
                await _handle_agent_disconnect(user_id, company_id)

        print(f"WS: {label} {user_id} disconnected [sid={sid}]")

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
        conversation_id = data.get("conversationId")  # Provided by support widget

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
            "senderIsCustomer": session.get("is_customer", False),
            "conversationId": conversation_id,
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
            rid = int(recipient_id)
            typing_data = {
                "senderId": session["user_id"],
                "isTyping": is_typing,
            }
            if session.get("is_customer"):
                # Customer typing to agent — agent is always an employee
                await emit_to_user(rid, "chat:typing", typing_data)
            elif data.get("isSupportChat"):
                # Agent typing to customer in support chat
                await emit_to_customer(rid, "chat:typing", typing_data)
            else:
                # Employee typing to employee (internal chat)
                await emit_to_user(rid, "chat:typing", typing_data)

    @sio.on("chat:read")
    async def handle_read(sid, data):
        """Mark messages as read in a 1:1 conversation."""
        session = await sio.get_session(sid)
        if not session:
            return {"success": False}

        conversation_id = data.get("conversationId")
        if not conversation_id:
            return {"success": False}

        # Determine if this is a support chat BEFORE marking messages,
        # so we can pass role info to avoid customer.id / user.id collision.
        reader_is_customer_flag = None  # None = regular chat
        try:
            from app.database.mongodb import get_db as get_mongo_db
            from bson import ObjectId
            mongo = get_mongo_db()
            conv_doc = await mongo.conversations.find_one(
                {"_id": ObjectId(conversation_id) if len(conversation_id) == 24 else conversation_id},
                {"isSupportChat": 1, "supportMetadata.customerId": 1},
            )
            is_support_chat = bool(conv_doc and conv_doc.get("isSupportChat"))
        except Exception:
            is_support_chat = False

        if is_support_chat:
            reader_is_customer_flag = session.get("is_customer", False)

        result = await chat_service.mark_messages_read(
            conversation_id, session["user_id"], is_group=False,
            reader_is_customer=reader_is_customer_flag,
        )

        # Notify each sender that their messages were read
        if result["markedCount"] > 0:
            for sender_id in result.get("senderIds", []):
                status_data = {
                    "conversationId": conversation_id,
                    "status": "read",
                    "messageIds": result["messageIds"],
                    "readBy": session["user_id"],
                }
                if is_support_chat:
                    # In support chat, senders are the opposite role of the reader.
                    fn = emit_to_user if reader_is_customer_flag else emit_to_customer
                else:
                    fn = emit_to_user
                await fn(sender_id, "chat:status_updated", status_data)

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


# ─── Agent Disconnect: Reassign Support Conversations ────────────────────────

async def _handle_agent_disconnect(agent_id: int, company_id: int):
    """
    When an agent goes fully offline, find their active support conversations
    and revert them to 'waiting' so another agent can pick them up.
    Notifies the customer and other agents.
    """
    from datetime import datetime, timezone
    from app.database.mongodb import get_db as get_mongo_db
    from app.services.rabbitmq_service import publish_support_notification

    mongo = get_mongo_db()
    now = datetime.now(timezone.utc)

    # Find all active support conversations assigned to this agent
    cursor = mongo.conversations.find({
        "isSupportChat": True,
        "supportStatus": "active",
        "participants": agent_id,
        "supportMetadata.companyId": company_id,
    })

    async for conv in cursor:
        conv_id = conv["_id"]
        customer_id = conv.get("supportMetadata", {}).get("customerId")

        # Remove agent from participants, revert to waiting
        customer_participants = [p for p in conv.get("participants", []) if p != agent_id]
        await mongo.conversations.update_one(
            {"_id": conv_id},
            {
                "$set": {
                    "participants": customer_participants,
                    "supportStatus": "waiting",
                    "supportMetadata.waitingSince": now,
                    "supportMetadata.acceptedAt": None,
                    "updatedAt": now,
                },
            },
        )

        # Notify customer that agent disconnected
        if customer_id:
            await emit_to_customer(customer_id, "support:agent:disconnected", {
                "conversationId": str(conv_id),
                "message": "Your agent is temporarily unavailable. Reconnecting you to the next available agent...",
            })

        # Notify other agents that a conversation is back in the queue
        async with async_session() as db:
            from app.services.customer_service import _get_support_agent_ids
            agent_ids = await _get_support_agent_ids(company_id, db)

        if agent_ids:
            await publish_support_notification({
                "event": "support:queue:updated",
                "agentIds": agent_ids,
                "data": {
                    "conversationId": str(conv_id),
                    "status": "waiting",
                    "reason": "agent_disconnected",
                    "previousAgentId": agent_id,
                },
            })

        print(f"WS: Reassigned support conv {conv_id} (agent {agent_id} disconnected) → waiting")


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
    sender_is_customer = payload.get("senderIsCustomer", False)
    provided_conv_id = payload.get("conversationId")
    attachment = payload.get("attachment")
    mentions = payload.get("mentions")

    # For support chats (customer → agent or agent → customer),
    # use the provided conversationId to find the correct support conversation.
    # This avoids get_or_create_conversation which could find a wrong conversation
    # due to customer_id / user_id collision.
    conv = None
    if provided_conv_id:
        from app.database.mongodb import get_db as get_mongo_db
        from bson import ObjectId
        mongo = get_mongo_db()
        raw_conv = await mongo.conversations.find_one({"_id": ObjectId(provided_conv_id)})
        if raw_conv:
            conv = chat_service._serialize(raw_conv)

    if not conv:
        # Only check for existing support conversation if the sender is a customer.
        # Without this guard, customer.id / user.id collision causes internal employee
        # messages to be routed into support conversations (e.g., customer.id=5 and
        # user.id=5 both exist, participants [3,5] match a support conv).
        if sender_is_customer:
            from app.database.mongodb import get_db as get_mongo_db2
            mongo2 = get_mongo_db2()
            raw_support = await mongo2.conversations.find_one({
                "participants": {"$all": [sender_id, recipient_id]},
                "isSupportChat": True,
            })
            if raw_support:
                conv = chat_service._serialize(raw_support)

        if not conv:
            conv = await chat_service.get_or_create_conversation(sender_id, recipient_id)

    is_support = conv.get("isSupportChat", False)
    support_customer_id = conv.get("supportMetadata", {}).get("customerId") if is_support else None

    print(f"[DEBUG] process_direct_message: sender={sender_id}, recipient={recipient_id}, conv_id={conv['_id']}, is_support={is_support}, support_customer_id={support_customer_id} (type={type(support_customer_id).__name__ if support_customer_id else 'None'}), provided_conv_id={provided_conv_id}")

    # Save message
    message = await chat_service.save_message(
        conversation_id=conv["_id"],
        sender_id=sender_id,
        recipient_id=recipient_id,
        content=content,
        attachment=attachment,
        mentions=mentions,
        sender_is_customer=sender_is_customer if is_support else None,
    )

    # Determine which emit function to use for each participant.
    # Uses sender_is_customer flag (role) instead of comparing IDs, because
    # customer.id and user.id can collide (separate auto-increment sequences).
    def _emit_for(*, is_sender: bool):
        """Return the correct emit function for a participant by role."""
        if not is_support:
            return emit_to_user
        if is_sender:
            return emit_to_customer if sender_is_customer else emit_to_user
        else:
            # Recipient is the opposite role of the sender
            return emit_to_user if sender_is_customer else emit_to_customer

    # Confirm to sender (tempId → real messageId)
    confirm_data = {
        "tempId": temp_id,
        "messageId": message["_id"],
        "conversationId": conv["_id"],
    }
    await _emit_for(is_sender=True)(sender_id, "chat:message_confirmed", confirm_data)
    print(f"[DELIVERY-DEBUG] STEP1: Confirmation sent to sender={sender_id}, sender_is_customer={sender_is_customer}")

    # Enrich conversation with otherUser info for each side.
    from sqlalchemy import select
    from app.models.user import User
    from app.models.customer import Customer

    user_map: dict = {}
    customer_map: dict = {}

    try:
        async with async_session() as db:
            if is_support and support_customer_id:
                # Support chat: look up customer and agent from their respective tables
                # Use role flag instead of ID comparison to avoid collision
                agent_id = recipient_id if sender_is_customer else sender_id
                result = await db.execute(select(User).where(User.id == agent_id))
                agent_user = result.scalar_one_or_none()
                if agent_user:
                    user_map[agent_user.id] = agent_user

                cust_result = await db.execute(
                    select(Customer).where(Customer.id == support_customer_id)
                )
                cust = cust_result.scalar_one_or_none()
                if cust:
                    customer_map[cust.id] = cust
            else:
                # Regular chat: look up from users, fallback to customers for missing IDs
                result = await db.execute(
                    select(User).where(User.id.in_([sender_id, recipient_id]))
                )
                user_map = {u.id: u for u in result.scalars().all()}

                missing_ids = [pid for pid in [sender_id, recipient_id] if pid not in user_map]
                if missing_ids:
                    cust_result = await db.execute(
                        select(Customer).where(Customer.id.in_(missing_ids))
                    )
                    customer_map = {c.id: c for c in cust_result.scalars().all()}
        print(f"[DELIVERY-DEBUG] STEP2: user_map_keys={list(user_map.keys())}, customer_map_keys={list(customer_map.keys())}")
    except Exception as e:
        print(f"[DELIVERY-DEBUG] STEP2 FAILED: DB lookup error: {e}")
        import traceback; traceback.print_exc()
        return

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

    def _customer_info(c):
        if not c:
            return None
        return {
            "id": c.id,
            "firstname": c.name,
            "lastname": "",
            "email": c.email,
            "profilePicture": None,
            "isOnline": True,
        }

    def _participant_info(pid):
        if pid in user_map:
            return _user_info(user_map[pid])
        if pid in customer_map:
            return _customer_info(customer_map[pid])
        return None

    def _participant_company_id(pid):
        if pid in user_map:
            return user_map[pid].company_id
        if pid in customer_map:
            return customer_map[pid].company_id
        return None

    if is_support and support_customer_id:
        # Role-based otherUser resolution to handle customer.id / user.id collision.
        # Look up each from their own table to avoid one shadowing the other.
        customer_as_other = _customer_info(customer_map.get(support_customer_id))
        agent_as_other = None
        for uid, u in user_map.items():
            agent_as_other = _user_info(u)
            break
        if sender_is_customer:
            sender_conv = {**conv, "otherUser": agent_as_other}      # customer sees agent
            recipient_conv = {**conv, "otherUser": customer_as_other}  # agent sees customer
        else:
            sender_conv = {**conv, "otherUser": customer_as_other}    # agent sees customer
            recipient_conv = {**conv, "otherUser": agent_as_other}    # customer sees agent
    else:
        # Regular chat: look up by participant ID
        sender_conv = {**conv, "otherUser": _participant_info(recipient_id)}
        recipient_conv = {**conv, "otherUser": _participant_info(sender_id)}

    # Deliver to sender's other sessions too
    print(f"[DELIVERY-DEBUG] STEP3: Emitting chat:receive to sender's other sessions (sender={sender_id})")
    await _emit_for(is_sender=True)(sender_id, "chat:receive", {
        "message": message,
        "conversation": sender_conv,
    })

    recipient_company_id = _participant_company_id(recipient_id)
    recipient_online = await is_user_online(recipient_id, recipient_company_id) if recipient_company_id else False
    print(f"[DELIVERY-DEBUG] STEP4: recipient={recipient_id}, company_id={recipient_company_id}, online={recipient_online}")
    if recipient_online:
        # Deliver and mark as delivered
        emit_fn = _emit_for(is_sender=False)
        print(f"[DELIVERY-DEBUG] STEP5: Delivering to recipient={recipient_id} via {emit_fn.__name__}")
        await emit_fn(recipient_id, "chat:receive", {
            "message": message,
            "conversation": recipient_conv,
        })
        print(f"[DELIVERY-DEBUG] STEP6: Delivered! Marking as delivered...")
        delivered_ids = await chat_service.mark_messages_delivered(
            conv["_id"], recipient_id, is_group=False,
            recipient_is_customer=(not sender_is_customer) if is_support else None,
        )
        if delivered_ids:
            await _emit_for(is_sender=True)(sender_id, "chat:status_updated", {
                "conversationId": conv["_id"],
                "status": "delivered",
                "messageIds": delivered_ids,
            })
            print(f"[DELIVERY-DEBUG] STEP7: Status updated to 'delivered' for {len(delivered_ids)} messages")
    else:
        print(f"[DELIVERY-DEBUG] STEP4-SKIP: Recipient {recipient_id} is OFFLINE (company_id={recipient_company_id})")


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


async def process_support_notification(payload: dict) -> None:
    """Process a support notification from RabbitMQ — relay to agent sockets."""
    event = payload.get("event", "support:queue:new")
    data = payload.get("data", {})
    agent_ids = payload.get("agentIds", [])
    print(f"[SUPPORT-NOTIFY] event={event}, agent_ids={agent_ids}, data_keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
    for agent_id in agent_ids:
        print(f"[SUPPORT-NOTIFY] Emitting {event} to agent_id={agent_id}")
        await emit_to_user(int(agent_id), event, data)
