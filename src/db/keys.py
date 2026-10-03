import os
import base64
import hashlib
from typing import Optional, Dict, Any, List, Tuple
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.config import GATEWAY_SECRET_KEY, logger
from src.db.connection import get_db_connection


# encryption

def _get_aes_key() -> bytes:
    """Derives a 32-byte (256-bit) AES key from GATEWAY_SECRET_KEY using SHA-256."""
    return hashlib.sha256(GATEWAY_SECRET_KEY.encode("utf-8")).digest()


def encrypt_key(raw_key: str) -> Tuple[str, str]:
    """Encrypts a raw API key using AES-256-GCM. Returns (base64_ciphertext, base64_iv)."""
    if not raw_key:
        raise ValueError("Cannot encrypt an empty key.")
    
    aes_key = _get_aes_key()
    aesgcm = AESGCM(aes_key)
    iv = os.urandom(12)
    ciphertext = aesgcm.encrypt(iv, raw_key.encode("utf-8"), None)
    
    return base64.b64encode(ciphertext).decode("utf-8"), base64.b64encode(iv).decode("utf-8")


def decrypt_key(encrypted_key_b64: str, iv_b64: str) -> str:
    """Decrypts a Base64-encoded AES-256-GCM cipher text back into raw string."""
    if not encrypted_key_b64 or not iv_b64:
        raise ValueError("Encrypted key and IV must both be provided.")
    
    try:
        aes_key = _get_aes_key()
        aesgcm = AESGCM(aes_key)
        ciphertext = base64.b64decode(encrypted_key_b64.encode("utf-8"))
        iv = base64.b64decode(iv_b64.encode("utf-8"))
        decrypted_bytes = aesgcm.decrypt(iv, ciphertext, None)
        return decrypted_bytes.decode("utf-8")
    except Exception as e:
        logger.error(f"[VAULT ERROR] Decryption failed: {str(e)}")
        raise ValueError(f"Failed to decrypt API key: {str(e)}")


# Key ops

async def get_admin_keys() -> List[Dict[str, Any]]:
    """Returns all registered API keys for dashboard display."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            """
            SELECT platform, display_name, api_url, is_healthy, enabled, last_used_at 
            FROM api_keys 
            ORDER BY id ASC
            """
        )
        rows = await cur.fetchall()
        return [
            {
                "platform": r[0],
                "display_name": r[1],
                "api_url": r[2],
                "is_healthy": r[3] == 1,
                "enabled": r[4] == 1,
                "last_used_at": r[5]
            }
            for r in rows
        ]


async def get_decrypted_key(platform: str) -> Optional[Dict[str, Any]]:
    """Retrieves and decrypts the active credential for a platform."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, platform, display_name, api_url, encrypted_key, iv, is_healthy 
            FROM api_keys 
            WHERE platform = ? AND enabled = 1
            """,
            (platform,)
        )
        row = await cur.fetchone()
        if not row:
            return None

        key_id, platform_name, display_name, api_url, cipher, iv, is_healthy = row
        if not cipher or not iv:
            return None

        try:
            decrypted = decrypt_key(cipher, iv)
        except Exception as e:
            logger.error(f"[DB KEY] Failed to decrypt key for {platform}: {e}")
            return None

        return {
            "id": key_id,
            "platform": platform_name,
            "display_name": display_name,
            "api_url": api_url,
            "api_key": decrypted,
            "is_healthy": is_healthy == 1
        }


async def save_key(
    platform: str,
    display_name: Optional[str] = None,
    api_url: Optional[str] = None,
    raw_key: Optional[str] = None
) -> None:
    """Encrypts and upserts an API key, or updates metadata if raw_key is omitted."""
    display_name = display_name or platform.capitalize()
    async with get_db_connection() as conn:
        if raw_key:
            cipher, iv = encrypt_key(raw_key)
            await conn.execute(
                """
                INSERT INTO api_keys (platform, display_name, api_url, encrypted_key, iv, is_healthy, enabled)
                VALUES (?, ?, ?, ?, ?, 1, 1)
                ON CONFLICT(platform) DO UPDATE SET
                    display_name = excluded.display_name,
                    api_url = excluded.api_url,
                    encrypted_key = excluded.encrypted_key,
                    iv = excluded.iv,
                    is_healthy = 1,
                    enabled = 1
                """,
                (platform, display_name, api_url, cipher, iv)
            )
        else:
            await conn.execute(
                """
                UPDATE api_keys
                SET display_name = COALESCE(?, display_name),
                    api_url = COALESCE(?, api_url)
                WHERE platform = ?
                """,
                (display_name, api_url, platform)
            )
        await conn.commit()


async def toggle_key(platform: str, enabled: bool) -> bool:
    """Toggles API key enabled state."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            "UPDATE api_keys SET enabled = ? WHERE platform = ?",
            (1 if enabled else 0, platform)
        )
        await conn.commit()
        return cur.rowcount > 0


async def set_key_health(platform: str, is_healthy: bool) -> bool:
    """Updates API key health state."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            "UPDATE api_keys SET is_healthy = ? WHERE platform = ?",
            (1 if is_healthy else 0, platform)
        )
        await conn.commit()
        return cur.rowcount > 0


async def delete_key(platform: str) -> bool:
    """Deletes an API key, clears active cooldowns, and disables associated models."""
    async with get_db_connection() as conn:
        await conn.execute("DELETE FROM rate_limit_cooldowns WHERE platform = ?", (platform,))
        await conn.execute("UPDATE models SET enabled = 0 WHERE platform = ?", (platform,))
        cur = await conn.execute("DELETE FROM api_keys WHERE platform = ?", (platform,))
        await conn.commit()
        return cur.rowcount > 0
