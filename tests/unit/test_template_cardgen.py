"""Card-generation helpers in core/template.py, checked against the test vectors
in rslib/src/template.rs (requirements()) and template::field_is_empty."""

from __future__ import annotations

import pytest

from anki_cli.core.template import (
    TemplateParseError,
    field_is_empty,
    parse_template,
    remove_field_from_template,
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
    assert _reqs("""
{{^a}}
    {{b}}
{{/a}}

{{#a}}
    {{a}}
    {{b}}
{{/a}}
""") == ("any", [0, 1])


def test_requirements_ignore_commented_out_directives_like_rslib() -> None:
    # rslib vector: commented-out directives are comments, so only {{b}} and
    # the {{#c}} section count.
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


def test_parse_is_strict_about_sections_like_rslib() -> None:
    with pytest.raises(TemplateParseError):
        parse_template("{{/nothing}}{{a}}")  # ConditionalNotOpen
    with pytest.raises(TemplateParseError):
        parse_template("{{#b}}{{a}}")  # ConditionalNotClosed
    with pytest.raises(TemplateParseError):
        parse_template("{{#a}}{{b}}{{/c}}")  # mismatched close


def test_parse_treats_unterminated_handlebar_as_text_like_rslib() -> None:
    nodes = parse_template("{{Front}} {{Back")
    assert [n.kind for n in nodes] == ["replacement", "text"]
    assert template_renders_with_fields(nodes, {"Front"}) is True


def test_parse_comments_are_opaque_even_around_directives() -> None:
    """rslib tries a handlebar, then a comment, at each position; a directive
    inside <!-- --> is therefore a Comment node and never a directive."""
    nodes = parse_template("<!-- {{Back}} -->{{Front}}")
    assert [n.kind for n in nodes] == ["comment", "replacement"]
    assert template_renders_with_fields(nodes, {"Back"}) is False
    assert template_renders_with_fields(nodes, {"Front"}) is True


def test_parse_legacy_triple_brace_and_alt_syntax() -> None:
    assert parse_template("{{{Front}}}")[0].key == "Front"
    nodes = parse_template("{{=<% %>=}}<%Front%> and <%#Back%>x<%/Back%>")
    assert [(n.kind, n.key) for n in nodes if n.kind != "text"] == [
        ("replacement", "Front"),
        ("conditional", "Back"),
    ]


def test_remove_field_rewrites_like_rslib() -> None:
    # Replacement dropped; section on the removed field unwrapped, keeping children.
    out = remove_field_from_template(
        "{{Front}}<br>{{#B}}{{Extra}}{{/B}}{{B}}",
        {"B"},
        first_remaining_field="Front",
        is_cloze=False,
        question_side=True,
    )
    assert out == "{{Front}}<br>{{Extra}}"
    # A front left with no field replacement gets the first remaining field.
    assert (
        remove_field_from_template(
            "{{B}} hi", {"B"}, first_remaining_field="Front", is_cloze=False, question_side=True
        )
        == " hi{{Front}}"
    )
    # Backs are not padded (rslib only pads the question side) ...
    assert (
        remove_field_from_template(
            "{{B}}", {"B"}, first_remaining_field="Front", is_cloze=False, question_side=False
        )
        == ""
    )
    # ... except cloze notetypes, which must keep a cloze replacement on both sides.
    assert (
        remove_field_from_template(
            "{{cloze:B}}", {"B"}, first_remaining_field="Text", is_cloze=True, question_side=False
        )
        == "{{cloze:Text}}"
    )
    # Filters and comments round-trip; unparseable templates are left alone.
    assert (
        remove_field_from_template(
            "<!-- c -->{{text:Front}}{{hint:B}}",
            {"B"},
            first_remaining_field="Front",
            is_cloze=False,
            question_side=True,
        )
        == "<!-- c -->{{text:Front}}"
    )
    assert (
        remove_field_from_template(
            "{{#B}}oops", {"B"}, first_remaining_field="Front", is_cloze=False, question_side=True
        )
        == "{{#B}}oops"
    )


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
        ('<img src="a.png">', False),  # media counts as content
        ("[sound:a.mp3]", False),
        ("<b></b>", False),  # only br/div are ignored, as in rslib
        ("\u00a0", False),  # [[:space:]] is ASCII-only in the regex crate
        ("<p></p>", False),
    ],
)
def test_field_is_empty_matches_rslib_regex(text: str, empty: bool) -> None:
    assert field_is_empty(text) is empty
