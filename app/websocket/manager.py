"""
WebSocket Connection Manager.

Local dict for socket routing: user_id → set of Socket.IO session IDs (sids).
Redis for online/offline status (cross-server).
Redis Pub/Sub for cross-server event delivery.
"""
import asyncio
from typing import Any

import socketio

from app.services.online_service import mark_user_online, mark_user_offline
from app.redis.pubsub import subscribe_for_user, publish_to_user, set_delivery_callback

# Local socket routing: user_id → set of sids
_user_sockets: dict[int, set[str]] = {}

# Reverse map: sid → (user_id, company_id)
_sid_to_user: dict[str, tuple[int, int]] = {}

# Pub/Sub listener tasks per user
_pubsub_tasks: dict[int, asyncio.Task] = {}

# Reference to Socket.IO server (set during init)
_sio: socketio.AsyncServer | None = None


def init_manager(sio: socketio.AsyncServer):
    """Initialize the manager with a Socket.IO server reference."""
    global _sio
    _sio = sio
    set_delivery_callback(_deliver_from_pubsub)


async def _deliver_from_pubsub(user_id: int, event: str, data: dict):
    """Callback for Redis Pub/Sub — deliver event to user's local sockets."""
    sids = _user_sockets.get(user_id, set())
    for sid in sids:
        try:
            await _sio.emit(event, data, to=sid)
        except Exception as e:
            print(f"WS pubsub emit error: event={event}, user={user_id}, error={e}")


async def connect_user(user_id: int, company_id: int, sid: str):
    """Register a user's socket connection."""
    # Add to local routing
    if user_id not in _user_sockets:
        _user_sockets[user_id] = set()
    _user_sockets[user_id].add(sid)
    _sid_to_user[sid] = (user_id, company_id)

    # Mark online in Redis (only on first connection)
    if len(_user_sockets[user_id]) == 1:
        await mark_user_online(user_id, company_id)

        # Start Pub/Sub listener for this user
        if user_id not in _pubsub_tasks:
            task = await subscribe_for_user(user_id)
            _pubsub_tasks[user_id] = task


async def disconnect_user(sid: str):
    """Unregister a socket connection."""
    info = _sid_to_user.pop(sid, None)
    if not info:
        return None

    user_id, company_id = info
    sids = _user_sockets.get(user_id, set())
    sids.discard(sid)

    # If no more connections, mark offline
    if not sids:
        _user_sockets.pop(user_id, None)
        await mark_user_offline(user_id, company_id)

        # Cancel Pub/Sub listener
        task = _pubsub_tasks.pop(user_id, None)
        if task:
            task.cancel()

    return (user_id, company_id)


def get_user_info(sid: str) -> tuple[int, int] | None:
    """Get (user_id, company_id) from a socket session ID."""
    return _sid_to_user.get(sid)


def get_user_sids(user_id: int) -> set[str]:
    """Get all socket session IDs for a user (local server only)."""
    return _user_sockets.get(user_id, set())


def is_user_connected_locally(user_id: int) -> bool:
    """Check if user has any sockets on THIS server."""
    return user_id in _user_sockets and len(_user_sockets[user_id]) > 0


async def emit_to_user(user_id: int, event: str, data: Any):
    """
    Send an event to a user. Tries local sockets first,
    falls back to Redis Pub/Sub for cross-server delivery.
    """
    local_sids = get_user_sids(user_id)

    if local_sids:
        # User is on this server — emit directly
        for sid in local_sids:
            try:
                await _sio.emit(event, data, to=sid)
            except Exception as e:
                print(f"WS emit error: event={event}, user={user_id}, error={e}")
    else:
        # User might be on another server — publish via Redis
        await publish_to_user(user_id, event, data)


async def emit_to_users(user_ids: list[int], event: str, data: Any, exclude_user: int | None = None):
    """Send an event to multiple users."""
    for uid in user_ids:
        if uid != exclude_user:
            await emit_to_user(uid, event, data)


def get_all_connected_users() -> dict[int, set[str]]:
    """Get all locally connected users and their sids."""
    return dict(_user_sockets)
