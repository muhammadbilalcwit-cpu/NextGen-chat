from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service
    FASTAPI_PORT: int = 8006
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3100,http://localhost:3001"

    # Auth
    JWT_SECRET: str = "your-jwt-secret-here"
    JWT_ALGORITHM: str = "HS256"
    COOKIE_NAME: str = "session_token"

    # PostgreSQL (read-only)
    POSTGRES_URL: str = "postgresql+asyncpg://devuser:password@localhost:5432/mydb"

    # MongoDB
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "fastapi_chat"

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # RabbitMQ
    RABBITMQ_URL: str = "amqp://guest:guest@localhost:5672"

    # Support chat (public widget)
    SUPPORT_VISITOR_TOKEN_EXPIRES_HOURS: int = 720  # 30 days
    SUPPORT_AGENT_ROLE_SLUGS: str = "super_admin,company_admin,manager"
    SUPPORT_MAX_QUEUE_PAGE_LIMIT: int = 100
    SUPPORT_MAX_MESSAGE_PAGE_LIMIT: int = 100

    # Encryption (AES-256-GCM at rest)
    CHAT_ENCRYPTION_KEY: str = ""  # 64-char hex (32 bytes), generate via: python -c "import secrets; print(secrets.token_hex(32))"

    # Storage
    STORAGE_MODE: int = 1  # 0=S3 URL from frontend, 1=local server
    UPLOAD_DIR: str = "./uploads"
    PUBLIC_URL: str = "http://localhost:8006"  # Base URL for absolute file URLs

    # Delete for everyone time window (hours) — like WhatsApp 48h limit
    DELETE_FOR_EVERYONE_HOURS: float = 24

    # Attachment size limits (bytes)
    MAX_IMAGE_SIZE: int = 10_485_760      # 10MB
    MAX_VIDEO_SIZE: int = 26_214_400      # 25MB
    MAX_DOCUMENT_SIZE: int = 26_214_400   # 25MB
    MAX_VOICE_SIZE: int = 5_242_880       # 5MB

    # Allowed MIME types per attachment type (comma-separated in .env)
    ALLOWED_IMAGE_MIMES: str = "image/jpeg,image/png,image/gif,image/webp,image/heic,image/heif"
    ALLOWED_VIDEO_MIMES: str = "video/mp4,video/quicktime,video/webm,video/x-msvideo,video/x-matroska"
    ALLOWED_DOCUMENT_MIMES: str = "application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.presentationml.presentation,text/plain,text/csv,application/zip,application/x-rar-compressed,application/json"
    ALLOWED_VOICE_MIMES: str = "audio/webm,audio/ogg,audio/mp4,audio/mpeg,audio/wav,audio/mp3,audio/x-m4a"

    # Pagination
    DEFAULT_PAGE_LIMIT: int = 50
    MAX_PAGE_LIMIT: int = 100

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    @property
    def attachment_limits(self) -> dict[str, int]:
        return {
            "image": self.MAX_IMAGE_SIZE,
            "video": self.MAX_VIDEO_SIZE,
            "document": self.MAX_DOCUMENT_SIZE,
            "voice": self.MAX_VOICE_SIZE,
        }

    @property
    def allowed_mime_types(self) -> dict[str, list[str]]:
        return {
            "image": [m.strip() for m in self.ALLOWED_IMAGE_MIMES.split(",")],
            "video": [m.strip() for m in self.ALLOWED_VIDEO_MIMES.split(",")],
            "document": [m.strip() for m in self.ALLOWED_DOCUMENT_MIMES.split(",")],
            "voice": [m.strip() for m in self.ALLOWED_VOICE_MIMES.split(",")],
        }

    @property
    def support_agent_role_slugs(self) -> list[str]:
        return [slug.strip().lower() for slug in self.SUPPORT_AGENT_ROLE_SLUGS.split(",") if slug.strip()]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
