from anki_cli.cli.commands.notetype import _default_templates


def test_default_templates_normal() -> None:
    name, front, back = _default_templates("normal")

    assert name == "Card 1"
    assert front == "{{Front}}"
    assert "{{FrontSide}}" in back
    assert "{{Back}}" in back


def test_default_templates_cloze() -> None:
    name, front, back = _default_templates("cloze")

    assert name == "Cloze"
    assert front == "{{cloze:Text}}"
    assert "{{cloze:Text}}" in back
    assert "{{Extra}}" in back


def test_default_templates_non_cloze_fallbacks_to_normal() -> None:
    name, front, back = _default_templates("anything-else")

    assert name == "Card 1"
    assert front == "{{Front}}"
    assert "{{Back}}" in back