"""
Support queue routes — agent-facing endpoints for managing the customer support queue.

Queue items are MongoDB conversations with isSupportChat=True.
The conversation itself is the queue — no separate table needed.
Customer info is looked up directly from the customers table (independent from users).
"""
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import CurrentUser, get_current_user
from app.database.mongodb import get_db as get_mongo_db
from app.database.postgres import get_db
from app.models.customer import Customer
from app.services import chat_service
from app.services.online_service import is_user_online
from app.services.rabbitmq_service import publish_support_notification
from app.websocket.manager import emit_to_user, emit_to_customer

router = APIRouter(prefix="/chat/support-queue", tags=["support-queue"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(doc: dict) -> dict:
    """Make MongoDB document JSON-safe."""
    def _convert(obj):
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_convert(item) for item in obj]
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            if obj.tzinfo is None:
                obj = obj.replace(tzinfo=timezone.utc)
            return obj.isoformat()
        return obj
    return _convert(doc)


@router.get("")
async def list_support_queue(
    status: str = Query("waiting", regex="^(waiting|active|resolved)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List support queue items (conversations with isSupportChat=True).
    Agents see conversations for their company.
    """
    mongo = get_mongo_db()

    query_filter = {
        "isSupportChat": True,
        "supportStatus": status,
        "supportMetadata.companyId": current_user.company_id,
    }

    # For active, only show conversations assigned to this agent
    if status in ("active",):
        query_filter["participants"] = current_user.id

    total = await mongo.conversations.count_documents(query_filter)

    cursor = mongo.conversations.find(query_filter).sort(
        "supportMetadata.waitingSince" if status == "waiting" else "updatedAt", -1
    ).skip((page - 1) * limit).limit(limit)

    items = []
    async for doc in cursor:
        serialized = _serialize(doc)

        # Get customer info directly from customers table via supportMetadata.customerId
        customer_info = None
        customer_id = doc.get("supportMetadata", {}).get("customerId")
        if customer_id:
            result = await db.execute(
                select(Customer).where(Customer.id == customer_id)
            )
            customer = result.scalar_one_or_none()
            if customer:
                customer_info = {
                    "customerId": customer.id,
                    "email": customer.email,
                    "name": customer.name,
                    "phone": customer.phone,
                    "location": customer.location,
                    "isOnline": await is_user_online(customer.id, customer.company_id),
                }

        # Count previous conversations for this customer
        prev_count = 0
        if customer_id:
            prev_count = await mongo.conversations.count_documents({
                "isSupportChat": True,
                "supportMetadata.customerId": customer_id,
                "_id": {"$ne": doc["_id"]},
            })

        serialized["customerInfo"] = customer_info
        serialized["previousConversationCount"] = prev_count

        # Unread count for this conversation (messages from customer not yet read by agent)
        unread_count = await chat_service.get_conversation_unread_count(
            str(doc["_id"]), current_user.id, is_group=False, is_support=True
        )
        serialized["unreadCount"] = unread_count
        items.append(serialized)

    # Counts for all statuses (for tab badges)
    status_counts = {}
    unread_counts = {}
    for s in ("waiting", "active", "resolved"):
        count_filter: dict = {
            "isSupportChat": True,
            "supportMetadata.companyId": current_user.company_id,
            "supportStatus": s,
        }
        if s == "active":
            count_filter["participants"] = current_user.id
        status_counts[s] = await mongo.conversations.count_documents(count_filter)

        # Per-status unread counts: count unread messages across all conversations in this status
        conv_ids_cursor = mongo.conversations.find(count_filter, {"_id": 1})
        conv_ids = [doc["_id"] async for doc in conv_ids_cursor]
        if conv_ids:
            unread_counts[s] = await mongo.messages.count_documents({
                "conversationId": {"$in": conv_ids},
                "senderIsCustomer": True,
                "status": {"$ne": "read"},
            })
        else:
            unread_counts[s] = 0

    return {
        "items": items,
        "total": total,
        "statusCounts": status_counts,
        "unreadCounts": unread_counts,
        "page": page,
        "limit": limit,
        "hasMore": (page * limit) < total,
    }


@router.post("/{conversation_id}/accept")
async def accept_queue_item(
    conversation_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Agent accepts a waiting customer. Updates conversation:
    - Adds agent to participants
    - Sets supportStatus to 'active'
    - Sets acceptedAt timestamp
    """
    mongo = get_mongo_db()
    now = _now()

    # Find the waiting conversation
    conv = await mongo.conversations.find_one({
        "_id": ObjectId(conversation_id),
        "isSupportChat": True,
        "supportStatus": "waiting",
    })

    if not conv:
        raise HTTPException(status_code=404, detail="Queue item not found or already accepted")

    # Verify company match
    conv_company_id = conv.get("supportMetadata", {}).get("companyId")
    if conv_company_id != current_user.company_id:
        raise HTTPException(status_code=403, detail="Not authorized for this company's queue")

    # Get customer_id from supportMetadata (the authoritative source)
    customer_id = conv.get("supportMetadata", {}).get("customerId")

    # Build participants: exactly [customer_id, new_agent_id]
    # Replace (not append) to remove any old agent IDs from previous sessions
    sorted_participants = sorted([customer_id, current_user.id]) if customer_id else [current_user.id]

    updated = await mongo.conversations.find_one_and_update(
        {"_id": ObjectId(conversation_id), "supportStatus": "waiting"},
        {
            "$set": {
                "participants": sorted_participants,
                "supportStatus": "active",
                "supportMetadata.acceptedAt": now,
                "updatedAt": now,
            },
        },
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Conversation was already accepted by another agent")

    serialized = _serialize(updated)

    # Notify customer via Socket.IO using customer_id (separate socket namespace)
    print(f"[DEBUG] accept_queue_item: customer_id={customer_id} (type={type(customer_id).__name__}), conversation_id={conversation_id}")
    if customer_id:
        await emit_to_customer(customer_id, "support:chat:started", {
            "conversationId": conversation_id,
            "agentId": current_user.id,
            "agentName": current_user.name,
        })

    # Notify other agents that queue changed (via RabbitMQ)
    from app.services.customer_service import _get_support_agent_ids
    agent_ids = await _get_support_agent_ids(current_user.company_id, db)
    if agent_ids:
        await publish_support_notification({
            "event": "support:queue:updated",
            "agentIds": agent_ids,
            "data": {
                "conversationId": conversation_id,
                "status": "active",
                "agentId": current_user.id,
            },
        })

    return serialized


@router.post("/{conversation_id}/resolve")
async def resolve_queue_item(
    conversation_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Agent resolves an active support conversation.
    """
    mongo = get_mongo_db()
    now = _now()

    conv = await mongo.conversations.find_one({
        "_id": ObjectId(conversation_id),
        "isSupportChat": True,
        "supportStatus": "active",
        "participants": current_user.id,
    })

    if not conv:
        raise HTTPException(status_code=404, detail="Active conversation not found")

    updated = await mongo.conversations.find_one_and_update(
        {"_id": ObjectId(conversation_id)},
        {
            "$set": {
                "supportStatus": "resolved",
                "supportMetadata.resolvedAt": now,
                "updatedAt": now,
            },
        },
        return_document=True,
    )

    serialized = _serialize(updated)

    # Notify customer using supportMetadata.customerId (separate socket namespace)
    customer_id = updated.get("supportMetadata", {}).get("customerId")
    if customer_id:
        await emit_to_customer(customer_id, "support:chat:resolved", {
            "conversationId": conversation_id,
        })

    # Notify agents
    from app.services.customer_service import _get_support_agent_ids
    agent_ids = await _get_support_agent_ids(current_user.company_id, db)
    if agent_ids:
        await publish_support_notification({
            "event": "support:queue:updated",
            "agentIds": agent_ids,
            "data": {
                "conversationId": conversation_id,
                "status": "resolved",
            },
        })

    return serialized


@router.get("/customer/{customer_id}/conversations")
async def get_customer_conversations(
    customer_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all support conversations for a specific customer (agent-facing).
    Returns conversations with message counts, sorted by most recent first.
    """
    mongo = get_mongo_db()

    # Verify customer belongs to agent's company
    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.company_id == current_user.company_id,
        )
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    query_filter = {
        "isSupportChat": True,
        "supportMetadata.customerId": customer_id,
    }

    total = await mongo.conversations.count_documents(query_filter)

    cursor = mongo.conversations.find(query_filter).sort(
        "createdAt", -1
    ).skip((page - 1) * limit).limit(limit)

    items = []
    async for doc in cursor:
        serialized = _serialize(doc)
        msg_count = await mongo.messages.count_documents({
            "conversationId": doc["_id"],
        })
        serialized["messageCount"] = msg_count
        items.append(serialized)

    return {
        "customer": {
            "customerId": customer.id,
            "email": customer.email,
            "name": customer.name,
            "phone": customer.phone,
        },
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "hasMore": (page * limit) < total,
    }
