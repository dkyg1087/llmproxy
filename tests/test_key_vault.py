import pytest
from src.key_vault import encrypt_key, decrypt_key


def test_key_vault_encryption_decryption():
    original_key = "sk-proj-test-key-123456789"
    cipher, iv = encrypt_key(original_key)
    
    assert cipher is not None
    assert iv is not None
    assert cipher != original_key
    
    decrypted = decrypt_key(cipher, iv)
    assert decrypted == original_key


def test_key_vault_empty_key_rejection():
    with pytest.raises(ValueError):
        encrypt_key("")

    with pytest.raises(ValueError):
        decrypt_key("", "")
