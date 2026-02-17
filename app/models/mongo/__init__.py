"""
MongoDB document models — Pydantic models representing the shape of each collection.

These models are the single source of truth for MongoDB document structure.
Used in services for save validation: Model(...).model_dump() → insert_one().
"""
from .conversation import ConversationDocument
from .group import GroupConversationDocument
from .message import (
    AttachmentSubDoc,
    DeliveryEntrySubDoc,
    MentionSubDoc,
    MessageDocument,
    SystemMessageDocument,
)
from .compliance import CompliancePolicyDocument, ComplianceAuditLogDocument
from .permission import ChatPermissionDocument
