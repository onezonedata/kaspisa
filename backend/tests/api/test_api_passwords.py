"""Т1, Т2: хеш пароля сервиса."""

import pytest

from kaspisa.api.passwords import DUMMY_HASH, hash_password, verify_password

PASSWORD = "service-pass-7f3a"  # noqa: S105


def test_hash_has_scrypt_format_and_verifies() -> None:
    stored = hash_password(PASSWORD)

    assert stored.startswith("scrypt$16384$8$5$")
    assert verify_password(PASSWORD, stored) is True
    assert verify_password("service-pass-7f3b", stored) is False
    assert PASSWORD not in stored


def test_hashes_of_same_password_differ() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_equivalent_unicode_forms_verify() -> None:
    # NFKC: полноширинная латиница приводится к обычной.
    stored = hash_password("ｓervice-pass-7f3a")

    assert verify_password(PASSWORD, stored) is True


@pytest.mark.parametrize("stored", ["", "scrypt$", "bcrypt$a$b", "scrypt$16384$8$1$!!$!!"])
def test_broken_hash_is_false_without_exception(stored: str) -> None:
    assert verify_password("x", stored) is False


def test_dummy_hash_is_valid_and_not_matching() -> None:
    assert DUMMY_HASH.startswith("scrypt$16384$8$5$")
    assert verify_password(PASSWORD, DUMMY_HASH) is False
