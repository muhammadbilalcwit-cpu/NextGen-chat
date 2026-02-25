"""
Customer auth routes — public endpoints for the support chat widget.

Handles customer registration, email lookup, and queue entry.
Customers are fully independent from the users table.

Session management uses HttpOnly cookies (secure, SameSite=None, Partitioned)
so that customer JWT tokens are never exposed to client-side JavaScript.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.postgres import get_db
from app.services import customer_service

router = APIRouter(prefix="/customer", tags=["customer-auth"])

CUSTOMER_COOKIE_NAME = "customerToken"
CUSTOMER_COOKIE_MAX_AGE = settings.SUPPORT_VISITOR_TOKEN_EXPIRES_HOURS * 3600  # seconds


def _set_customer_cookie(response: JSONResponse, token: str) -> JSONResponse:
    """Set HttpOnly cookie with the customer JWT token."""
    response.set_cookie(
        key=CUSTOMER_COOKIE_NAME,
        value=token,
        max_age=CUSTOMER_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,        # Required: SameSite=None needs Secure (localhost is treated as secure)
        samesite="none",    # Required for cross-origin iframe
        path="/",
    )
    return response


def _clear_customer_cookie(response: JSONResponse) -> JSONResponse:
    """Clear the customer JWT cookie."""
    response.delete_cookie(
        key=CUSTOMER_COOKIE_NAME,
        httponly=True,
        secure=True,        # Must match set_cookie
        samesite="none",
        path="/",
    )
    return response


# --- Request/Response Models ---


class CheckEmailRequest(BaseModel):
    email: EmailStr
    companyId: int


class RegisterCustomerRequest(BaseModel):
    email: EmailStr
    name: str
    companyId: int
    phone: Optional[str] = None
    location: Optional[str] = None


# --- Routes ---


@router.post("/check-email")
async def check_email(
    body: CheckEmailRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Check if a customer with this email exists for the given company.
    Called by the widget before showing the registration form.
    Sets HttpOnly cookie with customer JWT if customer exists.
    """
    result = await customer_service.check_email(
        company_id=body.companyId,
        email=body.email,
        db=db,
    )

    response = JSONResponse(content=result)

    # Set cookie if customer exists and we have a token
    if result.get("exists") and result.get("token"):
        _set_customer_cookie(response, result["token"])

    return response


@router.post("/register")
async def register_customer(
    body: RegisterCustomerRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new customer. Creates a customers row in PostgreSQL.
    Returns customer info and JWT token.
    Sets HttpOnly cookie with customer JWT.
    """
    ip_address = request.client.host if request.client else None

    result = await customer_service.register_customer(
        email=body.email,
        name=body.name,
        company_id=body.companyId,
        phone=body.phone,
        location=body.location,
        ip_address=ip_address,
        metadata={
            "source": "widget",
            "userAgent": request.headers.get("user-agent", "unknown"),
        },
        db=db,
    )

    response = JSONResponse(content=result)

    if result.get("token"):
        _set_customer_cookie(response, result["token"])

    return response


@router.get("/me")
async def get_current_customer(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Get the current authenticated customer from the HttpOnly cookie.
    Used by the widget on bootstrap to restore session without localStorage.
    Returns customer info or 401 if no valid session.
    """
    token_data = _extract_customer_token(request)
    customer = await customer_service.get_customer_by_id(
        token_data["customer_id"], db
    )
    if not customer:
        raise HTTPException(status_code=401, detail="Customer not found")

    return {
        "customerId": customer.id,
        "email": customer.email,
        "name": customer.name,
        "companyId": customer.company_id,
    }


@router.post("/logout")
async def logout_customer():
    """Clear the customer session cookie."""
    response = JSONResponse(content={"ok": True})
    _clear_customer_cookie(response)
    return response


@router.post("/enter-queue")
async def enter_queue(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Place the customer in the support queue.
    Requires a customer JWT token (from check-email or register).
    """
    token_data = _extract_customer_token(request)

    result = await customer_service.enter_queue(
        customer_id=token_data["customer_id"],
        company_id=token_data["company_id"],
        db=db,
    )
    return result


@router.get("/conversation")
async def get_current_conversation(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Get the current support conversation for this customer.
    Priority: waiting/active first, then most recent resolved (for history).
    Used on widget bootstrap (page reload) to restore state.
    """
    token_data = _extract_customer_token(request)
    customer_id = token_data["customer_id"]

    from app.database.mongodb import get_db as get_mongo_db
    mongo = get_mongo_db()

    # First: check for active/waiting conversation
    conv = await mongo.conversations.find_one({
        "isSupportChat": True,
        "supportStatus": {"$in": ["waiting", "active"]},
        "supportMetadata.customerId": customer_id,
    })

    if conv:
        from app.services.customer_service import _serialize
        return _serialize(conv)

    # Fallback: return most recent resolved conversation (so customer sees history)
    resolved = await mongo.conversations.find_one(
        {
            "isSupportChat": True,
            "supportStatus": "resolved",
            "supportMetadata.customerId": customer_id,
        },
        sort=[("supportMetadata.resolvedAt", -1)],
    )

    if resolved:
        from app.services.customer_service import _serialize
        return _serialize(resolved)

    return None


# --- Token Helpers ---


def _extract_customer_token(request: Request) -> dict:
    """
    Extract and validate customer JWT from Authorization header or cookie.
    Returns { customer_id, company_id }.
    """
    from jose import JWTError, jwt as jose_jwt
    from app.config import settings

    token = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    if not token:
        token = request.cookies.get("customerToken") or request.cookies.get("accessToken")

    if not token:
        raise HTTPException(status_code=401, detail="Customer token required")

    try:
        payload = jose_jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_sub": False},
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid customer token")

    typ = payload.get("typ")

    if typ == customer_service.CUSTOMER_JWT_TYPE:
        customer_id = payload.get("sub")
        company_id = payload.get("companyId")
    else:
        raise HTTPException(status_code=403, detail="Not a customer token")

    if not customer_id or not company_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    return {"customer_id": int(customer_id), "company_id": int(company_id)}
