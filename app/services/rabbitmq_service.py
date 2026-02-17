"""
RabbitMQ producer/consumer — queue-first message processing.

Queues:
  chat.messages       → 1:1 message processing
  chat.group_messages → Group message processing
  chat.dlx / *.dlq    → Dead letter handling
"""
import json
import asyncio
from typing import Callable, Awaitable

import aio_pika
from aio_pika import ExchangeType

from app.config import settings

# Types
MessageHandler = Callable[[dict], Awaitable[None]]

# Module state
_connection: aio_pika.RobustConnection | None = None
_channel: aio_pika.Channel | None = None
_direct_handler: MessageHandler | None = None
_group_handler: MessageHandler | None = None


async def init_rabbitmq():
    """Connect to RabbitMQ, declare queues and exchanges."""
    global _connection, _channel

    _connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
    _channel = await _connection.channel()
    await _channel.set_qos(prefetch_count=10)

    # Dead letter exchange
    dlx = await _channel.declare_exchange("chat.dlx", ExchangeType.DIRECT, durable=True)

    # Dead letter queues
    dm_dlq = await _channel.declare_queue("chat.messages.dlq", durable=True)
    gm_dlq = await _channel.declare_queue("chat.group_messages.dlq", durable=True)
    await dm_dlq.bind(dlx, routing_key="chat.messages")
    await gm_dlq.bind(dlx, routing_key="chat.group_messages")

    # Main queues (with DLX)
    await _channel.declare_queue(
        "chat.messages",
        durable=True,
        arguments={
            "x-dead-letter-exchange": "chat.dlx",
            "x-dead-letter-routing-key": "chat.messages",
        },
    )
    await _channel.declare_queue(
        "chat.group_messages",
        durable=True,
        arguments={
            "x-dead-letter-exchange": "chat.dlx",
            "x-dead-letter-routing-key": "chat.group_messages",
        },
    )



async def close_rabbitmq():
    """Close RabbitMQ connection."""
    global _connection, _channel
    if _connection:
        await _connection.close()
        _connection = None
        _channel = None


def set_message_handlers(
    direct_handler: MessageHandler,
    group_handler: MessageHandler,
):
    """Register handlers for processing consumed messages."""
    global _direct_handler, _group_handler
    _direct_handler = direct_handler
    _group_handler = group_handler


async def publish_direct_message(payload: dict) -> None:
    """Publish a 1:1 message to the queue."""
    if not _channel:
        raise RuntimeError("RabbitMQ not connected")

    await _channel.default_exchange.publish(
        aio_pika.Message(
            body=json.dumps(payload).encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key="chat.messages",
    )


async def publish_group_message(payload: dict) -> None:
    """Publish a group message to the queue."""
    if not _channel:
        raise RuntimeError("RabbitMQ not connected")

    await _channel.default_exchange.publish(
        aio_pika.Message(
            body=json.dumps(payload).encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key="chat.group_messages",
    )


async def start_consuming():
    """Start consuming messages from both queues."""
    if not _channel:
        raise RuntimeError("RabbitMQ not connected")

    dm_queue = await _channel.get_queue("chat.messages")
    gm_queue = await _channel.get_queue("chat.group_messages")

    async def _process_direct(message: aio_pika.IncomingMessage):
        async with message.process():
            try:
                payload = json.loads(message.body.decode())
                if _direct_handler:
                    await _direct_handler(payload)
            except Exception as e:
                print(f"RabbitMQ: Error processing direct message: {e}")
                # Message will be requeued or sent to DLQ

    async def _process_group(message: aio_pika.IncomingMessage):
        async with message.process():
            try:
                payload = json.loads(message.body.decode())
                if _group_handler:
                    await _group_handler(payload)
            except Exception as e:
                print(f"RabbitMQ: Error processing group message: {e}")

    await dm_queue.consume(_process_direct)
    await gm_queue.consume(_process_group)
