"""Т3–Т8, Т10: шифр секретов."""

import os
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kaspisa.security import (
    DecryptionError,
    KeyRing,
    Secret,
    SecretCipher,
    SecretContext,
    SecretField,
    UnknownKeyError,
)


def _ctx(field: SecretField = SecretField.SHOP_PASSWORD) -> SecretContext:
    return SecretContext(uuid.uuid4(), uuid.uuid4(), field)


def _cipher(key_id: int = 1, key: bytes | None = None) -> SecretCipher:
    return SecretCipher(KeyRing(key_id, {key_id: key or os.urandom(32)}))


@given(st.text())
def test_roundtrip(value: str) -> None:
    cipher = _cipher()
    ctx = _ctx()
    assert cipher.decrypt(cipher.encrypt(Secret(value), ctx), ctx) == Secret(value)


def test_roundtrip_non_ascii_and_empty() -> None:
    cipher = _cipher()
    ctx = _ctx()
    for value in ("", "пароль-Қазақстан-🔑"):
        assert cipher.decrypt(cipher.encrypt(Secret(value), ctx), ctx).reveal() == value


def test_random_nonce_and_no_plaintext() -> None:
    cipher = _cipher()
    ctx = _ctx()
    value = "visible-plain-value"
    a = cipher.encrypt(Secret(value), ctx)
    b = cipher.encrypt(Secret(value), ctx)
    assert a != b
    assert value.encode() not in a


def test_nonce_has_full_entropy() -> None:
    """Обе половины nonce меняются от блока к блоку (нет усечённого или повторяющегося nonce)."""
    cipher = _cipher()
    ctx = _ctx()
    nonces = [cipher.encrypt(Secret("x"), ctx)[2:14] for _ in range(32)]
    assert len({n[:6] for n in nonces}) > 1
    assert len({n[6:] for n in nonces}) > 1
    assert all(n[:6] != n[6:] for n in nonces)


def test_block_format() -> None:
    cipher = _cipher(key_id=5)
    blob = cipher.encrypt(Secret("x"), _ctx())
    assert blob[0] == 1
    assert blob[1] == 5
    assert len(blob) == 2 + 12 + 1 + 16
    assert cipher.key_id(blob) == 5


def test_wrong_context_rejected() -> None:
    cipher = _cipher()
    ctx = _ctx()
    blob = cipher.encrypt(Secret("v"), ctx)
    other_tenant = SecretContext(uuid.uuid4(), ctx.shop_id, ctx.field)
    other_shop = SecretContext(ctx.tenant_id, uuid.uuid4(), ctx.field)
    other_field = SecretContext(ctx.tenant_id, ctx.shop_id, SecretField.SHOP_LOGIN)
    for wrong in (other_tenant, other_shop, other_field):
        with pytest.raises(DecryptionError):
            cipher.decrypt(blob, wrong)


def test_any_modified_byte_rejected() -> None:
    cipher = _cipher()
    ctx = _ctx()
    blob = cipher.encrypt(Secret("some value"), ctx)
    for i in range(len(blob)):
        broken = bytearray(blob)
        broken[i] ^= 0x01
        with pytest.raises(DecryptionError):
            cipher.decrypt(bytes(broken), ctx)


def test_truncated_and_garbage_rejected() -> None:
    cipher = _cipher()
    ctx = _ctx()
    blob = cipher.encrypt(Secret("some value"), ctx)
    for bad in (b"", b"\x01", blob[:10], blob[:-1], blob + b"\x00"):
        with pytest.raises(DecryptionError):
            cipher.decrypt(bad, ctx)


def test_other_key_same_id_and_unknown_id() -> None:
    ctx = _ctx()
    blob = _cipher(1).encrypt(Secret("v"), ctx)
    with pytest.raises(DecryptionError) as other:
        _cipher(1).decrypt(blob, ctx)
    assert not isinstance(other.value, UnknownKeyError)
    with pytest.raises(UnknownKeyError) as unknown:
        _cipher(2).decrypt(blob, ctx)
    assert "1" in str(unknown.value)


def test_rotation() -> None:
    old_key, new_key = os.urandom(32), os.urandom(32)
    ctx = _ctx()
    old_blob = SecretCipher(KeyRing(1, {1: old_key})).encrypt(Secret("v"), ctx)
    both = SecretCipher(KeyRing(2, {1: old_key, 2: new_key}))
    assert both.decrypt(old_blob, ctx).reveal() == "v"
    assert both.needs_rotation(old_blob)
    new_blob = both.rotate(old_blob, ctx)
    assert not both.needs_rotation(new_blob)
    assert both.key_id(new_blob) == 2
    only_new = SecretCipher(KeyRing(2, {2: new_key}))
    assert only_new.decrypt(new_blob, ctx).reveal() == "v"


def test_reprs_and_errors_do_not_leak() -> None:
    key = os.urandom(32)
    ring = KeyRing(1, {1: key})
    cipher = SecretCipher(ring)
    ctx = _ctx()
    value = "leak-check-value"
    blob = cipher.encrypt(Secret(value), ctx)
    with pytest.raises(DecryptionError) as bad:
        cipher.decrypt(blob[:-1] + bytes([blob[-1] ^ 1]), ctx)
    with pytest.raises(UnknownKeyError) as unknown:
        SecretCipher(KeyRing(2, {2: os.urandom(32)})).decrypt(blob, ctx)
    texts = [repr(ring), repr(cipher), str(bad.value), repr(bad.value), str(unknown.value)]
    for text in texts:
        assert key.hex() not in text
        assert value not in text
        assert blob.hex() not in text
        assert repr(key) not in text
