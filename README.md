# FastAPI Chat Backend — Real-Time Messaging Microservice

Real-time chat microservice handling 1:1 messaging, group chat, customer support queue, file uploads, AES-256-GCM encryption at rest, and compliance monitoring.

## Tech Stack

- **FastAPI** (Python 3.10+)
- **MongoDB** (Motor async driver)
- **Redis** (online presence)
- **RabbitMQ** (async message processing)
- **Socket.IO** (real-time WebSocket)
- **AES-256-GCM** (encryption at rest)

## Quick Start

```bash
python -m venv venv
source venv/bin/activate        # Linux/Mac
# venv\Scripts\activate         # Windows

pip install -r requirements.txt

# Create .env (see below)

mkdir -p uploads
uvicorn app.main:app --host 0.0.0.0 --port 8006 --reload
```

## Environment Variables

```env
FASTAPI_PORT=8006
CORS_ORIGINS=http://localhost:3001,http://localhost:3100

# Auth (shared with NestJS)
JWT_SECRET=your_shared_secret
JWT_ALGORITHM=HS256
COOKIE_NAME=accessToken

# Databases
POSTGRES_URL=postgresql+asyncpg://postgres:password@localhost:5432/auth_crud
MONGODB_URL=mongodb://localhost:27017
MONGODB_DB=fastapi_chat
REDIS_URL=redis://localhost:6379
RABBITMQ_URL=amqp://guest:guest@localhost:5672

# Encryption (generate: python -c "import secrets; print(secrets.token_hex(32))")
CHAT_ENCRYPTION_KEY=your_64_char_hex_key

# Storage
STORAGE_MODE=1                    # 0=S3 passthrough, 1=local
UPLOAD_DIR=./uploads
PUBLIC_URL=http://localhost:8006

# Limits
DELETE_FOR_EVERYONE_HOURS=48
MAX_IMAGE_SIZE=10485760           # 10MB
MAX_VIDEO_SIZE=26214400           # 25MB
MAX_DOCUMENT_SIZE=26214400        # 25MB
MAX_VOICE_SIZE=5242880            # 5MB

# Support
SUPPORT_VISITOR_TOKEN_EXPIRES_HOURS=720
SUPPORT_AGENT_ROLE_SLUGS=super_admin,company_admin,manager
```

## REST API Endpoints

### Conversations & Messages

| Method | Path | Description |
|--------|------|-------------|
| GET | `/chat/users` | Chatable users (role-based) |
| GET | `/chat/conversations` | All 1:1 conversations |
| POST | `/chat/conversations/{user_id}` | Get/create conversation |
| DELETE | `/chat/conversations/{id}` | Soft-delete |
| GET | `/chat/conversations/{id}/messages` | Paginated messages |
| POST | `/chat/conversations/{id}/read` | Mark as read |
| DELETE | `/chat/messages/{id}` | Delete (self or everyone) |
| GET | `/chat/messages/{id}/info` | Delivery/read info |
| GET | `/chat/unread-count` | Unread counts |

### Groups

| Method | Path | Description |
|--------|------|-------------|
| GET | `/chat/groups` | All groups |
| POST | `/chat/groups` | Create group |
| PATCH | `/chat/groups/{id}` | Update name/avatar |
| POST | `/chat/groups/{id}/members` | Add members |
| DELETE | `/chat/groups/{id}/members/{uid}` | Remove member |
| POST | `/chat/groups/{id}/leave` | Leave group |
| GET | `/chat/groups/{id}/messages` | Group messages |
| POST | `/chat/groups/{id}/read` | Mark group read |

### File Uploads

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat/attachments` | Upload image/video/document |
| POST | `/chat/attachments/voice` | Upload voice note |
| POST | `/chat/groups/{id}/avatar` | Group avatar |

### Customer Auth (Public / Widget)

Session managed via HttpOnly cookies (`customerToken`). No tokens exposed to JavaScript.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/customer/check-email` | Check customer exists (sets HttpOnly cookie) |
| POST | `/customer/register` | Register customer (sets HttpOnly cookie) |
| GET | `/customer/me` | Restore session from cookie |
| POST | `/customer/logout` | Clear session cookie |
| POST | `/customer/enter-queue` | Enter support queue (cookie auth) |
| GET | `/customer/conversation` | Get current conversation (cookie auth) |

### Support Queue (Agent)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/chat/support-queue` | List queue (filter by status) |
| POST | `/chat/support-queue/{id}/accept` | Accept customer |
| POST | `/chat/support-queue/{id}/resolve` | Resolve conversation |
| GET | `/chat/support-queue/customer/{id}/conversations` | Customer history |

### Compliance (Super Admin / Officer)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat/compliance/policies` | Create policy |
| GET | `/chat/compliance/policies` | List policies |
| GET | `/chat/compliance/users` | Scoped user list |
| GET | `/chat/compliance/conversations/{id}/messages` | Read messages (audited) |
| GET | `/chat/compliance/users/{id}/messages` | User messages (audited) |
| GET | `/chat/compliance/audit-logs` | Audit trail |

## WebSocket Events (Socket.IO)

### 1:1 Messages

| Event | Direction | Description |
|-------|-----------|-------------|
| `chat:send` | Client → Server | Send message |
| `chat:message_confirmed` | Server → Client | tempId → realId |
| `chat:receive` | Server → Client | Message delivery |
| `chat:typing` | Bidirectional | Typing indicator |
| `chat:status_updated` | Server → Client | Delivered/read |
| `chat:message_deleted` | Server → Client | Deleted for everyone |

### Group Messages

| Event | Direction | Description |
|-------|-----------|-------------|
| `chat:group_send` | Client → Server | Send group message |
| `chat:group_message` | Server → Client | Group message delivery |
| `chat:group_typing` | Bidirectional | Group typing |
| `chat:group_messages_read` | Server → Client | Read by member |
| `chat:group_message_delivered` | Server → Client | Delivered to member |
| `chat:group_member_added` | Server → Client | User added to group |
| `chat:group_member_removed` | Server → Client | User removed |
| `chat:group_member_left` | Server → Client | Member left |
| `chat:group_members_added` | Server → Client | New members added |
| `chat:group_updated` | Server → Client | Group info changed |
| `chat:group_system_message` | Server → Client | System message |

### Online Presence

| Event | Direction | Description |
|-------|-----------|-------------|
| `user:online` / `user:offline` | Server → Client | Employee status |
| `users:online_list` | Server → Client | Online list on connect |
| `customer:online` / `customer:offline` | Server → Client | Customer status |

### Support Chat

| Event | Direction | Description |
|-------|-----------|-------------|
| `support:chat:started` | Server → Customer | Agent accepted |
| `support:chat:resolved` | Server → Customer | Chat resolved |
| `support:agent:disconnected` | Server → Customer | Agent left |
| `support:queue:updated` | Server → Agent | Queue changed |

## RabbitMQ Queues

| Queue | Purpose |
|-------|---------|
| `chat.messages` | Async 1:1 message processing |
| `chat.group_messages` | Async group message processing |
| `support.notifications` | Agent queue notifications |
| `*.dlq` | Dead letter queues |

## MongoDB Collections

`conversations` · `messages` · `chat_permissions` · `compliance_policies` · `compliance_audit_logs`

Indexes created automatically on startup.

## Key Features

- **AES-256-GCM Encryption** — Per-conversation keys via HKDF-SHA256
- **RabbitMQ Processing** — Async message save + delivery with DLQ
- **Pending Delivery** — Offline messages auto-deliver on reconnect
- **@Mentions** — @user + @all with position-based rendering
- **System Messages** — WhatsApp-style group event notifications
- **Compliance** — Scoped audited access to encrypted messages
- **Dual Auth** — Employee JWT (cookie: `accessToken`/`session_token`) + Customer JWT (HttpOnly cookie: `customerToken`)

## Related Projects

| Project | Purpose |
|---------|---------|
| [nest-postgres-db-first-cookie-based](../nest-postgres-db-first-cookie-based) | Core API (shares PostgreSQL + JWT secret) |
| [nest-frontend](../nest-frontend) | Admin dashboard + chat UI |
| [support-chat-widget](../support-chat-widget) | Customer chat widget |
