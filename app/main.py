"""
FastAPI Chat Microservice — main entry point.

Wires up:
  - FastAPI with lifespan (startup/shutdown)
  - CORS middleware
  - Socket.IO (ASGI mount)
  - REST routes
  - RabbitMQ consumers
  - Static file serving (STORAGE_MODE=1)
"""
import logging
import os
import json
import time
from contextlib import asynccontextmanager

import socketio
from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse, Response

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.log_config import Colors, format_duration, time_async, setup_logging
from app.database.postgres import init_postgres, close_postgres, get_db
from app.database.mongodb import init_mongodb, close_mongodb
from app.redis.client import init_redis, close_redis
from app.services.rabbitmq_service import (
    init_rabbitmq,
    close_rabbitmq,
    set_message_handlers,
    set_support_notification_handler,
    start_consuming,
)
from app.websocket.gateway import (
    create_sio_server,
    process_direct_message,
    process_group_message,
    process_support_notification,
)

# Routes
from app.routes.users import router as users_router
from app.routes.conversations import router as conversations_router
from app.routes.messages import router as messages_router
from app.routes.groups import router as groups_router
from app.routes.upload import router as upload_router
from app.routes.compliance import router as compliance_router
from app.routes.customer_auth import router as customer_auth_router
from app.routes.support_queue import router as support_queue_router

# Set up logging before anything else
setup_logging()
logger = logging.getLogger("__main__")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    total_start = time.perf_counter()

    # Color shortcuts
    G = Colors.GREEN
    R = Colors.RED
    C = Colors.CYAN
    Y = Colors.YELLOW
    W = Colors.WHITE
    D = Colors.DIM
    RST = Colors.RESET

    # ── Banner ──
    server_info = f"http://0.0.0.0:{settings.FASTAPI_PORT} + Socket.IO"

    logger.info("")
    logger.info(f"{C}{'=' * 60}{RST}")
    title = "Chat Microservice"
    logger.info(f"{C}={RST} {W}{title}{RST}{' ' * (60 - 4 - len(title))}{C}={RST}")
    logger.info(f"{C}={RST} {D}{server_info}{RST}{' ' * (60 - 4 - len(server_info))}{C}={RST}")
    logger.info(f"{C}{'=' * 60}{RST}")
    logger.info("")

    # ── Connections ──
    logger.info(f"  {W}Connections:{RST}")

    _, dur, ok = await time_async(init_postgres())
    if ok:
        logger.info(f"  {G}✓{RST} PostgreSQL {D}(read-only, {format_duration(dur)}){RST}")
    else:
        logger.info(f"  {R}✗{RST} PostgreSQL {D}(connection failed){RST}")

    _, dur, ok = await time_async(init_mongodb())
    if ok:
        logger.info(f"  {G}✓{RST} MongoDB {D}('{settings.MONGODB_DB}', {format_duration(dur)}){RST}")
    else:
        logger.info(f"  {R}✗{RST} MongoDB {D}(connection failed){RST}")

    _, dur, ok = await time_async(init_redis())
    if ok:
        logger.info(f"  {G}✓{RST} Redis {D}({format_duration(dur)}){RST}")
    else:
        logger.info(f"  {R}✗{RST} Redis {D}(connection failed){RST}")

    _, dur, ok = await time_async(init_rabbitmq())
    if ok:
        logger.info(f"  {G}✓{RST} RabbitMQ {D}({format_duration(dur)}){RST}")
    else:
        logger.info(f"  {R}✗{RST} RabbitMQ {D}(connection failed){RST}")

    logger.info("")

    # ── Core Services ──
    logger.info(f"  {W}Core Services:{RST}")

    # Register RabbitMQ message handlers + start consuming
    set_message_handlers(
        direct_handler=process_direct_message,
        group_handler=process_group_message,
    )
    set_support_notification_handler(process_support_notification)
    _, dur, ok = await time_async(start_consuming())
    if ok:
        logger.info(f"  {G}✓{RST} RabbitMQ Consumers {D}(direct + group + support, {format_duration(dur)}){RST}")
    else:
        logger.info(f"  {R}✗{RST} RabbitMQ Consumers {D}(failed to start){RST}")

    logger.info(f"  {G}✓{RST} Socket.IO {D}(via uvicorn){RST}")

    # Storage
    if settings.STORAGE_MODE == 1:
        os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
        logger.info(f"  {G}✓{RST} Storage {D}(local: {settings.UPLOAD_DIR}/){RST}")
    else:
        logger.info(f"  {G}✓{RST} Storage {D}(S3 URL passthrough){RST}")

    logger.info("")

    # ── Ready ──
    total_ms = (time.perf_counter() - total_start) * 1000
    ready_text = f"Ready! Started in {format_duration(total_ms)}"

    logger.info(f"{C}{'=' * 60}{RST}")
    logger.info(f"{C}={RST} {G}Ready!{RST} {W}Started in {format_duration(total_ms)}{RST}{' ' * (60 - 4 - len(ready_text))}{C}={RST}")
    logger.info(f"{C}{'=' * 60}{RST}")
    logger.info("")

    yield

    # ── Shutdown ──
    logger.info("")
    logger.info(f"{Y}Shutting down services...{RST}")

    _, dur, _ = await time_async(close_rabbitmq())
    logger.info(f"  {G}✓{RST} RabbitMQ stopped {D}({format_duration(dur)}){RST}")

    _, dur, _ = await time_async(close_redis())
    logger.info(f"  {G}✓{RST} Redis stopped {D}({format_duration(dur)}){RST}")

    _, dur, _ = await time_async(close_mongodb())
    logger.info(f"  {G}✓{RST} MongoDB stopped {D}({format_duration(dur)}){RST}")

    _, dur, _ = await time_async(close_postgres())
    logger.info(f"  {G}✓{RST} PostgreSQL stopped {D}({format_duration(dur)}){RST}")

    logger.info("")
    logger.info(f"{G}Goodbye!{RST}")
    logger.info("")


# ── FastAPI app ──
app = FastAPI(
    title="Chat Microservice",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Response Wrapper Middleware ──
# Wraps all /chat and /health responses in { message, status_code, data }
# to match the NestJS ApiResponse format the frontend expects.
# IMPORTANT: This must be registered BEFORE CORSMiddleware so that CORS
# becomes the outermost layer (Starlette add_middleware uses insert(0)).
@app.middleware("http")
async def wrap_api_response(request: Request, call_next):
    response = await call_next(request)

    path = request.url.path
    # Only wrap our API routes, not socket.io or static files
    if not path.startswith("/chat") and not path.startswith("/customer") and path != "/health":
        return response

    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        return response

    # Don't wrap error responses — FastAPI already returns { detail: "..." }
    if response.status_code >= 400:
        return response

    # Read response body
    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    try:
        data = json.loads(body)
        wrapped = json.dumps({
            "message": "Success",
            "status_code": response.status_code,
            "data": data,
        }, default=str)
        return Response(
            content=wrapped,
            status_code=response.status_code,
            media_type="application/json",
        )
    except (json.JSONDecodeError, TypeError):
        return Response(
            content=body,
            status_code=response.status_code,
            media_type=content_type,
        )




# ── REST Routes ──
app.include_router(users_router)
app.include_router(conversations_router)
app.include_router(messages_router)
app.include_router(groups_router)
app.include_router(upload_router)
app.include_router(compliance_router)
app.include_router(customer_auth_router)
app.include_router(support_queue_router)

# ── Static files (local uploads) ──
if settings.STORAGE_MODE == 1:
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")

# ── Socket.IO ──
sio = create_sio_server()
sio_asgi = socketio.ASGIApp(sio, other_asgi_app=app)


# ── ASGI-level CORS middleware ──
# Wraps the ENTIRE ASGI app (including Socket.IO, Starlette's
# ServerErrorMiddleware, etc.) so CORS headers are guaranteed on
# every HTTP response — 200, 422, 500, unhandled crashes, everything.
# This is the enterprise-grade approach: nothing can bypass it because
# it is the outermost layer that uvicorn calls.
class CORSASGIMiddleware:
    """
    Raw ASGI middleware that injects CORS headers into every HTTP response.

    Unlike Starlette's CORSMiddleware (which lives inside the middleware
    stack and can be bypassed by ServerErrorMiddleware), this sits at the
    ASGI boundary itself — the very first thing uvicorn hits.

    Handles:
      - Preflight (OPTIONS) → immediate 204 with full CORS headers
      - Normal requests     → proxies to inner app, injects headers
      - Crashes             → returns JSON 500 with CORS headers
    """

    def __init__(self, app, allowed_origins: list[str]):
        self.app = app
        self.allowed_origins = set(o.lower().rstrip("/") for o in allowed_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # WebSocket / lifespan — pass through untouched
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin", b"").decode().lower().rstrip("/")
        cors_headers = self._cors_headers(origin)

        # Preflight
        if scope["method"] == "OPTIONS" and b"access-control-request-method" in headers:
            await self._send_preflight(send, cors_headers)
            return

        # Normal request — inject CORS headers into the response
        headers_sent = False
        status_code = 200

        async def send_with_cors(message):
            nonlocal headers_sent, status_code
            if message["type"] == "http.response.start":
                headers_sent = True
                status_code = message.get("status", 200)
                existing = list(message.get("headers", []))
                existing.extend(cors_headers)
                message = {**message, "headers": existing}
            await send(message)

        try:
            await self.app(scope, receive, send_with_cors)
        except Exception:
            # If the inner app crashes before sending headers, return a
            # proper JSON 500 with CORS headers so the browser can read it.
            if not headers_sent:
                body = b'{"detail":"Internal server error"}'
                resp_headers = [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ] + cors_headers
                await send({"type": "http.response.start", "status": 500, "headers": resp_headers})
                await send({"type": "http.response.body", "body": body})
            # Re-raise so uvicorn still logs the traceback
            raise

    def _cors_headers(self, origin: str) -> list[tuple[bytes, bytes]]:
        """Build CORS response headers for the given origin."""
        if origin and origin in self.allowed_origins:
            return [
                (b"access-control-allow-origin", origin.encode()),
                (b"access-control-allow-credentials", b"true"),
                (b"access-control-allow-methods", b"GET, POST, PUT, PATCH, DELETE, OPTIONS"),
                (b"access-control-allow-headers", b"Authorization, Content-Type, X-Requested-With"),
                (b"access-control-max-age", b"86400"),
                (b"vary", b"Origin"),
            ]
        return [(b"vary", b"Origin")]

    @staticmethod
    async def _send_preflight(send, cors_headers):
        """Respond to an OPTIONS preflight request immediately."""
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": cors_headers,
        })
        await send({
            "type": "http.response.body",
            "body": b"",
        })


# Wrap the entire app — this is the outermost layer uvicorn serves
application = CORSASGIMiddleware(sio_asgi, allowed_origins=settings.cors_origins_list)


# ── Health check ──
@app.get("/health")
async def health():
    return {"status": "ok", "service": "chat"}


# ── Client config — single source of truth for frontend ──
@app.get("/chat/config")
async def get_chat_config(db: AsyncSession = Depends(get_db)):
    # Everyone can access chat by default (permissions only control WHO they see)
    # Return all role slugs so frontend's canAccessChat check passes for all roles
    from sqlalchemy import select
    from app.models.role import Role

    result = await db.execute(select(Role.slug))
    all_role_slugs = list(result.scalars().all())

    return {
        "deleteForEveryoneHours": settings.DELETE_FOR_EVERYONE_HOURS,
        "maxImageSize": settings.MAX_IMAGE_SIZE,
        "maxVideoSize": settings.MAX_VIDEO_SIZE,
        "maxDocumentSize": settings.MAX_DOCUMENT_SIZE,
        "maxVoiceSize": settings.MAX_VOICE_SIZE,
        "chatAccessRoles": all_role_slugs,
    }
