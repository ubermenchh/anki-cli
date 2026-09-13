from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from pydantic import ValidationError

from anki_cli.models.config import AppConfig, BackendPreference, OutputFormat

_ALLOWED_BACKENDS = frozenset(get_args(BackendPreference))
_ALLOWED_OUTPUTS = frozenset(get_args(OutputFormat))

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

# ``main``'s serializer wrote every field, so files it produced still carry
# the dead standalone-backend defaults. A main-written file is identifiable
# by this stale path marker; the defaults below were never user-chosen.
_LEGACY_STANDALONE_PATH = "~/.local/share/anki-cli/collection.db"
_LEGACY_DEFAULT_PROFILE = "User 1"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    app: AppConfig
    config_path: Path
    file_data: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    app: AppConfig
    config_path: Path
    backend: str
    output_format: str
    no_color: bool
    collection_override: Path | None
    # Non-fatal notices collected while resolving (stale config values, dead
    # env vars). Surfaced through Meta.warnings so --format json consumers
    # never get a bare text line on stderr in front of the error envelope.
    warnings: list[str] = field(default_factory=list)


def resolve_runtime_config(
    *,
    cli_backend: str,
    cli_backend_set: bool,
    cli_output_format: str,
    cli_output_set: bool,
    cli_no_color: bool,
    cli_no_color_set: bool,
    cli_collection_path: Path | None,
    cli_collection_set: bool,
    env: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    loaded = load_app_config()
    values = os.environ if env is None else env
    warnings = list(loaded.warnings)

    backend = _resolve_backend(
        cli_backend=cli_backend,
        cli_backend_set=cli_backend_set,
        env_backend=values.get("ANKI_CLI_BACKEND"),
        file_backend=loaded.app.backend.prefer,
        warnings=warnings,
    )

    output_format = _resolve_output_format(
        cli_output=cli_output_format,
        cli_output_set=cli_output_set,
        env_output=values.get("ANKI_CLI_OUTPUT"),
        file_output=loaded.app.display.default_output,
    )

    color = _resolve_color(
        cli_no_color=cli_no_color,
        cli_no_color_set=cli_no_color_set,
        env_color=values.get("ANKI_CLI_COLOR"),
        file_color=loaded.app.display.color,
    )

    collection_override = _resolve_collection_override(
        cli_collection_path=cli_collection_path,
        cli_collection_set=cli_collection_set,
        env_collection=values.get("ANKI_CLI_COLLECTION"),
        file_collection=loaded.app.collection.path,
        file_data=loaded.file_data,
    )

    return RuntimeConfig(
        app=loaded.app,
        config_path=loaded.config_path,
        backend=backend,
        output_format=output_format,
        no_color=not color,
        collection_override=collection_override,
        warnings=warnings,
    )


def load_app_config(config_path: Path | None = None) -> LoadedConfig:
    path = (config_path or Path("~/.config/anki-cli/config.toml")).expanduser().resolve()

    parsed: dict[str, Any] = {}
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            raw = tomllib.loads(text)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Failed reading config file at {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError(f"Config file {path} must parse to a TOML table.")
        parsed = raw

    warnings: list[str] = []
    if parsed:
        _normalize_legacy_values(parsed, path, warnings)

    merged: dict[str, Any] = AppConfig().model_dump(mode="python")
    _deep_merge(merged, parsed)

    try:
        app = AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config values in {path}: {exc}") from exc

    return LoadedConfig(app=app, config_path=path, file_data=parsed, warnings=warnings)


def set_config_value(
    *,
    key: str,
    raw_value: str,
    config_path: Path | None = None,
) -> tuple[LoadedConfig, Any, Any]:
    loaded = load_app_config(config_path=config_path)
    merged = loaded.app.model_dump(mode="python")

    parts = _normalize_key(key)
    old_value = _get_nested(merged, parts)
    # Pydantic lax mode coerces "true"/"0"/"on" etc. for bool fields and
    # rejects scalars assigned to a section key. An empty value clears the
    # Optional fields (collection.path, collection.anki_profile) and fails
    # validation for required ones.
    new_value = raw_value.strip() or None

    _set_nested(merged, parts, new_value)

    try:
        validated = AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"Invalid value for '{key}': {exc}") from exc

    _write_config_file(loaded.config_path, validated)
    final_value = _get_nested(validated.model_dump(mode="python"), parts)

    refreshed = LoadedConfig(
        app=validated,
        config_path=loaded.config_path,
        file_data=validated.model_dump(mode="python"),
        warnings=loaded.warnings,
    )
    return refreshed, old_value, final_value


def _normalize_legacy_values(
    parsed: dict[str, Any], path: Path, warnings: list[str]
) -> None:
    """Strip values ``main`` persisted that this version must ignore.

    Every ``config:set`` on ``main`` re-serialized all defaults, so existing
    files carry ``collection.path = <standalone default>`` and
    ``collection.anki_profile = "User 1"`` — both dead defaults, not user
    choices. Left in place they act as an explicit ``--col`` to a nonexistent
    file and as a hard profile filter, reproducing the exit-3 lockout.
    """
    collection = parsed.get("collection")
    if (
        isinstance(collection, dict)
        and collection.get("path") == _LEGACY_STANDALONE_PATH
    ):
        collection.pop("path", None)
        if collection.get("anki_profile") == _LEGACY_DEFAULT_PROFILE:
            # main defaulted anki_profile to "User 1" and persisted it
            # unread; only a main-written file (stale path marker) has it
            # dropped — a user who genuinely picked "User 1" keeps it.
            collection.pop("anki_profile", None)
        warnings.append(
            f"ignoring stale collection.path in {path} left by the removed "
            "standalone backend; the next `anki config:set` rewrites the "
            "file without it"
        )

    backend = parsed.get("backend")
    if (
        isinstance(backend, dict)
        and str(backend.get("prefer", "")).strip().lower() == "standalone"
    ):
        backend["prefer"] = "auto"
        warnings.append(
            "backend.prefer 'standalone' is stale — the standalone backend "
            "was removed; using 'auto'. "
            "Run: anki config:set --key backend.prefer --value auto"
        )


def _resolve_backend(
    *,
    cli_backend: str,
    cli_backend_set: bool,
    env_backend: str | None,
    file_backend: str,
    warnings: list[str],
) -> str:
    candidate = file_backend
    if env_backend is not None:
        candidate = env_backend
    if cli_backend_set:
        candidate = cli_backend

    normalized = candidate.strip().lower()
    if normalized == "standalone":
        # Only reachable via ANKI_CLI_BACKEND: the CLI Choice rejects it and
        # _normalize_legacy_values rewrites the stale file value at load.
        warnings.append(
            "ANKI_CLI_BACKEND=standalone is stale — the standalone backend "
            "was removed; using 'auto' instead. "
            "Unset it or set it to 'auto' to silence this warning."
        )
        return "auto"
    if normalized not in _ALLOWED_BACKENDS:
        options = ", ".join(sorted(_ALLOWED_BACKENDS))
        raise ConfigError(f"Invalid backend value '{candidate}'. Expected one of: {options}.")
    return normalized


def _resolve_output_format(
    *,
    cli_output: str,
    cli_output_set: bool,
    env_output: str | None,
    file_output: str,
) -> str:
    candidate = file_output
    if env_output is not None:
        candidate = env_output
    if cli_output_set:
        candidate = cli_output

    normalized = candidate.strip().lower()
    if normalized not in _ALLOWED_OUTPUTS:
        options = ", ".join(sorted(_ALLOWED_OUTPUTS))
        raise ConfigError(f"Invalid output format '{candidate}'. Expected one of: {options}.")
    return normalized


def _resolve_color(
    *,
    cli_no_color: bool,
    cli_no_color_set: bool,
    env_color: str | None,
    file_color: bool,
) -> bool:
    color = file_color

    if env_color is not None:
        color = _parse_bool_string("ANKI_CLI_COLOR", env_color)

    if cli_no_color_set and cli_no_color:
        color = False

    return color


def _resolve_collection_override(
    *,
    cli_collection_path: Path | None,
    cli_collection_set: bool,
    env_collection: str | None,
    file_collection: str | None,
    file_data: dict[str, Any],
) -> Path | None:
    if cli_collection_set and cli_collection_path is not None:
        return cli_collection_path.expanduser().resolve()

    if env_collection is not None:
        value = env_collection.strip()
        if not value:
            raise ConfigError("ANKI_CLI_COLLECTION is set but empty.")
        return Path(value).expanduser().resolve()

    if _has_nested_key(file_data, "collection", "path") and file_collection is not None:
        # Treat "" like unset — Path("").resolve() is cwd, which detection
        # would accept and DirectBackend would then fail on with a raw
        # sqlite3 error.
        stripped = file_collection.strip()
        return Path(stripped).expanduser().resolve() if stripped else None

    return None


def _parse_bool_string(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    expected = sorted(_TRUE_VALUES | _FALSE_VALUES)
    raise ConfigError(f"Invalid boolean for {name}: '{value}'. Expected one of {expected}.")


def _deep_merge(base: dict[str, Any], updates: Mapping[str, Any]) -> None:
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, Mapping):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _has_nested_key(data: Mapping[str, Any], *keys: str) -> bool:
    current: Any = data
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            return False
        current = current[key]
    return True


def _normalize_key(key: str) -> list[str]:
    normalized = key.strip()
    if not normalized:
        raise ConfigError("Config key cannot be empty.")

    parts = [part.strip() for part in normalized.split(".")]
    if any(not part for part in parts):
        raise ConfigError(f"Invalid config key '{key}'. Use dotted keys like display.color.")
    return parts


def _get_nested(data: dict[str, Any], parts: list[str]) -> Any:
    current: Any = data
    traversed: list[str] = []

    for part in parts:
        traversed.append(part)
        if not isinstance(current, dict):
            raise ConfigError(f"'{'.'.join(traversed[:-1])}' is not a config table.")
        if part not in current:
            raise ConfigError(f"Unknown config key '{'.'.join(parts)}'.")
        current = current[part]

    return current


def _set_nested(data: dict[str, Any], parts: list[str], value: Any) -> None:
    current: dict[str, Any] = data
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            raise ConfigError(f"'{part}' is not a config table.")
        current = child
    current[parts[-1]] = value


def _write_config_file(path: Path, app_config: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _serialize_config_toml(app_config)
    try:
        tomllib.loads(content)  # never persist something we can't read back
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Refusing to write unparseable config to {path}: {exc}") from exc

    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def _serialize_config_toml(app_config: AppConfig) -> str:
    data = app_config.model_dump(mode="python")

    sections = list(data)
    lines: list[str] = []

    for idx, section in enumerate(sections):
        section_data = data.get(section, {})
        lines.append(f"[{section}]")
        for key, value in section_data.items():
            if value is None:
                continue
            lines.append(f"{key} = {_toml_scalar(value)}")
        if idx != len(sections) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"


_TOML_BASIC_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_basic_string(value: str) -> str:
    """Quote ``value`` as a TOML basic string.

    Escapes every char the spec forbids raw (C0 controls and DEL become
    ``\\uXXXX``) so a value containing e.g. a newline can never produce an
    unparseable file.
    """
    out: list[str] = []
    for ch in value:
        escape = _TOML_BASIC_ESCAPES.get(ch)
        if escape is not None:
            out.append(escape)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return _toml_basic_string(value)
    raise ConfigError(f"Unsupported TOML scalar type: {type(value).__name__}.")
