from __future__ import annotations

from pathlib import Path

import pytest

import anki_cli.backends.detect as detect_mod
import anki_cli.config_runtime as config_runtime
from anki_cli.config_runtime import (
    ConfigError,
    LoadedConfig,
    load_app_config,
    resolve_runtime_config,
    set_config_value,
)
from anki_cli.models.config import AppConfig


def _loaded_config(
    *,
    prefer: str = "auto",
    output: str = "table",
    color: bool = True,
    collection_path: str | None = None,
    file_data: dict[str, object] | None = None,
    config_path: Path | None = None,
) -> LoadedConfig:
    app = AppConfig()
    app.backend.prefer = prefer
    app.display.default_output = output
    app.display.color = color
    app.collection.path = collection_path

    return LoadedConfig(
        app=app,
        config_path=config_path or Path("/tmp/config.toml"),
        file_data=file_data or {},
    )


def test_resolve_runtime_config_cli_overrides_env_and_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loaded = _loaded_config(
        prefer="direct",
        output="md",
        color=True,
        collection_path=str(tmp_path / "from-file.db"),
        file_data={"collection": {"path": str(tmp_path / "from-file.db")}},
        config_path=tmp_path / "config.toml",
    )

    monkeypatch.setattr(config_runtime, "load_app_config", lambda config_path=None: loaded)

    runtime = resolve_runtime_config(
        cli_backend="direct",
        cli_backend_set=True,
        cli_output_format="json",
        cli_output_set=True,
        cli_no_color=True,
        cli_no_color_set=True,
        cli_collection_path=tmp_path / "from-cli.db",
        cli_collection_set=True,
        env={
            "ANKI_CLI_BACKEND": "ankiconnect",
            "ANKI_CLI_OUTPUT": "csv",
            "ANKI_CLI_COLOR": "true",
            "ANKI_CLI_COLLECTION": str(tmp_path / "from-env.db"),
        },
    )

    assert runtime.backend == "direct"
    assert runtime.output_format == "json"
    assert runtime.no_color is True
    assert runtime.collection_override == (tmp_path / "from-cli.db").resolve()
    assert runtime.config_path == loaded.config_path


def test_resolve_runtime_config_env_overrides_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loaded = _loaded_config(
        prefer="direct",
        output="md",
        color=True,
        collection_path=str(tmp_path / "from-file.db"),
        file_data={"collection": {"path": str(tmp_path / "from-file.db")}},
    )
    monkeypatch.setattr(config_runtime, "load_app_config", lambda config_path=None: loaded)

    runtime = resolve_runtime_config(
        cli_backend="auto",
        cli_backend_set=False,
        cli_output_format="table",
        cli_output_set=False,
        cli_no_color=False,
        cli_no_color_set=False,
        cli_collection_path=None,
        cli_collection_set=False,
        env={
            "ANKI_CLI_BACKEND": "ankiconnect",
            "ANKI_CLI_OUTPUT": "csv",
            "ANKI_CLI_COLOR": "false",
            "ANKI_CLI_COLLECTION": str(tmp_path / "from-env.db"),
        },
    )

    assert runtime.backend == "ankiconnect"
    assert runtime.output_format == "csv"
    assert runtime.no_color is True
    assert runtime.collection_override == (tmp_path / "from-env.db").resolve()


def test_collection_override_from_file_only_when_key_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loaded_without_key = _loaded_config(
        collection_path=str(tmp_path / "from-model.db"),
        file_data={},
    )
    monkeypatch.setattr(
        config_runtime,
        "load_app_config",
        lambda config_path=None:
        loaded_without_key
    )

    runtime_without_key = resolve_runtime_config(
        cli_backend="auto",
        cli_backend_set=False,
        cli_output_format="table",
        cli_output_set=False,
        cli_no_color=False,
        cli_no_color_set=False,
        cli_collection_path=None,
        cli_collection_set=False,
        env={},
    )
    assert runtime_without_key.collection_override is None

    loaded_with_key = _loaded_config(
        collection_path=str(tmp_path / "from-file.db"),
        file_data={"collection": {"path": str(tmp_path / "from-file.db")}},
    )
    monkeypatch.setattr(config_runtime, "load_app_config", lambda config_path=None: loaded_with_key)

    runtime_with_key = resolve_runtime_config(
        cli_backend="auto",
        cli_backend_set=False,
        cli_output_format="table",
        cli_output_set=False,
        cli_no_color=False,
        cli_no_color_set=False,
        cli_collection_path=None,
        cli_collection_set=False,
        env={},
    )
    assert runtime_with_key.collection_override == (tmp_path / "from-file.db").resolve()


def test_resolve_runtime_config_maps_legacy_standalone_to_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale ANKI_CLI_BACKEND=standalone warns and falls back to auto
    instead of locking out every command — including config:set itself."""
    loaded = _loaded_config()
    monkeypatch.setattr(config_runtime, "load_app_config", lambda config_path=None: loaded)

    runtime = resolve_runtime_config(
        cli_backend="auto",
        cli_backend_set=False,
        cli_output_format="table",
        cli_output_set=False,
        cli_no_color=False,
        cli_no_color_set=False,
        cli_collection_path=None,
        cli_collection_set=False,
        env={"ANKI_CLI_BACKEND": "standalone"},
    )

    assert runtime.backend == "auto"
    assert any(
        "standalone" in w and "ANKI_CLI_BACKEND" in w for w in runtime.warnings
    )


def test_resolve_runtime_config_propagates_load_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warnings collected at load (e.g. the stale-path shim) ride along on
    RuntimeConfig so the CLI can put them in meta.warnings."""
    loaded = _loaded_config()
    loaded = LoadedConfig(
        app=loaded.app,
        config_path=loaded.config_path,
        file_data=loaded.file_data,
        warnings=["stale collection.path ignored"],
    )
    monkeypatch.setattr(config_runtime, "load_app_config", lambda config_path=None: loaded)

    runtime = resolve_runtime_config(
        cli_backend="auto",
        cli_backend_set=False,
        cli_output_format="table",
        cli_output_set=False,
        cli_no_color=False,
        cli_no_color_set=False,
        cli_collection_path=None,
        cli_collection_set=False,
        env={},
    )

    assert "stale collection.path ignored" in runtime.warnings


def test_load_app_config_maps_legacy_standalone_prefer(
    tmp_path: Path,
) -> None:
    """A file-level prefer="standalone" left by main normalizes to auto at
    load (before Literal validation) and says how to silence it."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[backend]\nprefer = "standalone"\n', encoding="utf-8")

    loaded = load_app_config(config_path=config_path)

    assert loaded.app.backend.prefer == "auto"
    assert any(
        "standalone" in w and "config:set" in w for w in loaded.warnings
    )


def test_load_app_config_drops_legacy_standalone_collection_defaults(
    tmp_path: Path,
) -> None:
    """The stale `path`/`anki_profile` defaults main persisted are treated as
    absent — the exact file shape that locked existing users out."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[collection]\n'
        'path = "~/.local/share/anki-cli/collection.db"\n'
        'anki_profile = "User 1"\n',
        encoding="utf-8",
    )

    loaded = load_app_config(config_path=config_path)

    assert loaded.app.collection.path is None
    assert loaded.app.collection.anki_profile is None
    assert "path" not in loaded.file_data["collection"]
    assert "anki_profile" not in loaded.file_data["collection"]
    assert any("stale collection.path" in w for w in loaded.warnings)

    # ...and the dropped path no longer acts as a --col override.
    override = config_runtime._resolve_collection_override(
        cli_collection_path=None,
        cli_collection_set=False,
        env_collection=None,
        file_collection=loaded.app.collection.path,
        file_data=loaded.file_data,
    )
    assert override is None


def test_load_app_config_keeps_profile_when_no_stale_path_marker(
    tmp_path: Path,
) -> None:
    """anki_profile="User 1" without the stale path could be deliberate —
    only a main-written file (stale path marker) has it dropped."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[collection]\nanki_profile = "User 1"\n', encoding="utf-8"
    )

    loaded = load_app_config(config_path=config_path)

    assert loaded.app.collection.anki_profile == "User 1"


def test_load_app_config_keeps_custom_profile_with_stale_path(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[collection]\n'
        'path = "~/.local/share/anki-cli/collection.db"\n'
        'anki_profile = "Work"\n',
        encoding="utf-8",
    )

    loaded = load_app_config(config_path=config_path)

    assert loaded.app.collection.path is None
    assert loaded.app.collection.anki_profile == "Work"


def test_set_config_value_drops_legacy_dead_default_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[collection]\npath = "~/.local/share/anki-cli/collection.db"\n',
        encoding="utf-8",
    )

    set_config_value(
        key="collection.anki_profile",
        raw_value="Work",
        config_path=config_path,
    )

    text = config_path.read_text(encoding="utf-8")
    assert "collection.db" not in text
    assert 'anki_profile = "Work"' in text


def test_resolve_runtime_config_invalid_env_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _loaded_config()
    monkeypatch.setattr(config_runtime, "load_app_config", lambda config_path=None: loaded)

    with pytest.raises(ConfigError, match="Invalid backend value"):
        resolve_runtime_config(
            cli_backend="auto",
            cli_backend_set=False,
            cli_output_format="table",
            cli_output_set=False,
            cli_no_color=False,
            cli_no_color_set=False,
            cli_collection_path=None,
            cli_collection_set=False,
            env={"ANKI_CLI_BACKEND": "nope"},
        )


def test_resolve_runtime_config_invalid_env_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _loaded_config()
    monkeypatch.setattr(config_runtime, "load_app_config", lambda config_path=None: loaded)

    with pytest.raises(ConfigError, match="Invalid boolean for ANKI_CLI_COLOR"):
        resolve_runtime_config(
            cli_backend="auto",
            cli_backend_set=False,
            cli_output_format="table",
            cli_output_set=False,
            cli_no_color=False,
            cli_no_color_set=False,
            cli_collection_path=None,
            cli_collection_set=False,
            env={"ANKI_CLI_COLOR": "maybe"},
        )


def test_set_config_value_round_trip_bool(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    _loaded, old_value, new_value = set_config_value(
        key="display.color",
        raw_value="false",
        config_path=config_path,
    )

    assert old_value is True
    assert new_value is False

    # The written file must carry every section header — reload refilling
    # defaults would hide a missing section otherwise.
    text = config_path.read_text(encoding="utf-8")
    for header in ("[collection]", "[backend]", "[display]"):
        assert header in text

    reloaded = load_app_config(config_path=config_path)
    assert reloaded.app.display.color is False


def test_set_anki_profile_does_not_persist_collection_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: config:set must not persist the unset collection.path.

    A written ``collection.path`` acts as an explicit --col override on the
    next run; persisting the old dead default made every command exit 3.
    """
    config_path = tmp_path / "config.toml"

    _loaded, old_value, new_value = set_config_value(
        key="collection.anki_profile",
        raw_value="Work",
        config_path=config_path,
    )

    assert old_value is None
    assert new_value == "Work"

    text = config_path.read_text(encoding="utf-8")
    assert 'anki_profile = "Work"' in text
    assert "collection.db" not in text

    reloaded = load_app_config(config_path=config_path)
    assert reloaded.app.collection.anki_profile == "Work"
    assert reloaded.app.collection.path is None

    override = config_runtime._resolve_collection_override(
        cli_collection_path=None,
        cli_collection_set=False,
        env_collection=None,
        file_collection=reloaded.app.collection.path,
        file_data=reloaded.file_data,
    )
    assert override is None

    # ...and the configured profile still resolves a real collection.
    root = tmp_path / "Anki2"
    profile_dir = root / "Work"
    profile_dir.mkdir(parents=True)
    db = profile_dir / "collection.anki2"
    db.touch()
    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [root])

    assert (
        detect_mod._require_direct_collection(
            None, reloaded.app.collection.anki_profile
        )
        == db
    )


def test_set_config_value_unknown_key_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Unknown config key"):
        set_config_value(
            key="display.no_such_key",
            raw_value="x",
            config_path=tmp_path / "config.toml",
        )


def test_set_config_value_rejects_section_key(tmp_path: Path) -> None:
    # A key naming a whole section fails validation — a scalar can't be
    # assigned to a nested model.
    with pytest.raises(ConfigError, match="Invalid value for 'display'"):
        set_config_value(
            key="display",
            raw_value="[1]",
            config_path=tmp_path / "config.toml",
        )


def test_set_config_value_rejects_invalid_enum_value(tmp_path: Path) -> None:
    # Literal-typed fields reject bad writes at set time instead of failing
    # every subsequent run.
    with pytest.raises(ConfigError, match="Invalid value"):
        set_config_value(
            key="backend.prefer",
            raw_value="direkt",
            config_path=tmp_path / "config.toml",
        )

    with pytest.raises(ConfigError, match="Invalid value"):
        set_config_value(
            key="display.default_output",
            raw_value="xml",
            config_path=tmp_path / "config.toml",
        )


def test_set_config_value_empty_string_clears_optional_field(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    set_config_value(
        key="collection.anki_profile", raw_value="Work", config_path=config_path
    )
    assert 'anki_profile = "Work"' in config_path.read_text(encoding="utf-8")

    _loaded, old_value, new_value = set_config_value(
        key="collection.anki_profile", raw_value="", config_path=config_path
    )

    assert old_value == "Work"
    assert new_value is None
    assert "anki_profile" not in config_path.read_text(encoding="utf-8")


def test_collection_path_empty_string_is_unset() -> None:
    override = config_runtime._resolve_collection_override(
        cli_collection_path=None,
        cli_collection_set=False,
        env_collection=None,
        file_collection="",
        file_data={"collection": {"path": ""}},
    )
    assert override is None


def test_set_config_value_escapes_control_chars(tmp_path: Path) -> None:
    """A value with control chars must serialize as an escaped basic string —
    never an unparseable file that locks out every later command."""
    config_path = tmp_path / "config.toml"

    set_config_value(
        key="collection.anki_profile",
        raw_value="Work\nHome\x01",
        config_path=config_path,
    )

    text = config_path.read_text(encoding="utf-8")
    assert "\\n" in text       # named escape for the newline
    assert "\\u0001" in text   # non-named C0 chars become \uXXXX

    loaded = load_app_config(config_path=config_path)
    assert loaded.app.collection.anki_profile == "Work\nHome\x01"


def test_set_config_value_rejects_unknown_section(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Unknown config key"):
        set_config_value(
            key="review.max_answer_seconds",
            raw_value="60",
            config_path=tmp_path / "config.toml",
        )


def test_load_app_config_invalid_toml_raises(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[display\ncolor = true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Failed reading config file"):
        load_app_config(config_path=config_path)
