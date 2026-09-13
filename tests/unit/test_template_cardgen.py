"""Card-generation helpers in core/template.py, checked against the test vectors
in rslib/src/template.rs (requirements()) and template::field_is_empty."""

from __future__ import annotations

import pytest

from anki_cli.core.template import (
    field_is_empty,
    parse_template,
    template_renders_with_fields,
    template_requirements,
)

FIELDS = ["a", "b", "c"]


def _reqs(text: str) -> tuple[str, list[int]]:
    return template_requirements(parse_template(text), FIELDS)


def test_requirements_vectors_from_rslib() -> None:
    assert _reqs("{{a}}{{b}}") == ("any", [0, 1])
    assert _reqs("{{#a}}{{b}}{{/a}}") == ("all", [0, 1])
    assert _reqs("{{z}}") == ("none", [])
    assert _reqs("{{^a}}{{b}}{{/a}}") == ("any", [1])
    assert _reqs("{{^a}}{{#b}}{{c}}{{/b}}{{/a}}") == ("all", [1, 2])
    assert _reqs("{{#a}}{{#b}}{{a}}{{/b}}{{/a}}") == ("all", [0, 1])
    assert (
        _reqs(
            """
{{^a}}
    {{b}}
{{/a}}

{{#a}}
    {{a}}
    {{b}}
{{/a}}
"""
        )
        == ("any", [0, 1])
    )


def test_requirements_ignore_commented_out_directives_like_rslib() -> None:
    # rslib strips HTML comment delimiters wrapped directly around a directive.
    assert _reqs("<!--{{^a}}-->\n    {{b}}\n<!--{{/a}}-->\n{{#c}}{{c}}{{/c}}") == ("any", [1, 2])


def test_requirements_strip_filters_from_keys() -> None:
    assert _reqs("{{text:a}}") == ("any", [0])
    assert _reqs("{{hint:cloze:b}}") == ("any", [1])


def test_renders_with_fields_basic_and_reversed() -> None:
    front = parse_template("{{Front}}")
    back_front = parse_template("{{Back}}")
    assert template_renders_with_fields(front, {"Front"}) is True
    assert template_renders_with_fields(back_front, {"Front"}) is False
    assert template_renders_with_fields(back_front, {"Front", "Back"}) is True


def test_renders_with_fields_conditionals() -> None:
    nodes = parse_template("{{#Extra}}{{Front}}{{/Extra}}")
    assert template_renders_with_fields(nodes, {"Front"}) is False
    assert template_renders_with_fields(nodes, {"Front", "Extra"}) is True
    # Negated conditional is honoured for generation (check_negated=True)...
    neg = parse_template("{{^Extra}}{{Front}}{{/Extra}}")
    assert template_renders_with_fields(neg, {"Front"}) is True
    assert template_renders_with_fields(neg, {"Front", "Extra"}) is False
    # ...but not for the legacy reqs cache (check_negated=False), as in rslib.
    assert template_requirements(neg, ["Extra", "Front"]) == ("any", [1])


def test_renders_with_fields_text_only_front_is_empty() -> None:
    assert template_renders_with_fields(parse_template("Hello <b>world</b>"), {"a"}) is False


def test_parse_tolerates_unbalanced_close_tags() -> None:
    nodes = parse_template("{{/nothing}}{{a}}{{#b}}unclosed")
    assert template_renders_with_fields(nodes, {"a"}) is True


@pytest.mark.parametrize(
    ("text", "empty"),
    [
        ("", True),
        ("   \n\t", True),
        ("<br>", True),
        ("<br/>", True),
        ("<br />", True),
        ("<div></div>", True),
        ("<div><br></div>\n", True),
        ("a", False),
        (" x ", False),
        ("<img src=\"a.png\">", False),  # media counts as content
        ("[sound:a.mp3]", False),
        ("<b></b>", False),  # only br/div are ignored, as in rslib
        ("<p></p>", False),
    ],
)
def test_field_is_empty_matches_rslib_regex(text: str, empty: bool) -> None:
    assert field_is_empty(text) is empty
