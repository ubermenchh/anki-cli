"""The exit/error code tables in README.md and SKILL.md are derived from
``models.output``; this pins that they were regenerated when the enums changed
(#28 item 3)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from anki_cli.models.output import EXIT_CODE_MEANINGS, ErrorCode, ExitCode

ROOT = Path(__file__).resolve().parents[2]


def test_skill_lists_every_error_code() -> None:
    # SKILL.md is the agent contract; README only carries the exit-code list.
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    missing = [code for code in ErrorCode if f"`{code}`" not in text]
    assert not missing, f"SKILL.md is missing error codes {missing}"


def test_skill_exit_code_table_matches_enum() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "### Exit Codes" in text, "SKILL.md exit-code heading moved or was renamed"
    section = text.split("### Exit Codes", 1)[1].split("###", 1)[0]
    rows = dict(re.findall(r"^\| (\d+) \| (.+?) \|$", section, flags=re.MULTILINE))
    assert {int(k) for k in rows} == {int(c) for c in ExitCode}
    for code in ExitCode:
        assert rows[str(int(code))] == EXIT_CODE_MEANINGS[code]


def test_readme_exit_code_list_matches_enum() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for code in ExitCode:
        assert f"- `{int(code)}`: {EXIT_CODE_MEANINGS[code]}" in text


# --- #28: every documented invocation must be a real command with real options ---------

_INVOCATION = re.compile(r"^\s*(?:\$ )?anki\s+(.*)$", re.MULTILINE)
_GLOBAL = {"--format", "--backend", "--col", "--yes", "--copy", "--no-color", "--version", "-h",
           "--help"}


def _documented_invocations(doc: str) -> list[tuple[str, list[str]]]:
    """``(command, [--options...])`` for each ``anki ...`` line in fenced code."""
    import shlex

    text = (ROOT / doc).read_text(encoding="utf-8")
    out: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        m = _INVOCATION.match(line.split("#", 1)[0])
        if not m:
            continue
        try:
            tokens = shlex.split(m.group(1))
        except ValueError:
            continue
        # skip group options and their values to find the command token
        i = 0
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 2 if tokens[i] in {"--format", "--backend", "--col"} else 1
        if i >= len(tokens) or tokens[i] == "...":
            continue  # bare `anki`, `anki --version`, or a `anki --format json ...` placeholder
        command = tokens[i]
        opts = [t.split("=", 1)[0] for t in tokens[i + 1:] if t.startswith("--")]
        out.append((command, opts))
    return out


@pytest.mark.parametrize("doc", ["README.md", "SKILL.md"])
def test_documented_commands_and_options_exist(doc: str) -> None:
    from anki_cli.cli.dispatcher import get_command, list_commands

    known = set(list_commands())
    problems: list[str] = []
    for command, opts in _documented_invocations(doc):
        if command not in known:
            problems.append(f"{doc}: `anki {command}` is not a command")
            continue
        cmd = get_command(command)
        assert cmd is not None
        dynamic = bool(cmd.context_settings.get("ignore_unknown_options"))
        spellings = {
            s for p in cmd.params if hasattr(p, "opts") for s in (*p.opts, *p.secondary_opts)
        }
        for opt in opts:
            if opt in _GLOBAL or opt in spellings:
                continue
            if dynamic and opt[2:3].isupper():
                continue  # --Front / --Back field options on note:add / note:edit
            problems.append(f"{doc}: `anki {command}` has no option {opt}")
    assert not problems, "\n".join(problems)


def test_docs_do_not_call_the_tui_as_if_it_were_json() -> None:
    """The failure #28 opened with: docs told agents to run `--format json cards`
    while `cards` launched Textual. `cards` is JSON now; `browse` is the TUI, and
    it must never appear with --format json."""
    for doc in ("README.md", "SKILL.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert not re.search(r"--format\s+json[^\n]*\bbrowse\b", text), doc
        assert not re.search(r"\bbrowse\b[^\n]*--format\s+json", text), doc


# --- #30: the documented object shapes are the model's fields --------------------------


def test_skill_card_shape_table_names_only_real_card_fields() -> None:
    from anki_cli.models.entities import Card, DueInfo

    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "### Object Shapes" in text
    section = text.split("### Object Shapes", 1)[1].split("## Command Reference", 1)[0]
    card_table = section.split("**Card**", 1)[1].split("`due_info` tells you", 1)[0]

    documented = {
        key.strip("` ")
        for row in re.findall(r"^\| ([^|]+) \|", card_table, flags=re.MULTILINE)
        for key in row.split(",")
        if key.strip("` ") and key.strip() != "Key"
    }
    known = set(Card.model_fields) | {"field_names", "data_parsed", "left_info"}
    unknown = documented - known
    assert not unknown, f"SKILL.md documents card keys the model does not define: {unknown}"

    due_table = section.split("`due_info` tells you", 1)[1].split("**Note**", 1)[0]
    due_kinds = set(re.findall(r"^\| `(\w+)` \|", due_table, flags=re.MULTILINE)) - {"kind"}
    from typing import get_args

    assert due_kinds == set(get_args(DueInfo.model_fields["kind"].annotation))
