"""
Server-side encryption service — AES-256-GCM with per-conversation keys.

Key hierarchy:
  Master Key (CHAT_ENCRYPTION_KEY from env)
    → Per-conversation key (HKDF-SHA256, info="chat-encryption-{conversationId}")
      → Per-message IV (random 12 bytes)

This follows Microsoft Teams-style encryption at rest:
  - Server encrypts before MongoDB insert
  - Server decrypts on read before sending to client
  - All transport is over TLS/WSS (in-transit encryption)
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from app.config import settings


def _get_master_key() -> bytes:
    """Get the 32-byte master encryption key from config."""
    key_hex = settings.CHAT_ENCRYPTION_KEY
    if not key_hex:
        raise RuntimeError("CHAT_ENCRYPTION_KEY is not set")
    return bytes.fromhex(key_hex)


def _derive_conversation_key(conversation_id: str) -> bytes:
    """Derive a per-conversation 256-bit key using HKDF-SHA256."""
    master_key = _get_master_key()
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"chat-encryption-{conversation_id}".encode(),
    )
    return hkdf.derive(master_key)


def is_encryption_enabled() -> bool:
    """Check if encryption is configured."""
    return bool(settings.CHAT_ENCRYPTION_KEY)


def encrypt_content(plaintext: str, conversation_id: str) -> dict:
    """
    Encrypt message content with AES-256-GCM.

    Returns:
        {"ciphertext": base64, "iv": base64, "tag": base64}
    """
    key = _derive_conversation_key(conversation_id)
    iv = os.urandom(12)  # 96-bit nonce for GCM
    aesgcm = AESGCM(key)

    # AESGCM.encrypt appends the 16-byte tag to the ciphertext
    ct_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)

    # Split: last 16 bytes = tag, rest = ciphertext
    ciphertext = ct_with_tag[:-16]
    tag = ct_with_tag[-16:]

    return {
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "iv": base64.b64encode(iv).decode(),
        "tag": base64.b64encode(tag).decode(),
    }


def decrypt_content(
    ciphertext_b64: str,
    iv_b64: str,
    tag_b64: str,
    conversation_id: str,
) -> str:
    """Decrypt message content from AES-256-GCM."""
    key = _derive_conversation_key(conversation_id)
    iv = base64.b64decode(iv_b64)
    ciphertext = base64.b64decode(ciphertext_b64)
    tag = base64.b64decode(tag_b64)

    aesgcm = AESGCM(key)
    # AESGCM.decrypt expects ciphertext + tag concatenated
    plaintext = aesgcm.decrypt(iv, ciphertext + tag, None)
    return plaintext.decode("utf-8")