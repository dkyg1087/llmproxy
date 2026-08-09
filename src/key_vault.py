import os
import base64
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from src.config import GATEWAY_SECRET_KEY, logger


def _get_aes_key() -> bytes:
    """Derives a 32-byte (256-bit) AES key from GATEWAY_SECRET_KEY using SHA-256."""
    return hashlib.sha256(GATEWAY_SECRET_KEY.encode("utf-8")).digest()


def encrypt_key(raw_key: str) -> tuple[str, str]:
    """
    Encrypts a raw API key using AES-256-GCM.
    Returns: (base64_ciphertext, base64_iv)
    """
    if not raw_key:
        raise ValueError("Cannot encrypt an empty key.")
    
    aes_key = _get_aes_key()
    aesgcm = AESGCM(aes_key)
    
    iv = os.urandom(12)
    
    ciphertext = aesgcm.encrypt(iv, raw_key.encode("utf-8"), None)
    
    b64_ciphertext = base64.b64encode(ciphertext).decode("utf-8")
    b64_iv = base64.b64encode(iv).decode("utf-8")
    
    return b64_ciphertext, b64_iv


def decrypt_key(encrypted_key_b64: str, iv_b64: str) -> str:
    """
    Decrypts a Base64-encoded AES-256-GCM cipher text back into raw string.
    """
    if not encrypted_key_b64 or not iv_b64:
        raise ValueError("Encrypted key and IV must both be provided.")
    
    try:
        aes_key = _get_aes_key()
        aesgcm = AESGCM(aes_key)
        
        ciphertext = base64.b64decode(encrypted_key_b64.encode("utf-8"))
        iv = base64.b64decode(iv_b64.encode("utf-8"))
        
        decrypted_bytes = aesgcm.decrypt(iv, ciphertext, None)
        raw_key = decrypted_bytes.decode("utf-8")
        
        return raw_key
    except Exception as e:
        logger.error(f"[VAULT ERROR] Decryption failed: {str(e)}")
        raise ValueError(f"Failed to decrypt API key: {str(e)}")
