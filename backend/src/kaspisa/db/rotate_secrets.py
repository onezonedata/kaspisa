"""Команда ротации ключа: `python -m kaspisa.db.rotate_secrets [--check]`.

Печатает только числа, идентификаторы ключей и адреса нечитаемых блоков
(арендатор, магазин, поле). Значений, шифртекстов и ключей не печатает.
Код выхода: 0 — нечего перешифровывать, битых блоков и блоков с ключом вне связки нет;
1 — есть ждущие или битые блоки; 2 — ошибка запуска;
3 — в связке не хватает ключа, которым зашифрованы блоки (ротацию завершённой считать нельзя,
старый ключ снимать нельзя; проверьте KASPISA_ENCRYPTION_OLD_KEYS).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from kaspisa.config import Settings
from kaspisa.db.database import Database, create_database
from kaspisa.db.system import (
    SecretsReport,
    UnreadableSecret,
    check_shop_secrets,
    reencrypt_shop_secrets,
)
from kaspisa.security import KeyConfigError, SecretField


def _address(item: UnreadableSecret) -> str:
    """Адрес блока; у токена Telegram нет магазина (блок арендатора)."""
    if item.field is SecretField.TELEGRAM_TOKEN:
        return f"  арендатор {item.tenant_id}, поле {item.field.value}"
    return f"  арендатор {item.tenant_id}, магазин {item.shop_id}, поле {item.field.value}"


def _print_report(report: SecretsReport, *, check: bool) -> None:
    if not check:
        print(f"перешифровано блоков: {report.rotated}")
    print("блоков по ключам:")
    for key_id, count in report.blocks_by_key.items():
        print(f"  ключ {key_id}: {count}")
    print(f"ждут перешифрования: {report.pending}")
    print(f"нечитаемых блоков: {len(report.unreadable)}")
    for item in report.unreadable:
        print(_address(item))
    if report.unknown_key:
        ids = ", ".join(str(key_id) for key_id in report.unknown_key_ids)
        print(f"блоков с ключом, которого нет в связке: {len(report.unknown_key)}")
        print(f"в связке не хватает ключей с идентификаторами: {ids}")
        print("ротация не завершена: добавьте ключи в KASPISA_ENCRYPTION_OLD_KEYS и повторите")
        for item in report.unknown_key:
            print(_address(item))


async def run_command(database: Database, *, check: bool) -> int:
    """Выполняет перешифрование или проверку, печатает отчёт, возвращает код выхода."""
    if check:
        report = await check_shop_secrets(database)
    else:
        report = await reencrypt_shop_secrets(database)
    _print_report(report, check=check)
    if report.unknown_key:
        return 3
    return 0 if report.pending == 0 and not report.unreadable else 1


async def _run(check: bool) -> int:
    database = create_database(Settings().database_url)
    try:
        return await run_command(database, check=check)
    finally:
        await database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kaspisa.db.rotate_secrets", description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="только проверить, ничего не записывая"
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args.check))
    except KeyConfigError as error:
        # Текст KeyConfigError не содержит значения ключа.
        print(f"ошибка настройки ключа: {error}", file=sys.stderr)
    except Exception as error:
        # Текст исключения драйвера может содержать адрес базы с паролем: печатаем только тип.
        print(f"ошибка выполнения: {type(error).__name__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
