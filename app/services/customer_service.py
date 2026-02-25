"""
Customer service — manages customer registration, lookup, and queue entry.

Customers are fully independent from the users table.
Handles CRUD for the PostgreSQL `customers` table and creates
support conversations in MongoDB using the unified conversation schema.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.mongodb import get_db as get_mongo_db
from app.models.customer import Customer
from app.models.mongo.conversation import ConversationDocument, SupportMetadata
from app.models.role import Role
from app.models.user import User
from app.services.rabbitmq_service import publish_support_notification


CUSTOMER_JWT_TYPE = "customer"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(doc: dict) -> dict:
    """Make MongoDB document JSON-safe (ObjectId -> str, datetime -> ISO)."""
    from bson import ObjectId

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


def create_customer_token(customer_id: int, company_id: int) -> str:
    """Create a JWT token for a customer (independent from users table)."""
    expires = _now() + timedelta(hours=settings.SUPPORT_VISITOR_TOKEN_EXPIRES_HOURS)
    payload = {
        "typ": CUSTOMER_JWT_TYPE,
        "sub": customer_id,
        "companyId": company_id,
        "roles": ["customer"],
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


async def check_email(*, company_id: int, email: str, db: AsyncSession) -> dict:
    """
    Check if a customer with this email exists for the given company.
    Returns { exists: true, name, email, customerId, token } or { exists: false }.
    """
    normalized = email.strip().lower()

    result = await db.execute(
        select(Customer).where(
            func.lower(Customer.email) == normalized,
            Customer.company_id == company_id,
        )
    )
    customer = result.scalar_one_or_none()

    if not customer:
        return {"exists": False}

    token = create_customer_token(customer.id, customer.company_id)
    return {
        "exists": True,
        "name": customer.name,
        "email": customer.email,
        "customerId": customer.id,
        "token": token,
    }


async def register_customer(
    *,
    email: str,
    name: str,
    company_id: int,
    phone: Optional[str] = None,
    location: Optional[str] = None,
    ip_address: Optional[str] = None,
    metadata: Optional[dict] = None,
    db: AsyncSession,
) -> dict:
    """
    Register a new customer. Creates ONLY a customers row (no users row).
    Returns { customer, token, isNew }.
    """
    normalized = email.strip().lower()

    # Check if customer already exists for this company
    existing = await db.execute(
        select(Customer).where(
            func.lower(Customer.email) == normalized,
            Customer.company_id == company_id,
        )
    )
    existing_customer = existing.scalar_one_or_none()

    if existing_customer:
        token = create_customer_token(existing_customer.id, existing_customer.company_id)
        return {
            "customer": _customer_to_dict(existing_customer),
            "token": token,
            "isNew": False,
        }

    # Create new customer record
    customer = Customer(
        email=normalized,
        name=name.strip(),
        company_id=company_id,
        phone=phone,
        location=location,
        ip_address=ip_address,
        source="widget",
        metadata_=metadata or {},
    )
    db.add(customer)
    await db.flush()
    await db.commit()

    token = create_customer_token(customer.id, company_id)
    return {
        "customer": _customer_to_dict(customer),
        "token": token,
        "isNew": True,
    }


async def enter_queue(
    *,
    customer_id: int,
    company_id: int,
    db: AsyncSession,
) -> dict:
    """
    Place customer in the support queue by creating a conversation
    with isSupportChat=True and supportStatus='waiting'.
    Returns the conversation document.
    """
    mongo = get_mongo_db()
    now = _now()

    # Check if there's already a waiting/active conversation for this customer
    existing = await mongo.conversations.find_one({
        "isSupportChat": True,
        "supportStatus": {"$in": ["waiting", "active"]},
        "supportMetadata.customerId": customer_id,
    })

    if existing:
        return _serialize(existing)

    # Look for preferred agent from last resolved conversation
    preferred_agent_id = None
    last_resolved = await mongo.conversations.find_one(
        {
            "isSupportChat": True,
            "supportStatus": "resolved",
            "supportMetadata.customerId": customer_id,
        },
        sort=[("supportMetadata.resolvedAt", -1)],
    )
    if last_resolved and len(last_resolved.get("participants", [])) >= 2:
        agents = [p for p in last_resolved["participants"] if p != customer_id]
        if agents:
            preferred_agent_id = agents[0]

    # Create support conversation (customer only -- agent added on accept)
    support_metadata = SupportMetadata(
        customerId=customer_id,
        companyId=company_id,
        preferredAgentId=preferred_agent_id,
        source="widget",
        waitingSince=now,
    )

    conv_doc = ConversationDocument(
        participants=[customer_id],
        isSupportChat=True,
        supportStatus="waiting",
        supportMetadata=support_metadata,
        createdAt=now,
        updatedAt=now,
    )

    doc = conv_doc.model_dump()
    if doc.get("supportMetadata"):
        doc["supportMetadata"] = support_metadata.model_dump()

    result = await mongo.conversations.insert_one(doc)
    doc["_id"] = result.inserted_id

    serialized = _serialize(doc)

    # Notify agents via RabbitMQ
    agent_ids = await _get_support_agent_ids(company_id, db)
    print(f"[ENTER-QUEUE] company_id={company_id}, agent_ids={agent_ids}")
    if agent_ids:
        await publish_support_notification({
            "event": "support:queue:new",
            "agentIds": agent_ids,
            "data": {
                "conversation": serialized,
                "preferredAgentId": preferred_agent_id,
            },
        })
        print(f"[ENTER-QUEUE] Published support:queue:new to RabbitMQ for {len(agent_ids)} agents")
    else:
        print(f"[ENTER-QUEUE] WARNING: No support agent IDs found for company_id={company_id}")

    return serialized


async def get_customer_by_id(customer_id: int, db: AsyncSession) -> Optional[Customer]:
    """Get customer record by ID."""
    result = await db.execute(
        select(Customer).where(Customer.id == customer_id)
    )
    return result.scalar_one_or_none()


# --- Helpers ---


async def _get_support_agent_ids(company_id: int, db: AsyncSession) -> list[int]:
    """Get all support-eligible agent user IDs for a company."""
    from app.models.role import UserRole

    allowed_slugs = set(settings.support_agent_role_slugs)

    role_result = await db.execute(
        select(Role.id, Role.slug).where(func.lower(Role.slug).in_(allowed_slugs))
    )
    slug_by_role_id = {row.id: row.slug for row in role_result.all()}
    if not slug_by_role_id:
        return []

    target_role_ids = list(slug_by_role_id.keys())

    primary_result = await db.execute(
        select(User.id).where(
            User.company_id == company_id,
            User.role_id.in_(target_role_ids),
            User.is_active == True,  # noqa: E712
            User.deleted_at.is_(None),
        )
    )
    agent_ids = set(row[0] for row in primary_result.all())

    secondary_result = await db.execute(
        select(UserRole.user_id).join(User, User.id == UserRole.user_id).where(
            User.company_id == company_id,
            UserRole.role_id.in_(target_role_ids),
            User.is_active == True,  # noqa: E712
            User.deleted_at.is_(None),
        )
    )
    for row in secondary_result.all():
        agent_ids.add(row[0])

    return list(agent_ids)


def _customer_to_dict(customer: Customer) -> dict:
    return {
        "id": customer.id,
        "email": customer.email,
        "name": customer.name,
        "companyId": customer.company_id,
        "phone": customer.phone,
        "location": customer.location,
        "ipAddress": customer.ip_address,
        "source": customer.source,
        "canLogin": customer.can_login,
    }
