from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from anki_cli.models.config import AppConfig

_ALLOWED_BACKENDS = {"auto", "ankiconnect", "direct"}
_ALLOWED_OUTPUTS = {"table", "json", "md", "csv", "plain"}

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    app: AppConfig
    config_path: Path
    file_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    app: AppConfig
    config_path: Path
    backend: str
    output_format: str
    no_color: bool
    collection_override: Path | None


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

    backend = _resolve_backend(
        cli_backend=cli_backend,
        cli_backend_set=cli_backend_set,
        env_backend=values.get("ANKI_CLI_BACKEND"),
        file_backend=loaded.app.backend.prefer,
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

    merged: dict[str, Any] = AppConfig().model_dump(mode="python")
    _deep_merge(merged, parsed)

    try:
        app = AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config values in {path}: {exc}") from exc

    return LoadedConfig(app=app, config_path=path, file_data=parsed)


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
    new_value = _coerce_raw_value(raw_value, old_value)

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
    )
    return refreshed, old_value, final_value


def _resolve_backend(
    *,
    cli_backend: str,
    cli_backend_set: bool,
    env_backend: str | None,
    file_backend: str,
) -> str:
    candidate = file_backend
    if env_backend is not None:
        candidate = env_backend
    if cli_backend_set:
        candidate = cli_backend

    normalized = candidate.strip().lower()
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
    file_collection: str,
    file_data: dict[str, Any],
) -> Path | None:
    if cli_collection_set and cli_collection_path is not None:
        return cli_collection_path.expanduser().resolve()

    if env_collection is not None:
        value = env_collection.strip()
        if not value:
            raise ConfigError("ANKI_CLI_COLLECTION is set but empty.")
        return Path(value).expanduser().resolve()

    if _has_nested_key(file_data, "collection", "path"):
        return Path(file_collection).expanduser().resolve()

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


def _coerce_raw_value(raw_value: str, old_value: Any) -> Any:
    if isinstance(old_value, bool):
        return _parse_bool_string("value", raw_value)

    if isinstance(old_value, int) and not isinstance(old_value, bool):
        try:
            return int(raw_value)
        except ValueError as exc:
            raise ConfigError(f"Expected integer, got '{raw_value}'.") from exc

    if isinstance(old_value, float):
        try:
            return float(raw_value)
        except ValueError as exc:
            raise ConfigError(f"Expected float, got '{raw_value}'.") from exc

    if isinstance(old_value, str):
        return raw_value

    raise ConfigError(f"Unsupported value type for config update: {type(old_value).__name__}.")


def _write_config_file(path: Path, app_config: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _serialize_config_toml(app_config)

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
            lines.append(f"{key} = {_toml_scalar(value)}")
        if idx != len(sections) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise ConfigError(f"Unsupported TOML scalar type: {type(value).__name__}.")
