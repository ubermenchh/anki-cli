import pytest

from anki_cli.core.template import render_template


def test_render_template_replaces_plain_fields() -> None:
    template = "Front: {{Front}}\nBack: {{Back}}"
    fields = {"Front": "Q", "Back": "A"}

    assert render_template(template, fields) == "Front: Q\nBack: A"


def test_render_template_missing_field_becomes_empty_string() -> None:
    assert render_template("X{{Missing}}Y", {}) == "XY"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("details", "EXTRA=details"),
        ("", "NO_EXTRA"),
        ("   ", "NO_EXTRA"),
    ],
)
def test_render_template_sections(extra: str, expected: str) -> None:
    template = "{{#Extra}}EXTRA={{Extra}}{{/Extra}}{{^Extra}}NO_EXTRA{{/Extra}}"
    fields = {"Extra": extra}

    assert render_template(template, fields) == expected


def test_render_template_nested_sections() -> None:
    template = "{{#Outer}}O{{#Inner}}I{{/Inner}}{{/Outer}}{{^Outer}}X{{/Outer}}"

    assert render_template(template, {"Outer": "1", "Inner": "1"}) == "OI"
    assert render_template(template, {"Outer": "", "Inner": "1"}) == "X"


def test_render_template_frontside_in_back_template() -> None:
    fields = {"Front": "What is 2+2?", "Back": "4"}
    front = render_template("{{Front}}", fields)
    back = render_template("{{FrontSide}}\n{{Back}}", fields, front_side=front)

    assert back == "What is 2+2?\n4"


def test_render_template_cloze_hides_target_reveals_others() -> None:
    fields = {"Text": "{{c1::Paris}} is in {{c2::France}}"}

    question = render_template(
        "{{cloze:Text}}",
        fields,
        cloze_index=1,
        reveal_cloze=False,
    )
    answer = render_template(
        "{{cloze:Text}}",
        fields,
        cloze_index=1,
        reveal_cloze=True,
    )

    assert question == "[...] is in France"
    assert answer == "Paris is in France"


def test_render_template_cloze_hint_hidden_and_revealed() -> None:
    fields = {"Text": "{{c1::Paris::capital}}"}

    question = render_template(
        "{{cloze:Text}}",
        fields,
        cloze_index=1,
        reveal_cloze=False,
    )
    answer = render_template(
        "{{cloze:Text}}",
        fields,
        cloze_index=1,
        reveal_cloze=True,
    )

    assert question == "[capital]"
    assert answer == "Paris (capital)"


def test_render_template_cloze_without_index_hides_all() -> None:
    fields = {"Text": "{{c1::A}} {{c2::B}}"}

    assert render_template("{{cloze:Text}}", fields, reveal_cloze=False) == "[...] [...]"