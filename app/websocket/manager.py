"""
WebSocket Connection Manager.

Local dict for socket routing: user_id → set of Socket.IO session IDs (sids).
Separate dict for customer sockets to avoid ID collision with employees.
Redis for online/offline status (cross-server).
Redis Pub/Sub for cross-server event delivery.

IMPORTANT: customer.id and user.id are separate auto-increment sequences that
can collide (e.g., customer.id=1 and user.id=1). We use separate dicts
(_user_sockets vs _customer_sockets) to prevent routing events to the wrong entity.
"""
import asyncio
from typing import Any

import socketio

from app.services.online_service import mark_user_online, mark_user_offline
from app.redis.pubsub import subscribe_for_user, publish_to_user, set_delivery_callback

# Local socket routing — separate namespaces to avoid customer/user ID collision.
_user_sockets: dict[int, set[str]] = {}       # employee user_id → set of sids
_customer_sockets: dict[int, set[str]] = {}   # customer_id → set of sids

# Reverse map: sid → (entity_id, company_id, is_customer)
_sid_to_identity: dict[str, tuple[int, int, bool]] = {}

# Pub/Sub listener tasks per entity (separate to avoid collision)
_user_pubsub_tasks: dict[int, asyncio.Task] = {}
_customer_pubsub_tasks: dict[int, asyncio.Task] = {}

# Reference to Socket.IO server (set during init)
_sio: socketio.AsyncServer | None = None


def init_manager(sio: socketio.AsyncServer):
    """Initialize the manager with a Socket.IO server reference."""
    global _sio
    _sio = sio
    set_delivery_callback(_deliver_from_pubsub)


async def _deliver_from_pubsub(user_id: int, event: str, data: dict):
    """Callback for Redis Pub/Sub — deliver event to employee user's local sockets."""
    sids = _user_sockets.get(user_id, set())
    for sid in sids:
        try:
            await _sio.emit(event, data, to=sid)
        except Exception as e:
            print(f"WS pubsub emit error: event={event}, user={user_id}, error={e}")


async def _deliver_customer_from_pubsub(customer_id: int, event: str, data: dict):
    """Callback for Redis Pub/Sub — deliver event to customer's local sockets."""
    sids = _customer_sockets.get(customer_id, set())
    for sid in sids:
        try:
            await _sio.emit(event, data, to=sid)
        except Exception as e:
            print(f"WS pubsub emit error: event={event}, customer={customer_id}, error={e}")


async def connect_user(user_id: int, company_id: int, sid: str, is_customer: bool = False):
    """Register a socket connection (employee or customer)."""
    sockets = _customer_sockets if is_customer else _user_sockets
    pubsub_tasks = _customer_pubsub_tasks if is_customer else _user_pubsub_tasks

    print(f"[DEBUG] connect_user: id={user_id}, company={company_id}, sid={sid}, is_customer={is_customer}")

    if user_id not in sockets:
        sockets[user_id] = set()
    sockets[user_id].add(sid)
    _sid_to_identity[sid] = (user_id, company_id, is_customer)
    print(f"[DEBUG] connect_user: _customer_sockets keys={list(_customer_sockets.keys())}, _user_sockets keys={list(_user_sockets.keys())}")

    # Mark online in Redis (only on first connection for this entity type)
    if len(sockets[user_id]) == 1:
        await mark_user_online(user_id, company_id, is_customer=is_customer)

        # Start Pub/Sub listener for this entity
        if user_id not in pubsub_tasks:
            if is_customer:
                from app.redis.pubsub import subscribe_for_customer
                task = await subscribe_for_customer(user_id, _deliver_customer_from_pubsub)
            else:
                task = await subscribe_for_user(user_id)
            pubsub_tasks[user_id] = task


async def disconnect_user(sid: str):
    """Unregister a socket connection."""
    info = _sid_to_identity.pop(sid, None)
    if not info:
        return None

    user_id, company_id, is_customer = info
    sockets = _customer_sockets if is_customer else _user_sockets
    pubsub_tasks = _customer_pubsub_tasks if is_customer else _user_pubsub_tasks

    sids = sockets.get(user_id, set())
    sids.discard(sid)

    # If no more connections for this entity type, mark offline
    if not sids:
        sockets.pop(user_id, None)
        await mark_user_offline(user_id, company_id, is_customer=is_customer)

        # Cancel Pub/Sub listener
        task = pubsub_tasks.pop(user_id, None)
        if task:
            task.cancel()

    return (user_id, company_id)


def get_user_info(sid: str) -> tuple[int, int] | None:
    """Get (user_id, company_id) from a socket session ID."""
    info = _sid_to_identity.get(sid)
    if info:
        return (info[0], info[1])
    return None


def get_sid_identity(sid: str) -> tuple[int, int, bool] | None:
    """Get (entity_id, company_id, is_customer) from a socket session ID."""
    return _sid_to_identity.get(sid)


def get_user_sids(user_id: int) -> set[str]:
    """Get all socket session IDs for an employee user (local server only)."""
    return _user_sockets.get(user_id, set())


def get_customer_sids(customer_id: int) -> set[str]:
    """Get all socket session IDs for a customer (local server only)."""
    return _customer_sockets.get(customer_id, set())


def is_user_connected_locally(user_id: int) -> bool:
    """Check if employee user has any sockets on THIS server."""
    return user_id in _user_sockets and len(_user_sockets[user_id]) > 0


def is_customer_connected_locally(customer_id: int) -> bool:
    """Check if customer has any sockets on THIS server."""
    return customer_id in _customer_sockets and len(_customer_sockets[customer_id]) > 0


async def emit_to_user(user_id: int, event: str, data: Any):
    """
    Send an event to an employee user. Tries local sockets first,
    falls back to Redis Pub/Sub for cross-server delivery.
    """
    local_sids = get_user_sids(user_id)

    if event.startswith("support:"):
        print(f"[EMIT-TO-USER] event={event}, user_id={user_id}, local_sids={local_sids}, _user_sockets_keys={list(_user_sockets.keys())}")

    if local_sids:
        for sid in local_sids:
            try:
                await _sio.emit(event, data, to=sid)
                if event.startswith("support:"):
                    print(f"[EMIT-TO-USER] SENT {event} to user={user_id}, sid={sid}")
            except Exception as e:
                print(f"WS emit error: event={event}, user={user_id}, error={e}")
    else:
        if event.startswith("support:"):
            print(f"[EMIT-TO-USER] No local sids for user={user_id}, falling back to Redis pubsub")
        await publish_to_user(user_id, event, data)


async def emit_to_customer(customer_id: int, event: str, data: Any):
    """
    Send an event to a customer (widget). Tries local sockets first,
    falls back to Redis Pub/Sub for cross-server delivery.
    """
    local_sids = get_customer_sids(customer_id)
    print(f"[DEBUG] emit_to_customer: customer_id={customer_id}, event={event}, local_sids={local_sids}, _customer_sockets keys={list(_customer_sockets.keys())}")

    if local_sids:
        for sid in local_sids:
            try:
                await _sio.emit(event, data, to=sid)
                print(f"[DEBUG] emit_to_customer: SENT event={event} to sid={sid}")
            except Exception as e:
                print(f"WS emit error: event={event}, customer={customer_id}, error={e}")
    else:
        print(f"[DEBUG] emit_to_customer: NO local sids, falling back to Redis pubsub")
        from app.redis.pubsub import publish_to_customer
        await publish_to_customer(customer_id, event, data)


async def emit_to_users(user_ids: list[int], event: str, data: Any, exclude_user: int | None = None):
    """Send an event to multiple employee users."""
    for uid in user_ids:
        if uid != exclude_user:
            await emit_to_user(uid, event, data)


def get_all_connected_users() -> dict[int, set[str]]:
    """Get all locally connected employee users and their sids."""
    return dict(_user_sockets)
