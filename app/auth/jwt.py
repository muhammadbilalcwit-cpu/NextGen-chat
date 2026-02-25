"""
JWT authentication — decodes token from cookie, validates session in PostgreSQL.

Supports three JWT formats:

Practice (NestJS):
  Cookie: accessToken
  { "sub": 123, "email": "u@t.com", "companyId": 1, "sessionId": 45 }
  -> sub is userId (number), load user directly

Production:
  Cookie: session_token
  { "sub": "user@company.com", "sid": "RSvCRK...", "iv": false }
  -> sid is session UUID, validate in user_sessions -> get user_id -> load user

Customer (widget):
  Bearer token
  { "typ": "customer", "sub": 42, "companyId": 5 }
  -> sub is customerId, load from customers table (independent from users)
"""
from dataclasses import dataclass

from datetime import datetime, timezone

from fastapi import Request, HTTPException, Depends
from jose import jwt, JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.postgres import get_db
from app.models.user import User
from app.models.session import UserSession


@dataclass
class CurrentUser:
    """Authenticated user data extracted from JWT + DB."""
    id: int
    name: str
    email: str
    picture: str | None
    company_id: int
    session_id: str
    role_id: int | None = None


@dataclass
class CurrentCustomer:
    """Authenticated customer data extracted from JWT + customers table."""
    id: int
    name: str
    email: str
    company_id: int


def decode_jwt(token: str) -> dict:
    """Decode and verify JWT token."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_sub": False},
        )
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_token_from_request(request: Request) -> str:
    """Extract JWT token from cookie or Authorization header."""
    token = (
        request.cookies.get(settings.COOKIE_NAME)
        or request.cookies.get("accessToken")
        or request.cookies.get("session_token")
    )
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return token


def _is_production_format(payload: dict) -> bool:
    """Detect JWT format: production has 'sid' (string UUID), practice has 'sub' as number."""
    return "sid" in payload


async def _validate_production_session(session_id: str, db: AsyncSession) -> UserSession | None:
    result = await db.execute(
        select(UserSession).where(
            UserSession.id == str(session_id),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > datetime.now(timezone.utc),
        )
    )
    return result.scalar_one_or_none()


async def _load_user(user_id: int, db: AsyncSession) -> User | None:
    """Load an active, non-deleted user with a company."""
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.is_active == True,  # noqa: E712
            User.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _resolve_user(payload: dict, db: AsyncSession) -> tuple[User, str]:
    """Resolve user from JWT payload — handles both NestJS and production formats."""
    if _is_production_format(payload):
        session_id = payload["sid"]
        session = await _validate_production_session(session_id, db)
        if not session:
            raise HTTPException(status_code=401, detail="Session expired or revoked")
        user = await _load_user(session.user_id, db)
        if not user:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        return user, str(session_id)
    else:
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        user_id = int(user_id)
        session_id = str(payload.get("sessionId", ""))
        user = await _load_user(user_id, db)
        if not user:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        return user, session_id


async def _resolve_customer(payload: dict, db: AsyncSession) -> CurrentCustomer:
    """Resolve customer from JWT payload. Loads from customers table (not users)."""
    from app.models.customer import Customer

    customer_id = payload.get("sub")
    company_id = payload.get("companyId")
    if not customer_id or not company_id:
        raise HTTPException(status_code=401, detail="Invalid customer token payload")

    result = await db.execute(
        select(Customer).where(Customer.id == int(customer_id))
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=401, detail="Customer not found")

    return CurrentCustomer(
        id=customer.id,
        name=customer.name,
        email=customer.email,
        company_id=customer.company_id,
    )


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """
    FastAPI dependency: extracts and validates the current user.
    Handles customer tokens by wrapping them as CurrentUser for route compatibility.
    """
    token = get_token_from_request(request)
    payload = decode_jwt(token)

    # Handle customer tokens — load from customers table
    if payload.get("typ") == "customer":
        cust = await _resolve_customer(payload, db)
        return CurrentUser(
            id=cust.id,
            name=cust.name,
            email=cust.email,
            picture=None,
            company_id=cust.company_id,
            session_id="",
            role_id=None,
        )

    user, session_id = await _resolve_user(payload, db)

    if not user.company_id:
        raise HTTPException(status_code=403, detail="User has no company")

    return CurrentUser(
        id=user.id,
        name=user.name,
        email=user.email,
        picture=user.picture,
        company_id=user.company_id,
        session_id=session_id,
        role_id=user.role_id,
    )


async def validate_ws_token(token: str, db: AsyncSession) -> CurrentUser | CurrentCustomer | None:
    """
    Validate JWT for WebSocket connections (no FastAPI Depends).
    Returns CurrentUser (agents) or CurrentCustomer (widget customers) or None.
    """
    try:
        payload = decode_jwt(token)
    except HTTPException:
        return None

    # Customer token — load from customers table
    if payload.get("typ") == "customer":
        try:
            return await _resolve_customer(payload, db)
        except (HTTPException, Exception):
            return None

    # Regular user token
    try:
        user, session_id = await _resolve_user(payload, db)
    except (HTTPException, Exception):
        return None

    if not user or not user.company_id:
        return None

    return CurrentUser(
        id=user.id,
        name=user.name,
        email=user.email,
        picture=user.picture,
        company_id=user.company_id,
        session_id=session_id,
        role_id=user.role_id,
    )
