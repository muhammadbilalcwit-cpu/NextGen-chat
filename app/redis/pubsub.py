"""
Redis Pub/Sub for cross-server WebSocket delivery.

Channels:
  chat:deliver:{user_id}  → deliver message/event to user on another server
"""
import json
import asyncio
from typing import Callable, Awaitable

from app.redis.client import get_redis

# Callback type: async fn(user_id: int, event: str, data: dict)
DeliveryCallback = Callable[[int, str, dict], Awaitable[None]]

_delivery_callback: DeliveryCallback | None = None
_subscriber_task: asyncio.Task | None = None


def set_delivery_callback(callback: DeliveryCallback) -> None:
    global _delivery_callback
    _delivery_callback = callback


async def publish_to_user(user_id: int, event: str, data: dict) -> None:
    """Publish an event to a specific user's channel (cross-server delivery)."""
    redis = get_redis()
    payload = json.dumps({"event": event, "data": data})
    await redis.publish(f"chat:deliver:{user_id}", payload)


async def _subscriber_loop(user_ids: set[int]) -> None:
    """Subscribe to channels for locally connected users and route events."""
    redis = get_redis()
    pubsub = redis.pubsub()

    channels = [f"chat:deliver:{uid}" for uid in user_ids]
    if channels:
        await pubsub.subscribe(*channels)

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            channel = message["channel"]
            # Extract user_id from channel name: "chat:deliver:42" → 42
            user_id = int(channel.split(":")[-1])
            payload = json.loads(message["data"])
            if _delivery_callback:
                await _delivery_callback(user_id, payload["event"], payload["data"])
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe()
        await pubsub.close()


async def subscribe_for_user(user_id: int) -> None:
    """Subscribe to a user's delivery channel (when they connect to this server)."""
    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"chat:deliver:{user_id}")

    async def _listen():
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                payload = json.loads(message["data"])
                if _delivery_callback:
                    await _delivery_callback(user_id, payload["event"], payload["data"])
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe()
            await pubsub.close()

    # Run listener as background task
    task = asyncio.create_task(_listen())
    return task


async def unsubscribe_for_user(user_id: int) -> None:
    """Unsubscribe from a user's delivery channel (when they disconnect)."""
    # The task cancellation handles cleanup — managed by ConnectionManager
    pass
