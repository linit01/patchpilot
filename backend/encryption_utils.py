"""
PatchPilot - Encryption Utilities
Handles encryption/decryption of SSH keys and passwords using Fernet (symmetric encryption)
"""

import os
import base64
from cryptography.fernet import Fernet, InvalidToken
import logging

logger = logging.getLogger(__name__)


class EncryptionManager:
    """Manages encryption and decryption of sensitive data"""
    
    def __init__(self):
        """Initialize with encryption key from environment"""
        self.encryption_key = self._get_or_create_key()
        self.fernet = Fernet(self.encryption_key)
    
    def _get_or_create_key(self) -> bytes:
        """
        Get encryption key from environment or generate new one.
        Key should be set in PATCHPILOT_ENCRYPTION_KEY environment variable.
        
        Returns:
            bytes: Fernet-compatible encryption key
        """
        key_env = os.getenv('PATCHPILOT_ENCRYPTION_KEY')
        
        if key_env:
            try:
                # Validate it's a proper Fernet key
                key_bytes = key_env.encode()
                Fernet(key_bytes)  # This will raise if invalid
                logger.info("Using encryption key from environment variable")
                return key_bytes
            except Exception as e:
                logger.error(f"Invalid encryption key in environment: {e}")
                raise ValueError("Invalid PATCHPILOT_ENCRYPTION_KEY format")
        
        # Generate new key (development/first-run only)
        # WARNING: In production, this should be set externally and persisted!
        logger.warning("No encryption key found - generating new key (DEVELOPMENT ONLY)")
        logger.warning("Set PATCHPILOT_ENCRYPTION_KEY environment variable for production!")
        
        new_key = Fernet.generate_key()
        logger.info(f"Generated new encryption key: {new_key.decode()}")
        logger.info("Save this key securely and set as PATCHPILOT_ENCRYPTION_KEY!")
        
        return new_key
    
    def encrypt(self, data: str) -> bytes:
        """
        Encrypt a string value.
        
        Args:
            data: Plain text string to encrypt
            
        Returns:
            bytes: Encrypted data
            
        Raises:
            ValueError: If data is empty or None
        """
        if not data:
            raise ValueError("Cannot encrypt empty data")
        
        try:
            encrypted = self.fernet.encrypt(data.encode())
            logger.debug("Successfully encrypted data")
            return encrypted
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise
    
    def decrypt(self, encrypted_data: bytes) -> str:
        """
        Decrypt encrypted data.
        
        Args:
            encrypted_data: Encrypted bytes to decrypt
            
        Returns:
            str: Decrypted plain text string
            
        Raises:
            ValueError: If data is invalid or decryption fails
        """
        if not encrypted_data:
            raise ValueError("Cannot decrypt empty data")
        
        try:
            decrypted = self.fernet.decrypt(encrypted_data)
            logger.debug("Successfully decrypted data")
            return decrypted.decode()
        except InvalidToken:
            logger.error("Decryption failed - invalid token (wrong key or corrupted data)")
            raise ValueError("Invalid encryption token - data may be corrupted")
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise
    
    def encrypt_to_base64(self, data: str) -> str:
        """
        Encrypt and return as base64 string (for JSON serialization).
        
        Args:
            data: Plain text string to encrypt
            
        Returns:
            str: Base64-encoded encrypted data
        """
        encrypted_bytes = self.encrypt(data)
        return base64.b64encode(encrypted_bytes).decode()
    
    def decrypt_from_base64(self, encrypted_base64: str) -> str:
        """
        Decrypt from base64-encoded string.
        
        Args:
            encrypted_base64: Base64-encoded encrypted data
            
        Returns:
            str: Decrypted plain text
        """
        encrypted_bytes = base64.b64decode(encrypted_base64.encode())
        return self.decrypt(encrypted_bytes)


class SSHKeyValidator:
    """Validates SSH key formats"""
    
    @staticmethod
    def validate_private_key(key_content: str) -> tuple[bool, str]:
        """
        Validate SSH private key format.
        
        Args:
            key_content: SSH private key string
            
        Returns:
            tuple: (is_valid: bool, message: str)
        """
        if not key_content or not key_content.strip():
            return False, "Key content is empty"
        
        key = key_content.strip()
        
        # Check for common SSH key formats
        valid_headers = [
            '-----BEGIN RSA PRIVATE KEY-----',
            '-----BEGIN DSA PRIVATE KEY-----',
            '-----BEGIN EC PRIVATE KEY-----',
            '-----BEGIN OPENSSH PRIVATE KEY-----',
            '-----BEGIN PRIVATE KEY-----',
        ]
        
        if not any(key.startswith(header) for header in valid_headers):
            return False, "Invalid SSH key format - missing header"
        
        # Check for footer
        if '-----END' not in key:
            return False, "Invalid SSH key format - missing footer"
        
        # Basic length check (keys are typically 1600+ chars)
        if len(key) < 200:
            return False, "Key appears too short to be valid"
        
        # Check for suspicious characters (should be mostly base64)
        lines = key.split('\n')
        for line in lines[1:-1]:  # Skip header/footer
            if line and not all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r' for c in line):
                return False, "Key contains invalid characters"
        
        logger.info("SSH private key validation passed")
        return True, "Valid SSH private key format"
    
    @staticmethod
    def get_key_type(key_content: str) -> str:
        """
        Determine SSH key type from content.
        
        Args:
            key_content: SSH private key string
            
        Returns:
            str: Key type (RSA, DSA, EC, OPENSSH, or UNKNOWN)
        """
        key = key_content.strip()
        
        if '-----BEGIN RSA PRIVATE KEY-----' in key:
            return 'RSA'
        elif '-----BEGIN DSA PRIVATE KEY-----' in key:
            return 'DSA'
        elif '-----BEGIN EC PRIVATE KEY-----' in key:
            return 'EC'
        elif '-----BEGIN OPENSSH PRIVATE KEY-----' in key:
            return 'OPENSSH'
        else:
            return 'UNKNOWN'


# Global encryption manager instance
encryption_manager = EncryptionManager()


# Convenience functions for use throughout the app
def encrypt_credential(data: str) -> str:
    """Encrypt a credential and return as base64 string for DB storage"""
    raw = encryption_manager.encrypt(data)
    return base64.b64encode(raw).decode('utf-8')


def decrypt_credential(encrypted_data) -> str:
    """Decrypt a credential.

    Handles three storage patterns that exist in the wild:
    1. Python str  → base64-encoded Fernet token (e.g. from old TEXT columns)
    2. BYTEA/bytes containing the UTF-8 bytes of a base64 string (current pattern:
       encrypt_credential() returns base64 str → .encode('utf-8') → stored as BYTEA)
    3. BYTEA/bytes containing raw Fernet ciphertext bytes (older direct-bytes pattern
       used by import_hosts before the .encode() fix)
    """
    if isinstance(encrypted_data, str):
        # Pattern 1: base64 string
        raw = base64.b64decode(encrypted_data.encode('utf-8'))
    else:
        # BYTEA → memoryview or bytes from asyncpg
        raw_bytes = bytes(encrypted_data) if isinstance(encrypted_data, memoryview) else bytes(encrypted_data)
        # Try pattern 2: bytes are the UTF-8 representation of a base64 string
        try:
            as_str = raw_bytes.decode('utf-8')
            # Verify it looks like a valid base64/Fernet token before decoding
            raw = base64.b64decode(as_str)
        except Exception:
            # Pattern 3: raw Fernet bytes stored directly
            raw = raw_bytes
    return encryption_manager.decrypt(raw)


def validate_ssh_key(key_content: str) -> tuple[bool, str]:
    """Validate SSH private key format"""
    return SSHKeyValidator.validate_private_key(key_content)


if __name__ == "__main__":
    # Test encryption/decryption
    print("Testing encryption utilities...")
    
    test_data = "Test SSH key content or password"
    
    encrypted = encrypt_credential(test_data)
    print(f"Encrypted: {encrypted[:50]}...")
    
    decrypted = decrypt_credential(encrypted)
    assert test_data == decrypted, "Encryption/decryption test failed!"
    print("✓ Encryption round-trip test passed")
    
    # Test SSH key validation
    valid_key = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACDj5X1Y8nQx8X8yxfhLPLfJPKnXZJxAZXz5LQXL7aM5RwAAAJj1KmzN9Sps
zQAAAAtzc2gtZWQyNTUxOQAAACDj5X1Y8nQx8X8yxfhLPLfJPKnXZJxAZXz5LQXL7aM5Rw
AAAEDdYN+T8X5yxfhLPLfJPKnXZJxAZXz5LQXL7aM5R2Pk/VjydDHxfzLF+Es8t8k8qdd
knEBlfPktBcvtozlHAAAAE3Rlc3RAZXhhbXBsZS5sb2NhbAECAwQFBg==
-----END OPENSSH PRIVATE KEY-----"""
    
    is_valid, message = validate_ssh_key(valid_key)
    print(f"SSH key validation: {is_valid} - {message}")
    print(f"Key type: {SSHKeyValidator.get_key_type(valid_key)}")
    
    print("\n✓ All encryption utility tests passed!")
