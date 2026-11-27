"""Т1, Т2: обёртка Secret не раскрывает значение."""

import copy
import pickle
from dataclasses import dataclass

import pytest

from kaspisa.security import Secret

VALUE = "p@ss-w0rd-uniq"


def test_repr_str_format_hide_value() -> None:
    secret = Secret(VALUE)
    assert secret.reveal() == VALUE
    for text in (repr(secret), str(secret), f"{secret}", format(secret, ">20"), f"{secret!r}"):
        assert VALUE not in text
        assert "***" in text


def test_not_serializable() -> None:
    secret = Secret(VALUE)
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    with pytest.raises(TypeError):
        copy.deepcopy(secret)
    with pytest.raises(TypeError):
        copy.copy(secret)


def test_vars_unavailable() -> None:
    with pytest.raises(TypeError):
        vars(Secret(VALUE))


def test_eq_and_hash() -> None:
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")
    assert Secret("a") != "a"
    with pytest.raises(TypeError):
        hash(Secret("a"))


def test_dataclass_with_secrets_repr_is_safe() -> None:
    @dataclass(frozen=True)
    class Creds:
        login: Secret
        password: Secret

    text = repr(Creds(Secret("login-uniq"), Secret(VALUE)))
    assert "login-uniq" not in text
    assert VALUE not in text
