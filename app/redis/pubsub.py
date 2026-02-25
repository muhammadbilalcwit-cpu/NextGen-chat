"""
Redis Pub/Sub for cross-server WebSocket delivery.

Channels:
  chat:deliver:{user_id}           → deliver event to employee user on another server
  chat:deliver:customer:{cust_id}  → deliver event to customer on another server
"""
import json
import asyncio
from typing import Callable, Awaitable

from app.redis.client import get_redis

# Callback type: async fn(entity_id: int, event: str, data: dict)
DeliveryCallback = Callable[[int, str, dict], Awaitable[None]]

_delivery_callback: DeliveryCallback | None = None
_subscriber_task: asyncio.Task | None = None


def set_delivery_callback(callback: DeliveryCallback) -> None:
    global _delivery_callback
    _delivery_callback = callback


async def publish_to_user(user_id: int, event: str, data: dict) -> None:
    """Publish an event to an employee user's channel (cross-server delivery)."""
    redis = get_redis()
    payload = json.dumps({"event": event, "data": data})
    await redis.publish(f"chat:deliver:{user_id}", payload)


async def publish_to_customer(customer_id: int, event: str, data: dict) -> None:
    """Publish an event to a customer's channel (cross-server delivery)."""
    redis = get_redis()
    payload = json.dumps({"event": event, "data": data})
    await redis.publish(f"chat:deliver:customer:{customer_id}", payload)


async def subscribe_for_user(user_id: int) -> None:
    """Subscribe to an employee user's delivery channel (when they connect to this server)."""
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

    task = asyncio.create_task(_listen())
    return task


async def subscribe_for_customer(customer_id: int, callback: DeliveryCallback) -> None:
    """Subscribe to a customer's delivery channel (when they connect to this server)."""
    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"chat:deliver:customer:{customer_id}")

    async def _listen():
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                payload = json.loads(message["data"])
                if callback:
                    await callback(customer_id, payload["event"], payload["data"])
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe()
            await pubsub.close()

    task = asyncio.create_task(_listen())
    return task


async def unsubscribe_for_user(user_id: int) -> None:
    """Unsubscribe from a user's delivery channel (when they disconnect)."""
    # The task cancellation handles cleanup — managed by ConnectionManager
    pass
