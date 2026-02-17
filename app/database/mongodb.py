from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import settings

client: AsyncIOMotorClient = None
db: AsyncIOMotorDatabase = None


async def init_mongodb():
    """Initialize MongoDB connection and create indexes."""
    global client, db
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    db = client[settings.MONGODB_DB]

    # Create indexes for conversations
    await db.conversations.create_index("participants")
    await db.conversations.create_index("isGroup")
    await db.conversations.create_index([("lastMessageAt", -1)])
    await db.conversations.create_index("companyId")

    # Create indexes for messages
    await db.messages.create_index("conversationId")
    await db.messages.create_index("senderId")
    await db.messages.create_index("recipientId")
    await db.messages.create_index("status")
    await db.messages.create_index([("createdAt", -1)])
    await db.messages.create_index(
        [("conversationId", 1), ("createdAt", -1)]
    )
    # For pending message delivery queries
    await db.messages.create_index(
        [("recipientId", 1), ("status", 1)]
    )
    # For group message delivery queries
    await db.messages.create_index(
        [("isGroupMessage", 1), ("conversationId", 1), ("createdAt", -1)]
    )

    # Create index for chat_permissions (role-based visibility)
    await db.chat_permissions.create_index("roleId", unique=True)

    # Compliance policies — one active policy per officer
    await db.compliance_policies.create_index(
        [("userId", 1), ("isActive", 1)],
        unique=True,
        partialFilterExpression={"isActive": True},
    )
    await db.compliance_policies.create_index("grantedBy")

    # Compliance audit logs — immutable trail
    await db.compliance_audit_logs.create_index([("timestamp", -1)])
    await db.compliance_audit_logs.create_index("officerId")
    await db.compliance_audit_logs.create_index(
        [("officerId", 1), ("timestamp", -1)]
    )


async def close_mongodb():
    global client
    if client:
        client.close()


def get_db() -> AsyncIOMotorDatabase:
    """Return the MongoDB database instance."""
    return db
