from __future__ import annotations

import importlib
import pkgutil
from typing import Final

import click

_COMMANDS_PACKAGE: Final[str] = "anki_cli.cli.commands"

_registry: dict[str, click.Command] = {}
_discovered = False


def register_command(name: str, command: click.Command) -> None:
    if not name:
        raise ValueError("Command name cannot be empty.")
    if " " in name:
        raise ValueError(f"Command name cannot contain spaces: {name!r}")
    if name in _registry:
        raise RuntimeError(f"Command {name!r} already registered.")
    _registry[name] = command


def discover_commands() -> None:
    global _discovered
    if _discovered:
        return

    package = importlib.import_module(_COMMANDS_PACKAGE)
    if not hasattr(package, "__path__"):
        _discovered = True
        return

    for module_info in pkgutil.iter_modules(package.__path__, f"{_COMMANDS_PACKAGE}."):
        if module_info.name.endswith(".__init__"):
            continue
        importlib.import_module(module_info.name)

    _discovered = True


def list_commands() -> list[str]:
    discover_commands()
    return sorted(_registry.keys())


def get_command(name: str) -> click.Command | None:
    discover_commands()
    return _registry.get(name)
