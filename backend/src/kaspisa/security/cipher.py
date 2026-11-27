"""Шифрование секретов магазина: AES-256-GCM с привязкой к арендатору, магазину и полю."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kaspisa.security.errors import DecryptionError, UnknownKeyError
from kaspisa.security.keys import KeyRing
from kaspisa.security.secret import Secret

FORMAT_VERSION = 1
_NONCE_SIZE = 12
_TAG_SIZE = 16
_HEADER_SIZE = 2 + _NONCE_SIZE
_AAD_PREFIX = b"kaspisa:v1|"

__all__ = [
    "DecryptionError",
    "SecretCipher",
    "SecretContext",
    "SecretField",
    "UnknownKeyError",
]


class SecretField(StrEnum):
    SHOP_LOGIN = "shop.login"
    SHOP_PASSWORD = "shop.password"  # noqa: S105 -- имя поля, не пароль
    SHOP_SESSION = "shop.session"
    TELEGRAM_TOKEN = "tenant.telegram_token"  # noqa: S105 -- имя поля, не токен


@dataclass(frozen=True, slots=True)
class SecretContext:
    """К чему привязан шифртекст."""

    tenant_id: UUID
    shop_id: UUID
    field: SecretField

    def aad(self, header: bytes) -> bytes:
        """Дополнительные данные: контекст и два первых байта блока."""
        return (
            _AAD_PREFIX
            + self.tenant_id.bytes
            + self.shop_id.bytes
            + self.field.value.encode("ascii")
            + header
        )


class SecretCipher:
    def __init__(self, keyring: KeyRing) -> None:
        self._keyring = keyring

    def __repr__(self) -> str:
        return f"SecretCipher(primary_id={self._keyring.primary_id})"

    def encrypt(self, value: Secret, context: SecretContext) -> bytes:
        key_id = self._keyring.primary_id
        header = bytes([FORMAT_VERSION, key_id])
        nonce = os.urandom(_NONCE_SIZE)
        body = AESGCM(self._keyring.key(key_id)).encrypt(
            nonce, value.reveal().encode("utf-8"), context.aad(header)
        )
        return header + nonce + body

    def decrypt(self, blob: bytes, context: SecretContext) -> Secret:
        if len(blob) < _HEADER_SIZE + _TAG_SIZE or blob[0] != FORMAT_VERSION:
            raise DecryptionError("шифртекст не расшифрован")
        header = blob[:2]
        key = self._keyring.key(blob[1])
        nonce = blob[2:_HEADER_SIZE]
        try:
            plain = AESGCM(key).decrypt(nonce, blob[_HEADER_SIZE:], context.aad(header))
            return Secret(plain.decode("utf-8"))
        except (InvalidTag, ValueError):
            raise DecryptionError("шифртекст не расшифрован") from None

    def key_id(self, blob: bytes) -> int:
        if len(blob) < 2:
            raise DecryptionError("шифртекст не расшифрован")
        return blob[1]

    def needs_rotation(self, blob: bytes) -> bool:
        return self.key_id(blob) != self._keyring.primary_id

    def rotate(self, blob: bytes, context: SecretContext) -> bytes:
        return self.encrypt(self.decrypt(blob, context), context)
