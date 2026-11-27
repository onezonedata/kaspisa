"""Тесты файлов установщика: настройки агента, правила, скиллы, лицензия, хук (ADR 0026).

Корень файлов установщика — `public-root/` в закрытом репозитории и корень в зеркале.
База и сеть не нужны.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from deploy_testing import REPO

INSTALLER = REPO / "public-root" if (REPO / "public-root").is_dir() else REPO
HOOK = INSTALLER / ".claude" / "hooks" / "kaspisa-guard.sh"
SKILLS = ("kaspisa-install", "kaspisa-update", "kaspisa-backup", "kaspisa-server")
MAC_HOME = "/Use" + "rs/"  # склейка: сам тест не должен попасть под проверку на утечку путей
LICENSE_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"

A13_CHECK = (
    "bash -c 'compgen -e' | grep -E '^(KASPISA_DRY_RUN|KASPISA_VERSION|KASPISA_IMAGE_PREFIX|"
    "COMPOSE_PROFILES|COMPOSE_FILE|COMPOSE_ENV_FILES|POSTGRES_.*)$'"
)

OVERLAY_COMMAND = (
    'rm -rf "$MIRROR/AGENTS.md" "$MIRROR/CLAUDE.md" "$MIRROR/.claude" '
    '"$MIRROR/docs/stages" "$MIRROR/docs/adr" && '
    'cp -R "$MIRROR/public-root/." "$MIRROR/" && rm -rf "$MIRROR/public-root"'
)


def _settings() -> dict[str, Any]:
    data = json.loads((INSTALLER / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _rules(kind: str) -> list[str]:
    rules = _settings()["permissions"][kind]
    assert isinstance(rules, list)
    return [str(rule) for rule in rules]


def _public_texts() -> list[Path]:
    paths = [INSTALLER / "AGENTS.md", REPO / "docs" / "install.md", REPO / "TERMS.md"]
    paths += sorted((INSTALLER / ".claude" / "skills").glob("*/SKILL.md"))
    return paths


# --- Т1–Т3, Т26: settings.json ---


def test_installer_settings_deny_env_access() -> None:
    deny = _rules("deny")
    expected = [
        "Read(./.env)",
        "Read(./.env.*)",
        "Read(./backend/.env)",
        "Read(./frontend/.env.local)",
        "Read(./backups/*.dump)",
        "Edit(./.env)",
        "Edit(./.env.*)",
    ]
    for rule in expected:
        assert rule in deny, rule
    # Правила Edit закрывают все инструменты правки; Write(путь) Claude Code не сверяет
    # и печатает предупреждение при каждом запуске (2.1.330, проверено на этапе 17).
    assert not [rule for rule in deny if rule.startswith("Write(")]
    assert _settings()["permissions"]["disableBypassPermissionsMode"] == "disable"


def test_installer_settings_deny_dangerous_bash() -> None:
    deny = _rules("deny")
    expected = [
        "Bash(make show-key:*)",
        "Bash(python -m kaspisa.api.add_user:*)",
        "Bash(python -m kaspisa.api.reset_token:*)",
        "Bash(docker inspect:*)",
        "Bash(docker compose config:*)",
        "Bash(docker compose -f docker-compose.prod.yml config:*)",
        "Bash(docker exec:*)",
        "Bash(docker compose exec:*)",
        "Bash(docker compose -f docker-compose.prod.yml exec:*)",
        "Bash(env)",
        "Bash(printenv:*)",
        "Bash(cat .env:*)",
        "Bash(make restore:*)",
        "Bash(bash deploy/restore.sh:*)",
        "Bash(docker compose convert:*)",
        "Bash(docker compose -f docker-compose.prod.yml convert:*)",
    ]
    for rule in expected:
        assert rule in deny, rule


def _bash_inner(rule: str) -> str | None:
    match = re.fullmatch(r"Bash\((.*)\)", rule)
    if match is None:
        return None
    inner = match.group(1)
    return inner.removesuffix(":*")


def test_installer_settings_allow_is_narrow() -> None:
    raw = (INSTALLER / ".claude" / "settings.json").read_text(encoding="utf-8")
    for forbidden in (MAC_HOME, "/home/", "bypassPermissions", "defaultMode"):
        assert forbidden not in raw, forbidden
    allow = _rules("allow")
    deny = _rules("deny")
    assert "Bash(*)" not in allow
    assert "Bash(docker:*)" not in allow
    assert not [rule for rule in allow if "logs" in rule]
    for a_rule in allow:
        a_inner = _bash_inner(a_rule)
        if a_inner is None:
            continue
        for d_rule in deny:
            d_inner = _bash_inner(d_rule)
            if d_inner is None:
                continue
            assert not a_inner.startswith(d_inner), (a_rule, d_rule)
            assert not d_inner.startswith(a_inner), (a_rule, d_rule)


def test_installer_hook_is_registered() -> None:
    entries = _settings()["hooks"]["PreToolUse"]
    bash_entries = [entry for entry in entries if entry.get("matcher") == "Bash"]
    assert bash_entries
    commands = [hook["command"] for entry in bash_entries for hook in entry["hooks"]]
    script = "$CLAUDE_PROJECT_DIR/.claude/hooks/kaspisa-guard.sh"
    assert any(command.startswith("bash ") and script in command for command in commands)


# --- Т4–Т7: AGENTS.md, CLAUDE.md, отсутствие файлов Codex и OpenCode, скиллы ---


def test_installer_agents_rules_numbered_cyrillic() -> None:
    text = (INSTALLER / "AGENTS.md").read_text(encoding="utf-8")
    heads = re.findall(r"^([АA])(\d+)\.", text, flags=re.MULTILINE)
    assert [int(number) for _, number in heads] == list(range(1, 14))
    assert all(letter == "\u0410" for letter, _ in heads)
    rules = {
        int(num): line
        for line in text.splitlines()
        if (m := re.match(r"^[АA](\d+)\.", line)) and (num := m.group(1))
    }
    anchors = {
        1: [".env", "backups/*.dump", "git grep"],
        3: ["KASPISA_DRY_RUN=false", "live"],
        4: ["веб-интерфейс"],
        5: ["окружени", "чат", "скилл"],
        6: ["printenv", "declare -p", "/usr/bin/env", "bash -c 'compgen -e'"],
        7: [
            "make show-key",
            "docker compose config",
            "docker compose convert",
            "docker inspect",
            "kaspisa.api.add_user",
            "kaspisa.api.reset_token",
        ],
        8: ["--yes"],
        9: ["id -u"],
        13: ["bash -c 'compgen -e'"],
    }
    for number, parts in anchors.items():
        for part in parts:
            assert part in rules[number], (number, part)


def test_installer_claude_md_imports_agents() -> None:
    assert (INSTALLER / "CLAUDE.md").read_text(encoding="utf-8").strip() == "@AGENTS.md"


def test_installer_has_no_codex_or_opencode_files() -> None:
    for name in (".codex", "opencode.json", "opencode.jsonc", ".opencode", ".agents"):
        assert not (INSTALLER / name).exists(), name


def test_installer_skills_are_four_with_headers() -> None:
    skills_dir = INSTALLER / ".claude" / "skills"
    names = sorted(path.name for path in skills_dir.iterdir() if path.is_dir())
    assert names == sorted(SKILLS)
    for name in names:
        text = (skills_dir / name / "SKILL.md").read_text(encoding="utf-8")
        header = re.match(r"---\n(.*?)\n---\n", text, flags=re.DOTALL)
        assert header is not None, name
        fields = dict(line.split(":", 1) for line in header.group(1).splitlines() if ":" in line)
        assert fields["name"].strip() == name
        assert fields["description"].strip()
    agents = (INSTALLER / "AGENTS.md").read_text(encoding="utf-8")
    for name in SKILLS:
        assert f".claude/skills/{name}/SKILL.md" in agents, name
    install = (skills_dir / "kaspisa-install" / "SKILL.md").read_text(encoding="utf-8")
    assert "id -u" in install
    assert A13_CHECK in install
    backup = (skills_dir / "kaspisa-backup" / "SKILL.md").read_text(encoding="utf-8")
    assert A13_CHECK in backup


# --- Т8: ссылки в текстах ---


def _makefile_targets() -> set[str]:
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Za-z0-9_.-]+):", text, flags=re.MULTILINE))


def test_installer_texts_reference_existing_targets_and_paths() -> None:
    targets = _makefile_targets()
    path_re = re.compile(
        r"(?:install\.sh|update\.sh|deploy/[^\s`'\"()]+\.sh|docs/[^\s`'\"()]+\.md|"
        r"\.claude/skills/[^\s`'\"()]+/SKILL\.md|\.claude/hooks/[^\s`'\"()]+\.sh|"
        r"LICENSE|TERMS\.md)"
    )
    problems: list[str] = []
    for path in _public_texts():
        text = path.read_text(encoding="utf-8")
        for span in re.findall(r"`([^`\n]+)`", text):
            for match in re.finditer(r"\bmake ([A-Za-z0-9_.-]+)", span):
                if match.group(1) not in targets:
                    problems.append(f"{path.name}: make {match.group(1)}")
            for token in span.split():
                token = token.strip(",.;:")
                if any(ch in token for ch in "*<~$") or token.startswith("/"):
                    continue
                if not path_re.fullmatch(token):
                    continue
                base = INSTALLER if token.startswith(".claude/") else REPO
                if not (base / token).exists():
                    problems.append(f"{path.name}: {token}")
        # Команды в блоках кода (строки с отступом) тоже должны указывать на цели Makefile.
        for line in text.splitlines():
            block = re.match(r"^\s*make ([A-Za-z0-9_.-]+)", line)
            if block and block.group(1) not in targets:
                problems.append(f"{path.name}: make {block.group(1)}")
        for banned in ("docs/" + "adr/", "docs/" + "stages/"):
            if banned in text:
                problems.append(f"{path.name}: {banned}")
    assert not problems, problems


# --- Т9, Т10: LICENSE и TERMS.md ---


def test_installer_license_is_verbatim_agpl() -> None:
    # Источник: https://www.gnu.org/licenses/agpl-3.0.txt, скачан 19.11.2026;
    # хэш снят с официального источника, а не с файла в репозитории.
    data = (REPO / "LICENSE").read_bytes()
    assert b"\r" not in data
    text = data.decode("utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text
    assert hashlib.sha256(data).hexdigest() == LICENSE_SHA256


def test_installer_terms_have_eight_sections() -> None:
    text = (REPO / "TERMS.md").read_text(encoding="utf-8")
    numbers = re.findall(r"^#{1,6}\s*(\d+)\.", text, flags=re.MULTILINE)
    assert [int(n) for n in numbers if 1 <= int(n) <= 8] == list(range(1, 9))


# --- Т11: корневой AGENTS.md конвейера ---


def test_pipeline_agents_forbids_secret_commands() -> None:
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    for phrase in ("make show-key", "docker compose config", "docker inspect"):
        assert phrase in text, phrase


# --- Т24, Т25: хук ---


def _run_hook(command: str, description: str = "Run command") -> subprocess.CompletedProcess[str]:
    event = {
        "session_id": "s",
        "transcript_path": "/home/kaspisa/t.jsonl",
        "cwd": "/home/kaspisa/kaspisa",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": description},
    }
    payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    return subprocess.run(  # noqa: S603
        ["bash", str(HOOK)],  # noqa: S607
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


DENIED = [
    "cat .env",
    'bash -c "cat .env"',
    "cat ./.env",
    "head -n 3 backend/.env",
    "grep KEY .env.tmp.123",
    "sed -i 's/true/false/' .env",
    "echo 'KASPISA_DRY_RUN=false' >> .env",
    "sed -i s/DRY_RUN=true/DRY_RUN=0/ x",
    "cat /proc/self/environ",
    'python3 -c "import os; print(os.environ)"',
    "env",
    "env | sort",
    "printenv KASPISA_ENCRYPTION_KEY",
    "export -p",
    "make show-key",
    "docker inspect kaspisa-api-1",
    "docker compose config",
    "docker-compose config",
    "docker compose -p x --env-file y config",
    "docker compose -f a -f b exec api sh",
    "docker exec -it x sh",
    "docker run -v /:/h alpine",
    "docker cp x:/a .",
    "make restore FILE=backups/a.dump",
    "bash deploy/restore.sh --yes x",
    "cd ~/kaspisa\nenv | sort",
    "ls\tenv",
    'docker compose -f "docker-compose.prod.yml" config',
    'docker compose -f "docker-compose.prod.yml" exec api sh',
    "docker compose -f docker-compose.prod.yml convert",
    "set",
    "set | grep TOKEN",
    "export",
    "declare",
    "typeset",
    "declare -p",
    "typeset -p HCLOUD_TOKEN",
    "/usr/bin/env",
    "ps auxeww",
    "ps e",
    "sudo ps axe",
    "grep -rn KASPISA .",
    "grep --recursive KEY .",
    "cd ~/kaspisa && grep -R KEY",
    "cat backups/manual-v0.0.2.dump | strings",
    "python -m kaspisa.api.add_user a@example.kz b@example.kz",
    "python3 -m kaspisa.api.reset_token a@example.kz",
    "uv run python -m kaspisa.api.add_user a@example.kz",
    "cd backend && python -m kaspisa.api.reset_token x",
    "cd backend\nuv run python -m kaspisa.api.reset_token x",
    "docker compose -f docker-compose.prod.yml run --rm api python -m kaspisa.api.reset_token x",
    "docker compose -f docker-compose.prod.yml exec api python -m kaspisa.api.add_user a@b.kz",
    "python backend/src/kaspisa/api/add_user.py a@example.kz",
    "python -m kaspisa.api.reset_token",
]

ALLOWED: list[tuple[str, str]] = [
    ("bash install.sh tunnel", "Run command"),
    (
        "KASPISA_DOMAIN=shop.example.kz KASPISA_CADDY_TLS=a@example.kz bash install.sh public",
        "Run command",
    ),
    ("bash update.sh v0.0.2-s15", "Run command"),
    ("make backup", "Run command"),
    ("ls -lt backups/", "List backups to restore from"),
    ("tail -n 50 backups/update-20261119T000000.log", "Run command"),
    ("docker compose -f docker-compose.prod.yml ps", "Run command"),
    ("docker compose -f docker-compose.prod.yml logs --tail 50 api", "Run command"),
    ("git status", "Run command"),
    ("git describe --tags", "Run command"),
    ("compgen -e", "List env variable names"),
    (A13_CHECK, "Check environment for conflicting env variable names"),
    ("sudo sshd -T -C user=kaspisa,host=h,addr=203.0.113.5", "Run command"),
    ("ss -ltn", "Run command"),
    ("set -euo pipefail", "Run command"),
    ("export KASPISA_DOMAIN=shop.example.kz", "Run command"),
    ("git restore docs/install.md", "Run command"),
    ("grep -i environment docs/deploy.md", "Run command"),
    ("ps aux", "Run command"),
    ("ps -ef", "Run command"),
    ("ps -C postgres -o pid,cmd", "Run command"),
    ("ps -u kaspisa -o pid,user,cmd", "Run command"),
    ("git grep -n KASPISA_UPDATE_REPO", "Run command"),
    ("rg -n KASPISA_UPDATE_REPO docs", "Run command"),
    ('docker compose -f "docker-compose.prod.yml" ps', "Run command"),
    ("cd ~/kaspisa\nbash update.sh v0.0.2-s15", "Run command"),
    ("docker compose -f docker-compose.prod.yml ps\ngit status", "Run command"),
    ("git grep add_user", "Run command"),
    ("ls backend/src/kaspisa/api", "Run command"),
    ("git grep -n reset_token docs", "Run command"),
    ("python -m kaspisa.api.openapi", "Run command"),
]


@pytest.mark.parametrize("command", DENIED)
def test_installer_hook_denies_dangerous_command(command: str) -> None:
    done = _run_hook(command)
    assert done.returncode == 2, done.stderr
    assert done.stderr.strip()
    assert command not in done.stderr


def test_installer_hook_denies_when_json_event_is_not_parsable() -> None:
    # Сбой разбора события: хук проверяет весь ввод и всё равно отклоняет.
    done = subprocess.run(  # noqa: S603
        ["bash", str(HOOK)],  # noqa: S607
        input="not json: cat .env",
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 2, done.stderr


def test_installer_hook_ignores_secret_words_in_description() -> None:
    # Описание команды не должно вызывать отказ: проверяется только поле command.
    done = _run_hook("ls -lt backups/", "Explain why cat .env is forbidden")
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(("command", "description"), ALLOWED)
def test_installer_hook_allows_legitimate_command(command: str, description: str) -> None:
    done = _run_hook(command, description)
    assert done.returncode == 0, done.stderr


def test_installer_hook_is_short() -> None:
    assert len(HOOK.read_text(encoding="utf-8").splitlines()) <= 35


def test_installer_hook_passes_shellcheck() -> None:
    # shellcheck ставит зависимость shellcheck-py рядом с интерпретатором; не найден — падение.
    local = Path(sys.executable).parent / "shellcheck"
    found = str(local) if local.is_file() else shutil.which("shellcheck")
    assert found is not None, "shellcheck не найден"
    done = subprocess.run(  # noqa: S603
        [found, "-s", "bash", str(HOOK)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_installer_texts_forbid_no_prompt_modes() -> None:
    for path in (INSTALLER / "AGENTS.md", REPO / "docs" / "install.md"):
        text = path.read_text(encoding="utf-8")
        assert "--dangerously-skip-permissions" in text, path.name


# --- Т31: дерево зеркала после команды наложения ---


def test_installer_overlay_builds_clean_mirror_tree(tmp_path: Path) -> None:
    if not (REPO / "public-root").is_dir():
        pytest.skip("раскладка зеркала: public-root отсутствует")
    git = shutil.which("git")
    if git is None:
        pytest.skip("нет git")
    listing = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z", "-co", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout.decode("utf-8")
    mirror = tmp_path / "mirror"
    skip_prefixes = (".claude/", "docs/" + "stages/", "docs/" + "adr/", "release/")
    for rel in filter(None, listing.split("\0")):
        if rel in ("AGENTS.md", "CLAUDE.md") or rel.startswith(skip_prefixes):
            continue
        source = REPO / rel
        if not source.is_file():
            continue
        target = mirror / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # Корневые файлы конвейера нужны, чтобы команда наложения их удаляла.
    (mirror / "AGENTS.md").write_text("pipeline", encoding="utf-8")
    (mirror / "CLAUDE.md").write_text("pipeline", encoding="utf-8")
    for pipeline_skill in ("stage-run", "debate", "kaspi-api", "kaspi-capture"):
        skill = mirror / ".claude" / "skills" / pipeline_skill
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("pipeline", encoding="utf-8")
    (mirror / "docs" / "stages").mkdir(parents=True, exist_ok=True)
    (mirror / "docs" / "adr").mkdir(parents=True, exist_ok=True)

    subprocess.run(  # noqa: S603
        ["bash", "-c", OVERLAY_COMMAND],  # noqa: S607
        env={"MIRROR": str(mirror), "PATH": "/usr/bin:/bin"},
        check=True,
        timeout=120,
    )

    assert not (mirror / "public-root").exists()
    assert not (mirror / "docs" / "stages").exists()
    for pipeline_skill in ("stage-run", "debate", "kaspi-api", "kaspi-capture"):
        assert not (mirror / ".claude" / "skills" / pipeline_skill).exists()
    mirrored = sorted(
        p.relative_to(mirror / ".claude").as_posix()
        for p in (mirror / ".claude").rglob("*")
        if p.is_file()
    )
    overlay = sorted(
        p.relative_to(REPO / "public-root" / ".claude").as_posix()
        for p in (REPO / "public-root" / ".claude").rglob("*")
        if p.is_file()
    )
    assert mirrored == overlay
    leaks = []
    for path in mirror.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if MAC_HOME in content:
            leaks.append(path.relative_to(mirror).as_posix())
    assert not leaks, leaks
    assert (mirror / "AGENTS.md").read_bytes() == (REPO / "public-root" / "AGENTS.md").read_bytes()
