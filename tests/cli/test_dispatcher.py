from __future__ import annotations

import types
from typing import ClassVar

import click
import pytest

import anki_cli.cli.dispatcher as dispatcher_mod


def test_register_command_rejects_empty_name(monkeypatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "_registry", {})
    with pytest.raises(ValueError, match="cannot be empty"):
        dispatcher_mod.register_command("", click.Command("x"))


def test_register_command_rejects_spaces(monkeypatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "_registry", {})
    with pytest.raises(ValueError, match="cannot contain spaces"):
        dispatcher_mod.register_command("bad name", click.Command("x"))


def test_register_command_rejects_duplicates(monkeypatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "_registry", {})
    cmd = click.Command("x")

    dispatcher_mod.register_command("x", cmd)
    with pytest.raises(RuntimeError, match="already registered"):
        dispatcher_mod.register_command("x", click.Command("y"))


def test_discover_commands_noop_when_already_discovered(monkeypatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "_discovered", True)

    def fail_import(name: str):
        raise AssertionError("import_module should not be called")

    monkeypatch.setattr(dispatcher_mod.importlib, "import_module", fail_import)
    dispatcher_mod.discover_commands()


def test_discover_commands_marks_discovered_when_package_has_no_path(monkeypatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "_discovered", False)

    class PackageNoPath:
        pass

    monkeypatch.setattr(dispatcher_mod.importlib, "import_module", lambda name: PackageNoPath())

    dispatcher_mod.discover_commands()
    assert dispatcher_mod._discovered is True


def test_discover_commands_imports_non_init_modules(monkeypatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "_discovered", False)

    imported: list[str] = []

    class PackageWithPath:
        __path__: ClassVar[list[str]] = ["/fake/path"]

    def fake_import(name: str):
        imported.append(name)
        if name == dispatcher_mod._COMMANDS_PACKAGE:
            return PackageWithPath()
        return types.ModuleType(name)

    monkeypatch.setattr(dispatcher_mod.importlib, "import_module", fake_import)

    module_infos = [
        types.SimpleNamespace(name=f"{dispatcher_mod._COMMANDS_PACKAGE}.alpha"),
        types.SimpleNamespace(name=f"{dispatcher_mod._COMMANDS_PACKAGE}.__init__"),
        types.SimpleNamespace(name=f"{dispatcher_mod._COMMANDS_PACKAGE}.beta"),
    ]

    monkeypatch.setattr(
        dispatcher_mod.pkgutil,
        "iter_modules",
        lambda path, prefix: module_infos,
    )

    dispatcher_mod.discover_commands()

    assert imported == [
        dispatcher_mod._COMMANDS_PACKAGE,
        f"{dispatcher_mod._COMMANDS_PACKAGE}.alpha",
        f"{dispatcher_mod._COMMANDS_PACKAGE}.beta",
    ]
    assert dispatcher_mod._discovered is True


def test_list_commands_sorts_and_discovers(monkeypatch) -> None:
    monkeypatch.setattr(
        dispatcher_mod, 
        "_registry", 
        {"b": click.Command("b"), "a": click.Command("a")}
    )

    calls = {"count": 0}

    def fake_discover() -> None:
        calls["count"] += 1

    monkeypatch.setattr(dispatcher_mod, "discover_commands", fake_discover)

    assert dispatcher_mod.list_commands() == ["a", "b"]
    assert calls["count"] == 1


def test_get_command_discovers_and_returns(monkeypatch) -> None:
    cmd = click.Command("a")
    monkeypatch.setattr(dispatcher_mod, "_registry", {"a": cmd})

    calls = {"count": 0}

    def fake_discover() -> None:
        calls["count"] += 1

    monkeypatch.setattr(dispatcher_mod, "discover_commands", fake_discover)

    assert dispatcher_mod.get_command("a") is cmd
    assert dispatcher_mod.get_command("missing") is None
    assert calls["count"] == 2