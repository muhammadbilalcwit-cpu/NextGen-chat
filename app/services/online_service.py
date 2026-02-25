"""
Online/offline status management via Redis.

Keys:
  online:company:{company_id}   → SET of user IDs  (company-scoped, for user lists)
  online:super_admins            → SET of super_admin user IDs (global, cross-company)
  online:customer:{company_id}   → SET of customer user IDs (separate from employees)
"""
from app.redis.client import get_redis

SUPER_ADMIN_ONLINE_KEY = "online:super_admins"


async def mark_user_online(user_id: int, company_id: int, is_customer: bool = False) -> None:
    redis = get_redis()
    if is_customer:
        await redis.sadd(f"online:customer:{company_id}", str(user_id))
    else:
        await redis.sadd(f"online:company:{company_id}", str(user_id))


async def mark_user_offline(user_id: int, company_id: int, is_customer: bool = False) -> None:
    redis = get_redis()
    if is_customer:
        await redis.srem(f"online:customer:{company_id}", str(user_id))
    else:
        await redis.srem(f"online:company:{company_id}", str(user_id))


async def is_user_online(user_id: int, company_id: int) -> bool:
    """Check if user is online within their company, as a customer, or as a global super_admin."""
    redis = get_redis()
    # Check company-scoped key first (employees)
    if await redis.sismember(f"online:company:{company_id}", str(user_id)):
        return True
    # Check customer-scoped key
    if await redis.sismember(f"online:customer:{company_id}", str(user_id)):
        return True
    # Fallback: check global super_admin key (super_admin is cross-company)
    return await redis.sismember(SUPER_ADMIN_ONLINE_KEY, str(user_id))


async def get_online_users(company_id: int) -> set[str]:
    """Get all online user IDs for a company (as strings)."""
    redis = get_redis()
    return await redis.smembers(f"online:company:{company_id}")


async def get_online_customers(company_id: int) -> set[str]:
    """Get all online customer user IDs for a company (as strings)."""
    redis = get_redis()
    return await redis.smembers(f"online:customer:{company_id}")
