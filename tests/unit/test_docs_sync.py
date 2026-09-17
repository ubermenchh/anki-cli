"""The exit/error code tables in README.md and SKILL.md are derived from
``models.output``; this pins that they were regenerated when the enums changed
(#28 item 3)."""

from __future__ import annotations

import re
from pathlib import Path

from anki_cli.models.output import EXIT_CODE_MEANINGS, ErrorCode, ExitCode

ROOT = Path(__file__).resolve().parents[2]


def test_skill_lists_every_error_code() -> None:
    # SKILL.md is the agent contract; README only carries the exit-code list.
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    missing = [code for code in ErrorCode if f"`{code}`" not in text]
    assert not missing, f"SKILL.md is missing error codes {missing}"


def test_skill_exit_code_table_matches_enum() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    section = text.split("### Exit Codes", 1)[1].split("###", 1)[0]
    rows = dict(re.findall(r"^\| (\d+) \| (.+?) \|$", section, flags=re.MULTILINE))
    assert {int(k) for k in rows} == {int(c) for c in ExitCode}
    for code in ExitCode:
        assert rows[str(int(code))] == EXIT_CODE_MEANINGS[code]


def test_readme_exit_code_list_matches_enum() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for code in ExitCode:
        assert f"- `{int(code)}`: {EXIT_CODE_MEANINGS[code]}" in text
