"""Т9, Т10 (часть про ключи): загрузка связки ключей из окружения."""

import base64
import os

import pytest

from kaspisa.security import KeyConfigError, KeyRing, SecuritySettings, load_keyring


def _k() -> bytes:
    return os.urandom(32)


def _enc(key_id: int, key: bytes) -> str:
    return f"{key_id}:{base64.b64encode(key).decode()}"


def _load(primary: str | None, old: str | None = None) -> KeyRing:
    return load_keyring(
        SecuritySettings(_env_file=None, encryption_key=primary, encryption_old_keys=old)
    )


def test_parses_primary_and_old_keys() -> None:
    k1, k2, k3 = _k(), _k(), _k()
    ring = _load(_enc(3, k3), f"{_enc(1, k1)}, {_enc(2, k2)}")
    assert ring.primary_id == 3
    assert ring.key(3) == k3
    assert ring.key(1) == k1
    assert ring.key(2) == k2


def test_empty_old_keys_ok() -> None:
    k = _k()
    for old in (None, "", "  "):
        assert _load(_enc(1, k), old).primary_id == 1


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "   ",
        "1:" + base64.b64encode(os.urandom(16)).decode(),  # короткий
        "1:" + base64.b64encode(os.urandom(33)).decode(),  # длинный
        "0:" + base64.b64encode(os.urandom(32)).decode(),
        "256:" + base64.b64encode(os.urandom(32)).decode(),
        "x:" + base64.b64encode(os.urandom(32)).decode(),
        "1:!!!not-base64!!!",
        # верный по длине ключ с посторонним символом внутри: не должен молча приниматься
        "1:" + (lambda b: b[:10] + "!" + b[10:])(base64.b64encode(os.urandom(32)).decode()),
        "nocolon",
    ],
)
def test_bad_primary_rejected_without_leaking_value(bad: str | None) -> None:
    with pytest.raises(KeyConfigError) as info:
        _load(bad)
    text = str(info.value)
    assert "<id>:<base64" in text
    if bad and bad.strip():
        assert bad not in text


def test_duplicate_id_rejected() -> None:
    with pytest.raises(KeyConfigError):
        _load(_enc(1, _k()), _enc(1, _k()))


def test_duplicate_id_inside_old_keys_rejected() -> None:
    with pytest.raises(KeyConfigError):
        _load(_enc(1, _k()), f"{_enc(2, _k())},{_enc(2, _k())}")


def test_bad_old_key_rejected_without_leaking_value() -> None:
    bad_old = "2:" + base64.b64encode(os.urandom(5)).decode()
    with pytest.raises(KeyConfigError) as info:
        _load(_enc(1, _k()), bad_old)
    assert bad_old not in str(info.value)


def test_keyring_repr_has_only_ids() -> None:
    k1, k2 = _k(), _k()
    ring = KeyRing(2, {1: k1, 2: k2})
    text = repr(ring)
    assert "2" in text
    for key in (k1, k2):
        assert key.hex() not in text
        assert base64.b64encode(key).decode() not in text
        assert repr(key) not in text


def test_keyring_requires_primary_among_keys() -> None:
    with pytest.raises(KeyConfigError):
        KeyRing(1, {2: _k()})


def test_load_without_env_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KASPISA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("KASPISA_ENCRYPTION_OLD_KEYS", raising=False)
    with pytest.raises(KeyConfigError):
        load_keyring(SecuritySettings(_env_file=None))


def test_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    k = _k()
    monkeypatch.setenv("KASPISA_ENCRYPTION_KEY", _enc(7, k))
    monkeypatch.delenv("KASPISA_ENCRYPTION_OLD_KEYS", raising=False)
    ring = load_keyring(SecuritySettings(_env_file=None))
    assert ring.primary_id == 7
    assert ring.key(7) == k
