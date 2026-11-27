"""Связка ключей шифрования и загрузка её из окружения."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from kaspisa.security.errors import KeyConfigError, UnknownKeyError

KEY_SIZE = 32
_FORMAT_HINT = (
    "ожидается <id>:<base64 от 32 байт>, id от 1 до 255; "
    "создать: python -c \"import os,base64;print('1:'+base64.b64encode(os.urandom(32)).decode())\""
)


class KeyRing:
    """Основной ключ и ключи только для расшифровки, по идентификаторам."""

    def __init__(self, primary_id: int, keys: Mapping[int, bytes]) -> None:
        if primary_id not in keys:
            raise KeyConfigError(f"основной ключ {primary_id} отсутствует в связке")
        for key_id, key in keys.items():
            if not 1 <= key_id <= 255 or len(key) != KEY_SIZE:
                raise KeyConfigError(f"ключ {key_id} задан неверно: {_FORMAT_HINT}")
        self.primary_id = primary_id
        self._keys = dict(keys)

    def key(self, key_id: int) -> bytes:
        try:
            return self._keys[key_id]
        except KeyError:
            raise UnknownKeyError(f"в связке нет ключа с идентификатором {key_id}") from None

    def __repr__(self) -> str:
        return f"KeyRing(primary_id={self.primary_id}, key_ids={sorted(self._keys)})"


class SecuritySettings(BaseSettings):
    """Ключи шифрования из окружения.

    KASPISA_ENCRYPTION_KEY: основной ключ, формат `<id>:<base64 от 32 байт>`.
    KASPISA_ENCRYPTION_OLD_KEYS: ключи только для расшифровки, через запятую, тот же формат.
    Создать ключ: python -c "import os,base64;print('1:'+base64.b64encode(os.urandom(32)).decode())"
    """

    model_config = SettingsConfigDict(env_prefix="KASPISA_", env_file=".env", extra="ignore")

    encryption_key: SecretStr | None = None
    encryption_old_keys: SecretStr | None = None


def _parse_one(text: str, name: str) -> tuple[int, bytes]:
    id_part, sep, key_part = text.strip().partition(":")
    if not sep:
        raise KeyConfigError(f"{name}: {_FORMAT_HINT}")
    try:
        key_id = int(id_part)
        key = base64.b64decode(key_part.strip(), validate=True)
    except (ValueError, binascii.Error):
        raise KeyConfigError(f"{name}: {_FORMAT_HINT}") from None
    if not 1 <= key_id <= 255:
        raise KeyConfigError(f"{name}: идентификатор вне 1..255; {_FORMAT_HINT}")
    if len(key) != KEY_SIZE:
        raise KeyConfigError(f"{name}: ключ не из 32 байт; {_FORMAT_HINT}")
    return key_id, key


def load_keyring(settings: SecuritySettings | None = None) -> KeyRing:
    """Собрать связку из окружения. Бросает KeyConfigError."""
    settings = settings or SecuritySettings()
    primary_raw = settings.encryption_key.get_secret_value() if settings.encryption_key else ""
    if not primary_raw.strip():
        raise KeyConfigError(f"KASPISA_ENCRYPTION_KEY не задан: {_FORMAT_HINT}")
    primary_id, primary_key = _parse_one(primary_raw, "KASPISA_ENCRYPTION_KEY")
    keys: dict[int, bytes] = {primary_id: primary_key}
    old_raw = (
        settings.encryption_old_keys.get_secret_value() if settings.encryption_old_keys else ""
    )
    for part in old_raw.split(","):
        if not part.strip():
            continue
        key_id, key = _parse_one(part, "KASPISA_ENCRYPTION_OLD_KEYS")
        if key_id in keys:
            raise KeyConfigError(f"повторный идентификатор ключа {key_id}")
        keys[key_id] = key
    return KeyRing(primary_id, keys)
