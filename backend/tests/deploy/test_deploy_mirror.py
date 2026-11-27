"""Публичное зеркало (ADR 0028): чистка данных тестового магазина и ссылок на закрытые документы.

Здесь — тесты проекта этапа 16: чистка данных (Т23, Т24, Т27), оверлей (Т18–Т22) и скрипт выпуска
`release/build-mirror.sh` (Т1–Т17, Т28, Т29). Строки, запрещённые в зеркале, собираются склейкой,
чтобы сам тест не попал под запрет.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from deploy_testing import REPO
from mirror_testing import (
    SCRIPT_NAME,
    Rig,
    calls,
    clone_source,
    commit_all,
    git,
    head,
    make_mirror,
    make_rig,
    output,
    run_build,
    snapshot,
    tree_files,
)
from test_deploy_installer_docs import OVERLAY_COMMAND

RELEASE = REPO / "release"
FIXTURES = REPO / "docs" / "fixtures" / "kaspi"
DENYLIST = RELEASE / "denylist.txt"

needs_release = pytest.mark.skipif(
    not RELEASE.is_dir(), reason="нужен каталог release/ (его нет в зеркале)"
)

# Вымышленные идентификаторы фикстур. Числовые начинаются с 9 (ADR 0028, раздел 6.2).
FAKE_IDS: frozenset[str] = frozenset(
    {
        # merchant uid: свой магазин и продавцы-конкуренты (порядок сохранён)
        "90100001",
        "90100002",
        "90100003",
        "90100004",
        "90100005",
        "90100006",
        "Magazin5",
        # master SKU
        "900000001",
        "900000002",
        "900000003",
        "900000004",
        "900000005",
        # вторая часть артикула `<master>_<n>`
        "900001",
        "900000021",
        "900000022",
        "900000023",
        "900000024",
        "900000025",
        "900000026",
        "900000027",
        # offerId и идентификаторы заданий записи цены
        "9f0000000000000000000001",
        "9f0000000000000000000002",
        "9f0000000000000000000003",
        "9f0000000000000000000004",
        "9f0000000000000000000005",
        # merchantSku конкурента без составной формы
        "9000000000017",
    }
)

# Публичные константы Kaspi: id города (Астана).
PUBLIC_IDS: frozenset[str] = frozenset({"710000000"})

ID_KEYS = frozenset(
    {"merchantUid", "merchantId", "storeId", "sku", "merchantSku", "offerId", "masterSku", "id"}
)
POINT_PART = re.compile(r"PP\d+")


def _collect(node: Any, keys: frozenset[str], out: list[tuple[str, str]]) -> None:
    """Собирает пары (ключ, значение-строка) со скалярными значениями под нужными ключами."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, str | int) and not isinstance(value, bool):
                out.append((key, str(value)))
            _collect(value, keys, out)
    elif isinstance(node, list):
        for item in node:
            _collect(item, keys, out)


def _fixtures() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def test_t27_fixture_identifiers_are_fake_or_public() -> None:
    """Т27: идентификаторы фикстур входят в перечень вымышленных или публичных значений."""
    assert _fixtures(), "нет фикстур"
    unknown: list[str] = []
    for path in _fixtures():
        pairs: list[tuple[str, str]] = []
        _collect(json.loads(path.read_text(encoding="utf-8")), ID_KEYS, pairs)
        for key, value in pairs:
            # Составное значение (`<uid>_PP1`, `<master>_<n>`) проверяется по частям.
            for part in value.split("_"):
                if part in FAKE_IDS or part in PUBLIC_IDS or POINT_PART.fullmatch(part):
                    continue
                unknown.append(f"{path.name}: {key} (часть из {len(part)} символов)")
    assert not unknown, unknown


def test_t27_shop_link_ends_with_fake_id() -> None:
    """Т27: последняя группа цифр `shopLink` — вымышленный id."""
    links: list[str] = []
    for path in _fixtures():
        pairs: list[tuple[str, str]] = []
        _collect(json.loads(path.read_text(encoding="utf-8")), frozenset({"shopLink"}), pairs)
        links += [value for _, value in pairs]
    assert links, "в фикстурах нет shopLink"
    for link in links:
        match = re.search(r"(\d+)/?$", link)
        assert match is not None, "shopLink без числа в конце"
        assert match.group(1) in FAKE_IDS


def test_t27_fake_numeric_ids_start_with_nine() -> None:
    """Т27: все числовые вымышленные id начинаются с 9."""
    numeric = [value for value in FAKE_IDS if value.isdigit()]
    assert numeric
    assert all(value.startswith("9") for value in numeric)


def _denylist_lines() -> list[str]:
    assert DENYLIST.is_file(), "нет release/denylist.txt"
    return DENYLIST.read_text(encoding="utf-8").splitlines()


@needs_release
def test_t24_denylist_format() -> None:
    """Т24: заголовки К1…К9 с записями; записи не короче 5 символов, без пробелов по краям."""
    assert b"\r" not in (RELEASE / "denylist.txt").read_bytes(), "в denylist переводы строк CRLF"
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in _denylist_lines():
        if not line.strip():
            continue
        header = re.fullmatch(r"# (К[1-9])", line)
        if header:
            current = header.group(1)
            sections[current] = []
        elif line.startswith("#"):
            continue
        else:
            assert current is not None, "запись до первого заголовка категории"
            sections[current].append(line)
    assert sorted(sections) == [f"К{n}" for n in range(1, 10)]
    for name, entries in sections.items():
        assert entries, f"в категории {name} нет записей"
        for number, entry in enumerate(entries, start=1):
            assert len(entry) >= 5, f"{name}, запись {number}: короче 5 символов"
            assert entry == entry.strip(), f"{name}, запись {number}: пробелы по краям"
    entries_all = {e for entries in sections.values() for e in entries}
    assert not entries_all & (FAKE_IDS | PUBLIC_IDS), "запись denylist совпала с вымышленным id"


def test_t23_installer_docs_have_no_pipeline_references() -> None:
    """Т23: в документах установщика нет ссылок на ADR, этапы и «вопрос пользователю»."""
    patterns = [
        r"ADR ?\d{4}",
        "docs/" + "adr",
        "docs/" + "stages",
        r"[Ээ]тап[а-я]* \d+",
        r"[Вв]опрос пользователю",
    ]
    for name in ("deploy.md", "install.md"):
        text = (REPO / "docs" / name).read_text(encoding="utf-8")
        for pattern in patterns:
            assert re.search(pattern, text) is None, f"docs/{name}: {pattern}"


# --- Оверлей: workflow автозакрытия, README и CONTRIBUTING зеркала (Т18–Т22) ---

OVERLAY = REPO / "public-root" if (REPO / "public-root").is_dir() else REPO
WORKFLOW = OVERLAY / ".github" / "workflows" / "close-foreign-prs.yml"
EXPECTED_IF = (
    'if: ${{ !contains(fromJSON(\'["OWNER","MEMBER","COLLABORATOR"]\'), '
    "github.event.pull_request.author_association) && "
    "(github.event.action == 'opened' || "
    "github.event.sender.login == github.event.pull_request.user.login) }}"
)


def _workflow_lines() -> list[str]:
    assert WORKFLOW.is_file(), "нет close-foreign-prs.yml"
    return WORKFLOW.read_text(encoding="utf-8").splitlines()


def _run_block(lines: list[str]) -> list[str]:
    """Строки блока `run: |` без отступа; блок кончается на первой строке с меньшим отступом."""
    for index, line in enumerate(lines):
        if line.strip() == "run: |":
            indent = len(line) - len(line.lstrip())
            block: list[str] = []
            for body in lines[index + 1 :]:
                if body.strip() and len(body) - len(body.lstrip()) <= indent:
                    break
                block.append(body.strip())
            return [item for item in block if item]
    raise AssertionError("в workflow нет блока `run: |`")


def _section(lines: list[str], header: str, indent: int) -> list[str]:
    """Дочерние строки секции `header:` с отступом больше `indent`."""
    for index, line in enumerate(lines):
        if line == " " * indent + header + ":":
            out: list[str] = []
            for body in lines[index + 1 :]:
                if body.strip() and len(body) - len(body.lstrip()) <= indent:
                    break
                out.append(body.strip())
            return [item for item in out if item]
    raise AssertionError(f"нет секции {header}")


def test_t18_workflow_static_shape() -> None:
    """Т18: pull_request_target без чужого кода, узкие права, данные события только через env."""
    lines = _workflow_lines()
    text = "\n".join(lines)
    assert _section(lines, "on", 0) == ["pull_request_target:", "types: [opened, reopened]"]
    for banned in (
        "uses:",
        "actions/checkout",
        "pull_request.head",
        "github.head_ref",
        "pull_request.title",
        "pull_request.body",
    ):
        assert banned not in text, banned
    assert _section(lines, "permissions", 0) == ["pull-requests: write"]
    assert [line.strip() for line in lines if line.strip().startswith("if:")] == [EXPECTED_IF]
    run = _run_block(lines)
    assert run, "пустой блок run"
    assert all("${{" not in line for line in run)
    assert sum(1 for line in lines if line.strip() == "run: |") == 1
    env = _section(lines, "env", 8)
    assert env == [
        "GH_TOKEN: ${{ github.token }}",
        "PR_NUMBER: ${{ github.event.pull_request.number }}",
    ]


def test_t19_run_command_closes_pull_request(tmp_path: Path) -> None:
    """Т19: блок run вызывает gh с номером заявки, репозиторием и пояснением."""
    bash = shutil.which("bash")
    assert bash is not None
    log = tmp_path / "gh.log"
    stub = tmp_path / "bin" / "gh"
    stub.parent.mkdir()
    stub.write_text(
        '#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a" >> "$GH_LOG"; done\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {
        "PATH": f"{stub.parent}:/usr/bin:/bin",
        "GH_LOG": str(log),
        "PR_NUMBER": "42",
        "GITHUB_REPOSITORY": "onezonedata/kaspisa",
        "GITHUB_SERVER_URL": "https://github.com",
    }
    script = "\n".join(_run_block(_workflow_lines()))
    done = subprocess.run(  # noqa: S603
        [bash, "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    args = log.read_text(encoding="utf-8").splitlines()
    assert args[:6] == ["pr", "close", "42", "--repo", "onezonedata/kaspisa", "--comment"]
    comment = "\n".join(args[6:])
    assert "зеркало" in comment.lower()
    assert "Issues" in comment
    assert "https://github.com/onezonedata/kaspisa/issues" in comment


def test_t20_workflow_passes_actionlint() -> None:
    """Т20: actionlint на файле оверлея; не найден — падение, а не пропуск."""
    local = Path(sys.executable).parent / "actionlint"
    found = str(local) if local.is_file() else shutil.which("actionlint")
    assert found is not None, "actionlint не найден"
    done = subprocess.run(  # noqa: S603
        [found, str(WORKFLOW)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_t21_workflow_acts_only_in_mirror() -> None:
    """Т21: в закрытом репозитории workflow лежит только в оверлее, в зеркале — в корне."""
    workflows = REPO / ".github" / "workflows"
    if (REPO / "public-root").is_dir():
        assert not (workflows / "close-foreign-prs.yml").exists()
        for path in workflows.iterdir():
            assert "pull_request_target" not in path.read_text(encoding="utf-8"), path.name
    else:
        assert (workflows / "close-foreign-prs.yml").is_file()


def test_t22_readme_contributing_terms() -> None:
    """Т22: README и CONTRIBUTING зеркала и черновик условий."""
    readme = (OVERLAY / "README.md").read_text(encoding="utf-8")
    for needle in ("docs/install.md", "LICENSE", "TERMS.md", "CONTRIBUTING.md", "Issues"):
        assert needle in readme, needle
    for banned in ("docs/" + "product.md", "docs/" + "roadmap.md"):
        assert banned not in readme, banned
    assert "подписк" not in readme
    contributing = (OVERLAY / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "pull request" in contributing
    assert "Issues" in contributing
    first = (REPO / "TERMS.md").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("Черновик.")


# --- Скрипт выпуска release/build-mirror.sh (Т1–Т17, Т28, Т29) ---

SCRIPT = RELEASE / SCRIPT_NAME
ADR_PATH = "docs/" + "adr/"
STAGES_PATH = "docs/" + "stages/"
MAC_HOME = "/Use" + "rs/"
GITLEAKS_IMAGE = "ghcr.io/gitleaks/gitleaks:v8.21.2"
TOP_LEVEL = {
    ".claude",
    ".env.example",
    ".github",
    ".gitignore",
    ".pre-commit-config.yaml",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "Makefile",
    "README.md",
    "TERMS.md",
    "backend",
    "deploy",
    "docker-compose.prod.yml",
    "docker-compose.yml",
    "docs",
    "frontend",
    "install.sh",
    "update.sh",
}


def _version() -> str:
    text = (REPO / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def _entries() -> list[str]:
    entries = [line for line in _denylist_lines() if line.strip() and not line.startswith("#")]
    assert entries, "release/denylist.txt пуст"
    return entries


@dataclass
class Built:
    src: Path
    mirror: Path
    bare: Path
    rig: Rig
    done: subprocess.CompletedProcess[str]


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Источник выпуска: рабочий набор закрытого репозитория в свежем git-репозитории."""
    from mirror_testing import build_source

    return build_source(tmp_path_factory.mktemp("src") / "repo")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory, source: Path) -> Built:
    """Один успешный прогон: зеркало без коммитов и `origin` — локальный bare-репозиторий."""
    base = tmp_path_factory.mktemp("built")
    rig = make_rig(base / "rig")
    bare = base / "bare.git"
    git(base, "init", "-q", "--bare", str(bare))
    mirror = make_mirror(base / "mirror")
    git(mirror, "remote", "add", "origin", str(bare))
    done = run_build(source, mirror, rig)
    return Built(source, mirror, bare, rig, done)


def _fresh(
    tmp_path: Path, source: Path, *, clone: bool = False, **rig_args: Any
) -> tuple[Path, Path, Rig]:
    """Источник (общий или его копия), пустое зеркало и окружение заглушек."""
    src = clone_source(source, tmp_path / "srcclone") if clone else source
    mirror = make_mirror(tmp_path / "mirror")
    return src, mirror, make_rig(tmp_path / "rig", **rig_args)


def _assert_refused(
    done: subprocess.CompletedProcess[str], mirror: Path, before: dict[str, str], rig: Rig
) -> None:
    assert done.returncode == 1, output(done)
    assert snapshot(mirror) == before
    assert head(mirror) == ""
    assert git(mirror, "status", "--porcelain") == ""
    assert list(rig.tmp.iterdir()) == []


def _walk(root: Path) -> tuple[list[Path], list[Path]]:
    """Файлы и символьные ссылки дерева без `.git`."""
    files: list[Path] = []
    links: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            if path.is_symlink():
                links.append(path)
            elif path.is_file():
                files.append(path)
    return files, links


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


@needs_release
def test_t1_build_into_empty_mirror(built: Built) -> None:
    """Т1: один коммит выпуска, автор из зеркала, сканер вызван раз по дереву после наложения."""
    done = built.done
    assert done.returncode == 0, output(done)
    subjects = git(built.mirror, "log", "--format=%s").splitlines()
    assert subjects == [f"Выпуск v{_version()}"]
    who = "Mirror Test <mirror@example.invalid>"
    assert git(built.mirror, "log", "-1", "--format=%an <%ae>") == who
    assert git(built.mirror, "log", "-1", "--format=%cn <%ce>") == who
    scan = calls(built.rig.gitleaks_log)
    assert len(scan) == 1
    args = scan[0]
    assert args[:5] == ["dir", "--no-banner", "--redact", "--exit-code", "1"]
    assert len(args) == 6
    assert Path(args[5]).is_absolute()
    assert (built.rig.snap / "public_root").read_text(encoding="utf-8").strip() == "absent"
    expected = (built.src / "public-root" / "AGENTS.md").read_bytes()
    assert (built.rig.snap / "AGENTS.md").read_bytes() == expected
    assert list(built.rig.marks.iterdir()) == []
    assert list(built.rig.tmp.iterdir()) == []
    # Настройки сканера из окружения сняты; сканер назван в выводе.
    assert (built.rig.snap / "config_env").read_text(encoding="utf-8").strip() == "unset"
    assert (built.rig.snap / "config_toml_env").read_text(encoding="utf-8").strip() == "unset"
    assert any(line.startswith("сканер: PATH ") for line in done.stdout.splitlines()), done.stdout


@needs_release
def test_t2_mirror_composition(built: Built) -> None:
    """Т2: верхний уровень, docs, workflows, .claude и файлы оверлея."""
    assert built.done.returncode == 0, output(built.done)
    mirror, src = built.mirror, built.src
    assert {p.name for p in mirror.iterdir() if p.name != ".git"} == TOP_LEVEL
    fixtures = {
        f"docs/fixtures/kaspi/{p.name}" for p in (src / "docs" / "fixtures" / "kaspi").iterdir()
    }
    docs = {str(p.relative_to(mirror)) for p in (mirror / "docs").rglob("*") if p.is_file()}
    assert docs == {"docs/deploy.md", "docs/install.md"} | fixtures
    workflows = {p.name for p in (mirror / ".github" / "workflows").iterdir()}
    assert workflows == {"ci.yml", "release.yml", "close-foreign-prs.yml"}
    overlay = src / "public-root"
    assert tree_files(mirror / ".claude") == tree_files(overlay / ".claude")
    for name in ("AGENTS.md", "CLAUDE.md", "README.md", "CONTRIBUTING.md"):
        assert (mirror / name).read_bytes() == (overlay / name).read_bytes(), name
    assert not (mirror / "release").exists()
    assert not (mirror / "public-root").exists()


@needs_release
def test_t3_mirror_has_no_forbidden_content(built: Built) -> None:
    """Т3: записи denylist (с нормализацией), закрытые пути, ссылки и файлы окружения."""
    assert built.done.returncode == 0, output(built.done)

    def norm(text: str) -> str:
        return text.casefold().replace("ё", "е")

    def json_strings(node: Any, out: list[str]) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                out.append(str(key))
                json_strings(value, out)
        elif isinstance(node, list):
            for item in node:
                json_strings(item, out)

    entries = [(number, norm(entry)) for number, entry in enumerate(_entries(), start=1)]
    files, links = _walk(built.mirror)
    assert links == []
    problems: list[str] = []
    for path in files:
        rel = path.relative_to(built.mirror)
        assert not path.name.startswith(".env") or path.name == ".env.example", rel
        assert path.suffix != ".har", rel
        text = _text(path)
        if text is None:
            continue
        for banned in (ADR_PATH, STAGES_PATH, MAC_HOME):
            if banned in text:
                problems.append(f"{rel}: закрытый путь")
        variants = [norm(text), norm(unquote(text))]
        if path.suffix == ".json":
            strings: list[str] = []
            json_strings(json.loads(text), strings)
            variants += [norm(item) for item in strings]
        for number, entry in entries:
            if any(entry in variant for variant in variants):
                problems.append(f"запись {number}: {rel}")
    assert not problems, problems


@needs_release
def test_t4_doc_links_point_to_mirror_files(built: Built) -> None:
    """Т4: каждая ссылка на docs/*.md в зеркале указывает на существующий файл зеркала."""
    assert built.done.returncode == 0, output(built.done)
    pattern = re.compile(r"docs/[A-Za-z0-9_./-]+\.md")
    files, _ = _walk(built.mirror)
    missing: list[str] = []
    for path in files:
        text = _text(path)
        if text is None:
            continue
        for match in pattern.findall(text):
            if not (built.mirror / match).is_file():
                missing.append(f"{path.relative_to(built.mirror)}: {match}")
    assert not missing, missing


@needs_release
@pytest.mark.parametrize("case", ["no-scanner", "old-version"])
def test_t5_scanner_missing_or_wrong_version(case: str, tmp_path: Path, source: Path) -> None:
    """Т5: нет ни gitleaks, ни docker — или версия gitleaks не 8.21.2."""
    if case == "no-scanner":
        src, mirror, rig = _fresh(tmp_path, source, gitleaks=None, docker="absent")
    else:
        src, mirror, rig = _fresh(tmp_path, source, gitleaks="v8.20.0")
    before = snapshot(mirror)
    done = run_build(src, mirror, rig)
    _assert_refused(done, mirror, before, rig)
    assert "gitleaks" in done.stderr
    assert calls(rig.gitleaks_log) == []
    assert not (rig.marks / "docker").exists()


@needs_release
def test_t6_scanner_found_leak(tmp_path: Path, source: Path) -> None:
    """Т6: gitleaks вышел с кодом 1 — выпуска нет."""
    src, mirror, rig = _fresh(tmp_path, source, gitleaks_code=1)
    before = snapshot(mirror)
    done = run_build(src, mirror, rig)
    _assert_refused(done, mirror, before, rig)
    assert len(calls(rig.gitleaks_log)) == 1


@needs_release
@pytest.mark.parametrize("kind", ["denylist", "stages", "home"])
def test_t7_forbidden_string_in_allowed_file(kind: str, tmp_path: Path, source: Path) -> None:
    """Т7: запрещённая строка в разрешённом файле останавливает выпуск до сканера."""
    src, mirror, rig = _fresh(tmp_path, source, clone=True)
    entry = _entries()[0]
    planted = {
        "denylist": f"x {entry} x\n",
        "stages": "см. " + STAGES_PATH + "x\n",
        "home": "путь " + MAC_HOME + "x\n",
    }[kind]
    (src / "backend" / "tests" / "planted.txt").write_text(planted, encoding="utf-8")
    commit_all(src, "planted")
    before = snapshot(mirror)
    done = run_build(src, mirror, rig)
    _assert_refused(done, mirror, before, rig)
    assert "backend/tests/planted.txt" in output(done)
    if kind == "denylist":
        assert entry not in output(done)
    assert calls(rig.gitleaks_log) == []


@needs_release
@pytest.mark.parametrize(
    "kind",
    [
        "skill",
        "link",
        "image",
        "agents",
        "launch",
        "local-settings",
        "env-file",
        "har",
        "node-modules",
        "ignore-file",
        "pdf-upper",
        "binary",
    ],
)
def test_t8_forbidden_path_after_overlay(kind: str, tmp_path: Path, source: Path) -> None:
    """Т8: запрещённый путь в собранном дереве."""
    src, mirror, rig = _fresh(tmp_path, source, clone=True)
    overlay_claude = src / "public-root" / ".claude"
    planted: dict[str, Path] = {
        "skill": overlay_claude / "skills" / "stage-run" / "SKILL.md",
        "agents": overlay_claude / "agents" / "x.md",
        "launch": overlay_claude / "launch.json",
        "local-settings": overlay_claude / "settings.local.json",
        "env-file": src / "backend" / ".env.local",
        "har": src / "backend" / "tests" / "session.har",
        "node-modules": src / "frontend" / "node_modules" / "pkg" / "index.js",
        "ignore-file": src / "backend" / ".gitleaksignore",
        "pdf-upper": src / "backend" / "tests" / "SCAN.PDF",
    }
    if kind == "link":
        os.symlink("/etc", src / "backend" / "link")
    elif kind == "image":
        (src / "backend" / "tests" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    elif kind == "binary":
        (src / "backend" / "tests" / "data.bin").write_bytes(b"\x00\x01\x02")
    else:
        target = planted[kind]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("внутреннее\n", encoding="utf-8")
        # Часть путей в .gitignore: в источник их кладут насильно, как мог бы сделать `git add -f`.
        git(src, "add", "-f", str(target))
    commit_all(src, "forbidden")
    before = snapshot(mirror)
    done = run_build(src, mirror, rig)
    _assert_refused(done, mirror, before, rig)
    assert calls(rig.gitleaks_log) == []
    if kind == "binary":
        assert "backend/tests/data.bin" in output(done)


def _commit_in_mirror(mirror: Path) -> None:
    (mirror / "notes.txt").write_text("заметка\n", encoding="utf-8")
    commit_all(mirror, "зеркало")


def _t9_dirty_source(src: Path, tmp: Path) -> Path:
    readme = src / "backend" / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "правка\n", encoding="utf-8")
    return make_mirror(tmp / "mirror")


def _t9_dirty_mirror(src: Path, tmp: Path) -> Path:
    mirror = make_mirror(tmp / "mirror")
    (mirror / "junk.txt").write_text("мусор\n", encoding="utf-8")
    return mirror


def _t9_not_git(src: Path, tmp: Path) -> Path:
    plain = tmp / "plain"
    plain.mkdir()
    return plain


def _t9_no_email(src: Path, tmp: Path) -> Path:
    return make_mirror(tmp / "mirror", email=False)


def _t9_same_as_source(src: Path, tmp: Path) -> Path:
    return src


def _t9_ignored_env(src: Path, tmp: Path) -> Path:
    mirror = make_mirror(tmp / "mirror")
    (mirror / ".gitignore").write_text(".env\n", encoding="utf-8")
    commit_all(mirror, "gitignore")
    (mirror / ".env").write_text("MARKER-ENV\n", encoding="utf-8")
    return mirror


def _t9_ignored_nested(src: Path, tmp: Path) -> Path:
    """Игнорируемый файл во вложенной папке: на верхнем уровне его не видно."""
    mirror = make_mirror(tmp / "mirror")
    (mirror / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (mirror / "sub").mkdir()
    (mirror / "sub" / "a.txt").write_text("a\n", encoding="utf-8")
    commit_all(mirror, "зеркало")
    (mirror / "sub" / "keep.log").write_text("MARKER-LOG\n", encoding="utf-8")
    return mirror


def _t9_hidden_untracked(src: Path, tmp: Path) -> Path:
    """В настройках зеркала скрыты неотслеживаемые файлы: проверка обязана их увидеть."""
    mirror = make_mirror(tmp / "mirror")
    (mirror / ".gitignore").write_text(".env\n", encoding="utf-8")
    (mirror / "backend").mkdir()
    (mirror / "backend" / "keep.txt").write_text("keep\n", encoding="utf-8")
    commit_all(mirror, "зеркало")
    git(mirror, "config", "--local", "status.showUntrackedFiles", "no")
    (mirror / "backend" / ".env").write_text("MARKER-HIDDEN-ENV\n", encoding="utf-8")
    (mirror / "backend" / "new.txt").write_text("MARKER-NEW\n", encoding="utf-8")
    return mirror


def _t9_inside_source(src: Path, tmp: Path) -> Path:
    return make_mirror(src / "inner-mirror")


def _t9_worktree(src: Path, tmp: Path) -> Path:
    tree = tmp / "worktree"
    git(src, "worktree", "add", "-q", str(tree))
    return tree


def _t9_worktree_of_mirror(src: Path, tmp: Path) -> Path:
    """Рабочее дерево самого зеркала: общей истории с источником нет, `.git` — файл."""
    mirror = make_mirror(tmp / "mirror")
    _commit_in_mirror(mirror)
    tree = tmp / "mirror-worktree"
    git(mirror, "worktree", "add", "-q", str(tree))
    return tree


def _t9_clone_of_source(src: Path, tmp: Path) -> Path:
    clone = tmp / "closed-clone"
    git(tmp, "clone", "-q", str(src), str(clone))
    git(clone, "config", "--local", "user.name", "Mirror Test")
    git(clone, "config", "--local", "user.email", "mirror@example.invalid")
    return clone


def _t9_empty_dir(src: Path, tmp: Path) -> Path:
    mirror = make_mirror(tmp / "mirror")
    _commit_in_mirror(mirror)
    (mirror / "backups").mkdir()
    return mirror


def _t9_nested_repo(src: Path, tmp: Path) -> Path:
    mirror = make_mirror(tmp / "mirror")
    _commit_in_mirror(mirror)
    nested = mirror / "data"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "marker.txt").write_text("MARKER-NESTED\n", encoding="utf-8")
    return mirror


T9_CASES = {
    "dirty-source": _t9_dirty_source,
    "dirty-mirror": _t9_dirty_mirror,
    "not-git": _t9_not_git,
    "no-email": _t9_no_email,
    "same-as-source": _t9_same_as_source,
    "ignored-env": _t9_ignored_env,
    "ignored-nested": _t9_ignored_nested,
    "hidden-untracked": _t9_hidden_untracked,
    "inside-source": _t9_inside_source,
    "worktree": _t9_worktree,
    "worktree-of-mirror": _t9_worktree_of_mirror,
    "clone-of-source": _t9_clone_of_source,
    "empty-dir": _t9_empty_dir,
    "nested-repo": _t9_nested_repo,
}


@needs_release
@pytest.mark.parametrize("case", sorted(T9_CASES))
def test_t9_target_and_source_preconditions(case: str, tmp_path: Path, source: Path) -> None:
    """Т9: небезопасная цель или грязный источник — отказ, ничего не тронуто."""
    src = clone_source(source, tmp_path / "srcclone")
    rig = make_rig(tmp_path / "rig")
    target = T9_CASES[case](src, tmp_path)
    before = snapshot(target)
    target_head, src_head = head(target), head(src)
    done = run_build(src, target, rig)
    assert done.returncode == 1, output(done)
    assert snapshot(target) == before
    assert head(target) == target_head
    assert head(src) == src_head
    assert calls(rig.gitleaks_log) == []
    assert list(rig.tmp.iterdir()) == []
    if case == "ignored-env":
        assert (target / ".env").read_text(encoding="utf-8") == "MARKER-ENV\n"
    if case == "ignored-nested":
        assert (target / "sub" / "keep.log").read_text(encoding="utf-8") == "MARKER-LOG\n"
    if case == "hidden-untracked":
        assert (target / "backend" / ".env").read_text(encoding="utf-8") == "MARKER-HIDDEN-ENV\n"
        assert (target / "backend" / "new.txt").read_text(encoding="utf-8") == "MARKER-NEW\n"
    if case == "nested-repo":
        assert (target / "data" / "marker.txt").read_text(encoding="utf-8") == "MARKER-NESTED\n"
    if case == "empty-dir":
        assert (target / "backups").is_dir()


@needs_release
def test_t10_untracked_source_file_is_not_exported(tmp_path: Path, source: Path) -> None:
    """Т10: неотслеживаемый файл источника в зеркало не попадает."""
    src, mirror, rig = _fresh(tmp_path, source, clone=True)
    (src / "backend" / "untracked-note.txt").write_text("заметка\n", encoding="utf-8")
    done = run_build(src, mirror, rig)
    assert done.returncode == 0, output(done)
    assert not (mirror / "backend" / "untracked-note.txt").exists()


@needs_release
def test_t11_second_run_without_changes(built: Built, tmp_path: Path) -> None:
    """Т11: повторный запуск без изменений источника новых коммитов не даёт."""
    assert built.done.returncode == 0, output(built.done)
    mirror = tmp_path / "mirror"
    shutil.copytree(built.mirror, mirror, symlinks=True)
    first = head(mirror)
    done = run_build(built.src, mirror, make_rig(tmp_path / "rig"))
    assert done.returncode == 0, output(done)
    assert "изменений нет" in done.stdout
    assert head(mirror) == first
    assert git(mirror, "rev-list", "--count", "HEAD") == "1"


@needs_release
def test_t12_version_mismatch(tmp_path: Path, source: Path) -> None:
    """Т12: версия frontend/package.json не равна версии backend/pyproject.toml."""
    src, mirror, rig = _fresh(tmp_path, source, clone=True)
    package = src / "frontend" / "package.json"
    text = package.read_text(encoding="utf-8")
    assert f'"version": "{_version()}"' in text
    package.write_text(text.replace(f'"version": "{_version()}"', '"version": "0.1.1"', 1))
    commit_all(src, "version")
    before = snapshot(mirror)
    _assert_refused(run_build(src, mirror, rig), mirror, before, rig)


@needs_release
@pytest.mark.parametrize(
    "case", ["no-install-md", "comment-only", "no-denylist", "ignore", "subst", "crlf"]
)
def test_t13_source_preconditions(case: str, tmp_path: Path, source: Path) -> None:
    """Т13: нет разрешённого пути, пустой denylist, CRLF в нём, атрибуты экспорта."""
    src, mirror, rig = _fresh(tmp_path, source, clone=True)
    denylist = src / "release" / "denylist.txt"
    if case == "no-install-md":
        (src / "docs" / "install.md").unlink()
    elif case == "comment-only":
        denylist.write_text("# К1\n", encoding="utf-8")
    elif case == "no-denylist":
        denylist.unlink()
    elif case == "crlf":
        raw = denylist.read_bytes()
        assert b"\r" not in raw
        denylist.write_bytes(raw.replace(b"\n", b"\r\n"))
    else:
        attr = "export-ignore" if case == "ignore" else "export-subst"
        (src / ".gitattributes").write_text(f"backend/README.md {attr}\n", encoding="utf-8")
    commit_all(src, "case")
    before = snapshot(mirror)
    done = run_build(src, mirror, rig)
    _assert_refused(done, mirror, before, rig)
    if case == "crlf":
        assert "CRLF" in done.stderr


@needs_release
def test_t14_removed_source_file_disappears_from_mirror(
    built: Built, tmp_path: Path, source: Path
) -> None:
    """Т14: удалённый в источнике файл исчезает из зеркала новым коммитом."""
    assert built.done.returncode == 0, output(built.done)
    mirror = tmp_path / "mirror"
    shutil.copytree(built.mirror, mirror, symlinks=True)
    assert (mirror / "deploy" / "restore.sh").is_file()
    src = clone_source(source, tmp_path / "srcclone")
    (src / "deploy" / "restore.sh").unlink()
    commit_all(src, "удалён restore.sh")
    done = run_build(src, mirror, make_rig(tmp_path / "rig"))
    assert done.returncode == 0, output(done)
    assert git(mirror, "rev-list", "--count", "HEAD") == "2"
    assert not (mirror / "deploy" / "restore.sh").exists()


@needs_release
def test_t15_nothing_is_published(built: Built) -> None:
    """Т15: ни ссылок в origin, ни меток, ни вызовов gh и docker."""
    assert built.done.returncode == 0, output(built.done)
    assert git(built.bare, "for-each-ref") == ""
    assert git(built.mirror, "tag") == ""
    assert list(built.rig.marks.iterdir()) == []


@needs_release
def test_t16_overlay_command_is_verbatim() -> None:
    """Т16: команда наложения в скрипте совпадает с командой ADR 0026 символ в символ."""
    assert OVERLAY_COMMAND in SCRIPT.read_text(encoding="utf-8")


@needs_release
def test_t17_script_passes_shellcheck() -> None:
    """Т17: shellcheck скрипта; не найден — падение, а не пропуск."""
    local = Path(sys.executable).parent / "shellcheck"
    found = str(local) if local.is_file() else shutil.which("shellcheck")
    assert found is not None, "shellcheck не найден"
    done = subprocess.run(  # noqa: S603
        [found, "-x", "-s", "bash", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


@needs_release
@pytest.mark.timeout(300)
def test_t28_mirror_passes_its_own_tests(built: Built, tmp_path: Path) -> None:
    """Т28: собранное зеркало проходит тесты deploy, kaspi и pricing на своём коде."""
    assert built.done.returncode == 0, output(built.done)
    copy = tmp_path / "copy"
    shutil.copytree(built.mirror, copy, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    backend = copy / "backend"
    env = {**os.environ, "PYTHONPATH": str(backend / "src")}
    where = subprocess.run(
        [sys.executable, "-c", "import kaspisa; print(kaspisa.__file__)"],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert where.returncode == 0, where.stderr
    assert Path(where.stdout.strip()).resolve().is_relative_to(copy.resolve())
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/deploy",
            "tests/kaspi",
            "tests/pricing",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "not live",
        ],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    text = output(done)
    assert done.returncode == 0, text[-3000:]


def _tree(repo: Path) -> dict[str, str]:
    """Пути и blob id закоммиченного дерева: `путь -> id`."""
    listing = git(repo, "ls-tree", "-r", "-z", "HEAD")
    out: dict[str, str] = {}
    for item in listing.split("\0"):
        if item:
            meta, path = item.split("\t", 1)
            out[path] = meta.split()[2]
    return out


@needs_release
def test_t30_mirror_settings_do_not_change_content(tmp_path: Path, source: Path) -> None:
    """Т30: excludesFile, autocrlf и `.gitignore` зеркала не меняют состав и содержимое коммита."""
    src, mirror, rig = _fresh(tmp_path, source, clone=True)
    tests = src / "backend" / "tests"
    (tests / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    (tests / "forced.log").write_text("лог\n", encoding="utf-8")
    ignore = src / ".gitignore"
    ignore.write_text(ignore.read_text(encoding="utf-8") + "\n*.log\n", encoding="utf-8")
    git(src, "add", "-f", str(tests / "forced.log"))
    commit_all(src, "crlf и принудительный файл")
    exclude_all = tmp_path / "exclude-all"
    exclude_all.write_text("*\n", encoding="utf-8")
    git(mirror, "config", "--local", "core.excludesFile", str(exclude_all))
    git(mirror, "config", "--local", "core.autocrlf", "true")
    done = run_build(src, mirror, rig)
    assert done.returncode == 0, output(done)
    mirrored = _tree(mirror)
    expected = {
        path: blob
        for path, blob in _tree(src).items()
        if path.startswith(("backend/", "frontend/", "deploy/"))
    }
    assert "backend/tests/forced.log" in expected
    assert expected.items() <= mirrored.items()


@needs_release
@pytest.mark.parametrize("code", [0, 1])
def test_t29_gitleaks_runs_from_docker_image(code: int, tmp_path: Path, source: Path) -> None:
    """Т29: без gitleaks в PATH сканер запускается из образа: без сети, один том, только чтение."""
    src, mirror, rig = _fresh(tmp_path, source, gitleaks=None, docker=code)
    before = snapshot(mirror)
    done = run_build(src, mirror, rig)
    scan = calls(rig.docker_log)
    assert len(scan) == 1
    args = scan[0]
    stage = args[-1]
    assert Path(stage).is_absolute()
    assert args[:5] == ["run", "--rm", "--network", "none", "-v"]
    assert args[5] == f"{stage}:{stage}:ro"
    assert args[6] == GITLEAKS_IMAGE or args[6].startswith(GITLEAKS_IMAGE + "@sha256:")
    assert args[7:12] == ["dir", "--no-banner", "--redact", "--exit-code", "1"]
    assert len(args) == 13
    assert args.count("-v") == 1
    for banned in ("--mount", "-e", "--env", "--privileged"):
        assert banned not in args
    assert (rig.snap / "public_root").read_text(encoding="utf-8").strip() == "absent"
    expected = (src / "public-root" / "AGENTS.md").read_bytes()
    assert (rig.snap / "AGENTS.md").read_bytes() == expected
    if code == 0:
        assert done.returncode == 0, output(done)
        assert head(mirror) != ""
    else:
        _assert_refused(done, mirror, before, rig)
