import redis.asyncio as aioredis

from app.config import settings

redis_client: aioredis.Redis = None


async def init_redis():
    global redis_client
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    await redis_client.ping()


async def close_redis():
    global redis_client
    if redis_client:
        await redis_client.close()


def get_redis() -> aioredis.Redis:
    return redis_client
