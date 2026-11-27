"""Т16, Т17, Т38: ротация ключа шифрования и проверка блоков."""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence

import pytest
from db_seed import SeededTenant, seed_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from kaspisa.db import (
    Database,
    SecretsReport,
    TenantId,
    check_shop_secrets,
    reencrypt_shop_secrets,
)
from kaspisa.db.rotate_secrets import main, run_command
from kaspisa.security import (
    KeyRing,
    Secret,
    SecretCipher,
    SecretContext,
    SecretField,
    UnknownKeyError,
)

_FIELDS = {
    "login_enc": SecretField.SHOP_LOGIN,
    "password_enc": SecretField.SHOP_PASSWORD,
    "session_enc": SecretField.SHOP_SESSION,
}


class Rig:
    """Две связки: старая (ключ 1) и новая (основной 2, старый 1) на одном соединении."""

    def __init__(self, connection: AsyncConnection) -> None:
        key1, key2 = os.urandom(32), os.urandom(32)
        self.old_ring = KeyRing(1, {1: key1})
        self.new_ring = KeyRing(2, {2: key2, 1: key1})
        self.only_new_ring = KeyRing(2, {2: key2})
        self.connection = connection
        factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        self.old_db = Database(factory, cipher=SecretCipher(self.old_ring))
        self.new_db = Database(factory, cipher=SecretCipher(self.new_ring))
        self.only_new_db = Database(factory, cipher=SecretCipher(self.only_new_ring))


@pytest.fixture
def rig(connection: AsyncConnection) -> Rig:
    return Rig(connection)


async def _blocks(connection: AsyncConnection, shop_id: uuid.UUID) -> dict[str, bytes]:
    row = (
        await connection.execute(
            text("SELECT login_enc, password_enc, session_enc FROM shop WHERE id = :i"),
            {"i": shop_id},
        )
    ).one()
    return dict(zip(_FIELDS, (bytes(v) for v in row), strict=True))


async def _write_block(
    connection: AsyncConnection, shop_id: uuid.UUID, column: str, blob: bytes
) -> None:
    assert column in _FIELDS
    await connection.execute(
        text(f"UPDATE shop SET {column} = :b WHERE id = :i"),
        {"b": blob, "i": shop_id},
    )


async def _state(connection: AsyncConnection, shop_id: uuid.UUID) -> tuple[str, int]:
    row = (
        await connection.execute(
            text("SELECT connection_state, connection_version FROM shop WHERE id = :i"),
            {"i": shop_id},
        )
    ).one()
    return row[0], row[1]


def _key_ids(blocks: dict[str, bytes]) -> set[int]:
    return {blob[1] for blob in blocks.values()}


async def test_reencrypt_moves_all_blocks_to_primary_key(rig: Rig) -> None:
    """Т16: все блоки двух арендаторов на новом ключе, значения те же, повтор ничего не делает."""
    a = await seed_tenant(rig.old_db, "a")
    b = await seed_tenant(rig.old_db, "b")
    before = {s.shop_id: await _blocks(rig.connection, s.shop_id) for s in (a, b)}
    assert all(_key_ids(blocks) == {1} for blocks in before.values())

    report = await reencrypt_shop_secrets(rig.new_db)

    assert report.rotated == 6
    assert report.pending == 0
    assert report.unreadable == []
    assert report.blocks_by_key == {2: 6}
    for seed in (a, b):
        blocks = await _blocks(rig.connection, seed.shop_id)
        assert _key_ids(blocks) == {2}
        assert blocks != before[seed.shop_id]
        async with rig.only_new_db.tenant(seed.tenant_id) as scope:
            credentials = await scope.shops.get_credentials(seed.shop_id)
            session = await scope.shops.get_session(seed.shop_id)
        assert credentials.login.reveal() == seed.login
        assert credentials.password.reveal() == seed.password
        assert session is not None
        assert session.reveal() == seed.session

    again = await reencrypt_shop_secrets(rig.new_db)
    assert again.rotated == 0
    assert again.pending == 0


async def test_reencrypt_skips_empty_session_block(rig: Rig) -> None:
    """Магазин без сессии (session_enc = NULL): два блока перешифрованы, NULL остался."""
    async with rig.old_db.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    async with rig.old_db.tenant(TenantId(tenant.id)) as scope:
        shop = await scope.shops.add(login=Secret("l-" + os.urandom(4).hex()), password=Secret("p"))
        await scope.commit()

    report = await reencrypt_shop_secrets(rig.new_db)

    assert report.rotated == 2
    assert report.blocks_by_key == {2: 2}
    assert (await _blocks_nullable(rig.connection, shop.id))[2] is None


async def _blocks_nullable(
    connection: AsyncConnection, shop_id: uuid.UUID
) -> tuple[bytes | None, ...]:
    row = (
        await connection.execute(
            text("SELECT login_enc, password_enc, session_enc FROM shop WHERE id = :i"),
            {"i": shop_id},
        )
    ).one()
    return tuple(None if v is None else bytes(v) for v in row)


async def test_unreadable_block_does_not_stop_rotation(rig: Rig) -> None:
    """Т17: нечитаемые блоки попадают в отчёт, остальное перешифровано, нечитаемое не тронуто."""
    seeds = [await seed_tenant(rig.old_db, n) for n in ("a", "b", "c")]
    seeds.sort(key=lambda s: s.shop_id)
    first, middle, last = seeds

    # Средний магазин: испорчен тег session_enc.
    middle_blocks = await _blocks(rig.connection, middle.shop_id)
    broken = bytearray(middle_blocks["session_enc"])
    broken[-1] ^= 0x01
    await _write_block(rig.connection, middle.shop_id, "session_enc", bytes(broken))
    # Последний магазин: password_enc зашифрован ключом 9, которого нет в связке.
    foreign = SecretCipher(KeyRing(9, {9: os.urandom(32)})).encrypt(
        Secret("whatever"),
        SecretContext(last.tenant_id, last.shop_id, SecretField.SHOP_PASSWORD),
    )
    await _write_block(rig.connection, last.shop_id, "password_enc", foreign)
    states = {s.shop_id: await _state(rig.connection, s.shop_id) for s in seeds}

    report = await reencrypt_shop_secrets(rig.new_db)

    assert report.rotated == 7  # 9 блоков минус два нечитаемых
    assert report.pending == 0
    unreadable = {(u.tenant_id, u.shop_id, u.field) for u in report.unreadable}
    assert unreadable == {(middle.tenant_id, middle.shop_id, SecretField.SHOP_SESSION)}
    unknown = {(u.tenant_id, u.shop_id, u.field) for u in report.unknown_key}
    assert unknown == {(last.tenant_id, last.shop_id, SecretField.SHOP_PASSWORD)}
    assert report.unknown_key_ids == [9]
    assert (await _blocks(rig.connection, middle.shop_id))["session_enc"] == bytes(broken)
    assert (await _blocks(rig.connection, last.shop_id))["password_enc"] == foreign
    assert _key_ids(await _blocks(rig.connection, first.shop_id)) == {2}
    assert (await _blocks(rig.connection, middle.shop_id))["login_enc"][1] == 2
    assert (await _blocks(rig.connection, last.shop_id))["session_enc"][1] == 2
    assert {s.shop_id: await _state(rig.connection, s.shop_id) for s in seeds} == states
    assert middle.login not in repr(report)


async def test_unreadable_block_with_unknown_key_raises_nothing(rig: Rig) -> None:
    """Блок на неизвестном ключе: без исключения наружу (UnknownKeyError внутри)."""
    seed = await seed_tenant(rig.old_db, "a")
    foreign = SecretCipher(KeyRing(9, {9: os.urandom(32)})).encrypt(
        Secret("x"), SecretContext(seed.tenant_id, seed.shop_id, SecretField.SHOP_LOGIN)
    )
    await _write_block(rig.connection, seed.shop_id, "login_enc", foreign)
    with pytest.raises(UnknownKeyError):
        rig.new_db._get_cipher().decrypt(
            foreign, SecretContext(seed.tenant_id, seed.shop_id, SecretField.SHOP_LOGIN)
        )
    report = await reencrypt_shop_secrets(rig.new_db)
    assert report.unreadable == []
    assert len(report.unknown_key) == 1
    assert report.unknown_key_ids == [9]
    assert report.blocks_by_key[9] == 1


async def test_check_reports_pending_and_writes_nothing(rig: Rig) -> None:
    """Т38: проверка видит блоки на старом ключе и ничего не меняет."""
    seed = await seed_tenant(rig.old_db, "a")
    before = await _blocks(rig.connection, seed.shop_id)

    report = await check_shop_secrets(rig.new_db)

    assert report.rotated == 0
    assert report.pending == 3
    assert report.blocks_by_key == {1: 3}
    assert await _blocks(rig.connection, seed.shop_id) == before

    await reencrypt_shop_secrets(rig.new_db)
    after = await check_shop_secrets(rig.new_db)
    assert after.pending == 0
    assert after.blocks_by_key == {2: 3}


async def test_check_does_not_count_unreadable_as_pending(rig: Rig) -> None:
    seed = await seed_tenant(rig.old_db, "a")
    await _write_block(rig.connection, seed.shop_id, "login_enc", b"\x01\x01garbage-block-bytes!")
    report = await check_shop_secrets(rig.new_db)
    assert report.pending == 2
    assert [u.field for u in report.unreadable] == [SecretField.SHOP_LOGIN]


async def test_missing_old_key_is_reported_apart_from_broken_blocks(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Б2: связка без старого ключа: «ключа нет» отдельно от «блок битый», код выхода 3."""
    seed = await seed_tenant(rig.old_db, "a")
    before = await _blocks(rig.connection, seed.shop_id)

    report = await check_shop_secrets(rig.only_new_db)
    assert report.unreadable == []
    assert len(report.unknown_key) == 3
    assert report.unknown_key_ids == [1]
    assert report.pending == 0
    for check in (True, False):
        code, out = await _run(rig.only_new_db, check=check, capsys=capsys)
        assert code == 3
        assert "нет в связке" in out
        assert "1" in out
        assert "ротация не завершена" in out
    assert await _blocks(rig.connection, seed.shop_id) == before

    # Битый блок при полной связке: код 1, про отсутствие ключа ничего нет.
    await _write_block(rig.connection, seed.shop_id, "login_enc", b"\x01\x02broken-block-bytes!!")
    code, out = await _run(rig.new_db, check=True, capsys=capsys)
    assert code == 1
    assert "нет в связке" not in out


async def test_batches_cover_all_shops(rig: Rig) -> None:
    """Переход между пачками: batch_size=1 на трёх магазинах."""
    seeds = [await seed_tenant(rig.old_db, n) for n in ("a", "b", "c")]

    checked = await check_shop_secrets(rig.new_db, batch_size=1)
    assert checked.pending == 9
    assert checked.blocks_by_key == {1: 9}

    report = await reencrypt_shop_secrets(rig.new_db, batch_size=1)

    assert report.rotated == 9
    assert report.blocks_by_key == {2: 9}
    for seed in seeds:
        assert _key_ids(await _blocks(rig.connection, seed.shop_id)) == {2}
    after = await check_shop_secrets(rig.new_db, batch_size=1)
    assert after.pending == 0
    assert after.blocks_by_key == {2: 9}


async def test_batch_size_must_be_positive(rig: Rig) -> None:
    with pytest.raises(ValueError):
        await reencrypt_shop_secrets(rig.new_db, batch_size=0)
    with pytest.raises(ValueError):
        await check_shop_secrets(rig.new_db, batch_size=0)


async def _run(db: Database, *, check: bool, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = await run_command(db, check=check)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


async def test_command_check_exit_codes(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    """Т38: код 1 при pending > 0 или нечитаемых блоках, 0 иначе."""
    seed = await seed_tenant(rig.old_db, "a")

    code, _ = await _run(rig.new_db, check=True, capsys=capsys)
    assert code == 1  # блоки на старом ключе

    code, _ = await _run(rig.new_db, check=False, capsys=capsys)
    assert code == 0
    code, _ = await _run(rig.new_db, check=True, capsys=capsys)
    assert code == 0

    await _write_block(rig.connection, seed.shop_id, "login_enc", b"\x01\x02broken-block-bytes!!")
    code, _ = await _run(rig.new_db, check=True, capsys=capsys)
    assert code == 1  # нечитаемый блок
    code, _ = await _run(rig.new_db, check=False, capsys=capsys)
    assert code == 1


async def test_command_output_has_no_secrets(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    """Т38: вывод команды — числа и идентификаторы, без значений, шифртекстов и ключей."""
    seeds: Sequence[SeededTenant] = [await seed_tenant(rig.old_db, n) for n in ("a", "b")]
    target = seeds[0]
    foreign = SecretCipher(KeyRing(9, {9: os.urandom(32)})).encrypt(
        Secret("x"), SecretContext(target.tenant_id, target.shop_id, SecretField.SHOP_LOGIN)
    )
    await _write_block(rig.connection, target.shop_id, "login_enc", foreign)
    blocks = [
        blob for s in seeds for blob in (await _blocks(rig.connection, s.shop_id)).values()
    ] + [foreign]

    outputs = []
    for check in (True, False, True):
        _, out = await _run(rig.new_db, check=check, capsys=capsys)
        outputs.append(out)
    outputs.append((await _run(rig.new_db, check=True, capsys=capsys))[1])
    joined = "\n".join(outputs)

    assert joined
    assert str(target.tenant_id) in joined  # нечитаемый блок назван арендатором и магазином
    assert str(target.shop_id) in joined
    assert "shop.login" in joined
    for seed in seeds:
        for value in (seed.login, seed.password, seed.session, seed.email, seed.merchant_uid):
            assert value not in joined
    for blob in blocks:
        assert blob.hex() not in joined
        assert repr(blob) not in joined
    for key in (rig.old_ring, rig.new_ring):
        for key_id in (1, 2):
            try:
                material = key.key(key_id)
            except Exception:  # noqa: S112 -- в связке может не быть такого идентификатора
                continue
            assert material.hex() not in joined


def test_main_hides_key_value_on_config_error(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Неверный ключ в окружении: код 2, ни значение ключа, ни пароль базы не печатаются."""
    bad_key = "7:not-a-valid-key-" + os.urandom(6).hex()
    monkeypatch.setenv("KASPISA_ENCRYPTION_KEY", bad_key)
    monkeypatch.setenv("KASPISA_DATABASE_URL", database_url)

    code = main(["--check"])

    out = capsys.readouterr()
    assert code == 2
    assert bad_key not in out.out + out.err
    assert "kaspisa:kaspisa@" not in out.out + out.err


def test_main_hides_database_password_on_connection_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """База недоступна: в выводе нет адреса с паролем."""
    key = KeyRing(1, {1: os.urandom(32)})
    import base64

    monkeypatch.setenv("KASPISA_ENCRYPTION_KEY", "1:" + base64.b64encode(key.key(1)).decode())
    password = "pw" + os.urandom(6).hex()
    monkeypatch.setenv(
        "KASPISA_DATABASE_URL", f"postgresql+asyncpg://kaspisa:{password}@127.0.0.1:1/none"
    )

    code = main(["--check"])

    out = capsys.readouterr()
    assert code == 2
    assert password not in out.out + out.err


def test_main_prints_only_error_type_for_driver_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Исключение драйвера с адресом и паролем в тексте: наружу идёт только тип."""
    import base64

    key = KeyRing(1, {1: os.urandom(32)})
    monkeypatch.setenv("KASPISA_ENCRYPTION_KEY", "1:" + base64.b64encode(key.key(1)).decode())
    password = "pw" + os.urandom(6).hex()

    async def failing(*_args: object, **_kwargs: object) -> int:
        raise ConnectionError(f"postgresql://kaspisa:{password}@db:5432/kaspisa")

    monkeypatch.setattr("kaspisa.db.rotate_secrets.run_command", failing)

    code = main([])

    out = capsys.readouterr()
    assert code == 2
    assert password not in out.out + out.err
    assert "ConnectionError" in out.err


async def test_report_type_is_exported() -> None:
    assert SecretsReport.__name__ == "SecretsReport"


def _foreign_block(seed: SeededTenant, field: SecretField, key_id: int = 9) -> bytes:
    return SecretCipher(KeyRing(key_id, {key_id: os.urandom(32)})).encrypt(
        Secret("whatever"), SecretContext(seed.tenant_id, seed.shop_id, field)
    )


async def test_unknown_key_wins_over_unreadable_in_exit_code(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Т42: при битом блоке и блоке на ключе вне связки код 3, а не 1; блоки названы отдельно."""
    a = await seed_tenant(rig.old_db, "a")
    b = await seed_tenant(rig.old_db, "b")
    await _write_block(rig.connection, a.shop_id, "login_enc", b"\x01\x02broken-block-bytes!!")
    await _write_block(
        rig.connection, b.shop_id, "password_enc", _foreign_block(b, SecretField.SHOP_PASSWORD)
    )
    for check in (True, False):
        code, out = await _run(rig.new_db, check=check, capsys=capsys)
        assert code == 3
        assert "нет в связке" in out
        assert "нечитаемых блоков: 1" in out
        assert "не хватает ключей с идентификаторами: 9\n" in out


async def test_unknown_key_ids_are_listed_sorted_and_unique(rig: Rig) -> None:
    a = await seed_tenant(rig.old_db, "a")
    await _write_block(
        rig.connection, a.shop_id, "login_enc", _foreign_block(a, SecretField.SHOP_LOGIN, 7)
    )
    await _write_block(
        rig.connection, a.shop_id, "password_enc", _foreign_block(a, SecretField.SHOP_PASSWORD, 5)
    )
    await _write_block(
        rig.connection, a.shop_id, "session_enc", _foreign_block(a, SecretField.SHOP_SESSION, 5)
    )
    report = await check_shop_secrets(rig.new_db)
    assert report.unknown_key_ids == [5, 7]
    assert len(report.unknown_key) == 3
    assert report.blocks_by_key == {5: 2, 7: 1}


async def test_rotation_completes_only_after_broken_block_is_rewritten(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Т42 (б, в): битый блок на K1 держит K1 в blocks_by_key и код 1, перезапись снимает."""
    seed = await seed_tenant(rig.old_db, "a")
    broken = bytearray((await _blocks(rig.connection, seed.shop_id))["session_enc"])
    broken[-1] ^= 0x01
    await _write_block(rig.connection, seed.shop_id, "session_enc", bytes(broken))

    report = await reencrypt_shop_secrets(rig.new_db)
    assert report.unknown_key == []
    assert report.unknown_key_ids == []
    assert report.rotated == 2
    assert [u.field for u in report.unreadable] == [SecretField.SHOP_SESSION]
    assert report.blocks_by_key == {1: 1, 2: 2}
    code, out = await _run(rig.new_db, check=True, capsys=capsys)
    assert code == 1
    assert "нет в связке" not in out

    version = (await _state(rig.connection, seed.shop_id))[1]
    async with rig.new_db.tenant(seed.tenant_id) as scope:
        await scope.shops.save_session(seed.shop_id, Secret("fresh"), expected_version=version)
        await scope.commit()
    after = await check_shop_secrets(rig.new_db)
    assert after.unreadable == []
    assert after.blocks_by_key == {2: 3}
    code, _ = await _run(rig.new_db, check=True, capsys=capsys)
    assert code == 0


async def test_batches_with_mixed_blocks_count_each_once(rig: Rig) -> None:
    """Пачки по 2 на пяти магазинах с битым и чужим блоками: ничего не пропущено и не задвоено."""
    seeds = [await seed_tenant(rig.old_db, n) for n in "abcde"]
    seeds.sort(key=lambda s: s.shop_id)
    await _write_block(rig.connection, seeds[2].shop_id, "login_enc", b"\x01\x02broken-bytes!!!!!")
    await _write_block(
        rig.connection,
        seeds[4].shop_id,
        "session_enc",
        _foreign_block(seeds[4], SecretField.SHOP_SESSION),
    )
    for size in (1, 2, 4, 5, 6, 100):
        report = await check_shop_secrets(rig.new_db, batch_size=size)
        assert report.pending == 13, size
        assert len(report.unreadable) == 1, size
        assert len(report.unknown_key) == 1, size
        assert report.blocks_by_key == {1: 13, 2: 1, 9: 1}, size
    written = await reencrypt_shop_secrets(rig.new_db, batch_size=2)
    assert written.rotated == 13
    after = await check_shop_secrets(rig.new_db, batch_size=3)
    assert after.pending == 0
    assert after.blocks_by_key == {2: 14, 9: 1}
