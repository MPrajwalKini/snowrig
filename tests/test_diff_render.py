"""Tests for FieldDiff.render() — unified-diff-style rendering of list-of-
dict field changes (columns, etc.), with git-diff -U<context> style
context windowing: unchanged items are only shown within `context` lines
of an actual change, longer unchanged runs collapse to a single '...'
marker.
"""

from __future__ import annotations

from snowrig.manifest.diff import FieldDiff


def _col(name: str, datatype: str) -> dict:
    return {"name": name, "datatype": datatype}


# --------------------------------------------------------------------- #
# Small lists — no context collapsing needed, everything shown
# --------------------------------------------------------------------- #

def test_render_small_list_shows_everything_no_collapsing():
    live = [_col("ID", "NUMBER"), _col("EMAIL", "VARCHAR(255)")]
    desired = [_col("ID", "NUMBER"), _col("EMAIL", "VARCHAR(255)"), _col("PHONE", "VARCHAR(20)")]
    fd = FieldDiff(live=live, desired=desired)

    rendered = fd.render(context=2)

    assert "ID NUMBER" in rendered
    assert "EMAIL VARCHAR(255)" in rendered
    assert "+ PHONE VARCHAR(20)" in rendered
    assert "..." not in rendered


def test_render_scalar_field_falls_back_to_plain_arrow():
    fd = FieldDiff(live="old comment", desired="new comment")

    assert fd.render() == "'old comment' -> 'new comment'"


# --------------------------------------------------------------------- #
# Context windowing — the actual feature being tested
# --------------------------------------------------------------------- #

def test_render_collapses_unchanged_columns_beyond_context_distance():
    """10 columns, only LABEL changed in the middle — with context=2,
    columns more than 2 away from the change should collapse."""
    live = [_col(f"C{i}", "VARCHAR(10)") for i in range(10)]
    live[5] = _col("LABEL", "VARCHAR(100)")
    desired = [_col(f"C{i}", "VARCHAR(10)") for i in range(10)]
    desired[5] = _col("LABEL", "VARCHAR(150)")
    fd = FieldDiff(live=live, desired=desired)

    rendered = fd.render(context=2)
    lines = rendered.splitlines()

    assert "- LABEL VARCHAR(100)" in lines
    assert "+ LABEL VARCHAR(150)" in lines
    assert "..." in rendered
    # Exactly 2 context columns kept above and below the change.
    assert "  C3 VARCHAR(10)" in lines
    assert "  C4 VARCHAR(10)" in lines
    assert "  C6 VARCHAR(10)" in lines
    assert "  C7 VARCHAR(10)" in lines
    # Further-away columns must NOT appear individually — only via the
    # collapsed marker.
    assert "  C0 VARCHAR(10)" not in lines
    assert "  C9 VARCHAR(10)" not in lines


def test_render_context_zero_shows_only_changed_lines():
    live = [_col(f"C{i}", "VARCHAR(10)") for i in range(6)]
    live[3] = _col("LABEL", "VARCHAR(100)")
    desired = [_col(f"C{i}", "VARCHAR(10)") for i in range(6)]
    desired[3] = _col("LABEL", "VARCHAR(150)")
    fd = FieldDiff(live=live, desired=desired)

    rendered = fd.render(context=0)
    lines = rendered.splitlines()

    assert lines == [
        "  ... (3 unchanged columns) ...",
        "- LABEL VARCHAR(100)",
        "+ LABEL VARCHAR(150)",
        "  ... (2 unchanged columns) ...",
    ]


def test_render_two_nearby_changes_share_context_without_double_collapsing():
    """A removal and an addition close enough together that their context
    windows overlap should merge into one visible block, not two separate
    collapsed regions with a gap."""
    live = [_col(f"C{i}", "VARCHAR(10)") for i in range(10)]
    live.insert(5, {"name": "TEMP_COL", "datatype": "VARCHAR(20)"})
    desired = [_col(f"C{i}", "VARCHAR(10)") for i in range(10)]
    desired.insert(6, {"name": "STATUS", "datatype": "VARCHAR(20)"})
    fd = FieldDiff(live=live, desired=desired)

    rendered = fd.render(context=2)
    marker_lines = [l for l in rendered.splitlines() if l.strip().startswith("...")]

    assert "- TEMP_COL VARCHAR(20)" in rendered
    assert "+ STATUS VARCHAR(20)" in rendered
    # Only one collapse marker on each side of the change cluster, not one
    # per individual change within it.
    assert len(marker_lines) == 2


def test_render_large_context_shows_everything_equivalent_to_no_collapsing():
    live = [_col(f"C{i}", "VARCHAR(10)") for i in range(20)]
    live[10] = _col("LABEL", "VARCHAR(100)")
    desired = [_col(f"C{i}", "VARCHAR(10)") for i in range(20)]
    desired[10] = _col("LABEL", "VARCHAR(150)")
    fd = FieldDiff(live=live, desired=desired)

    rendered = fd.render(context=100)

    assert "..." not in rendered
    # 20 columns, one of which is a change rendered as 2 lines (old+new) ->
    # 21 total lines, 20 newlines between them.
    assert rendered.count("\n") == 20


# --------------------------------------------------------------------- #
# Item formatting
# --------------------------------------------------------------------- #

def test_render_added_item_with_single_extra_field_shows_bare_value():
    fd = FieldDiff(live=[], desired=[_col("STATUS", "VARCHAR(20)")])

    assert "+ STATUS VARCHAR(20)" in fd.render()


def test_render_added_item_with_multiple_extra_fields_shows_key_value_pairs():
    fd = FieldDiff(
        live=[],
        desired=[{"name": "STATUS", "datatype": "VARCHAR(20)", "nullable": False}],
    )

    rendered = fd.render()

    assert "STATUS" in rendered
    assert "datatype=VARCHAR(20)" in rendered
    assert "nullable=False" in rendered


def test_render_changed_item_shows_full_old_and_new_lines_as_a_pair():
    """A changed item renders as the complete old line immediately
    followed by the complete new line — not just the differing sub-field
    — matching a PR 'suggested change' diff's before/after pair style."""
    live = [{"name": "LABEL", "datatype": "VARCHAR(100)", "nullable": True}]
    desired = [{"name": "LABEL", "datatype": "VARCHAR(150)", "nullable": True}]
    fd = FieldDiff(live=live, desired=desired)

    rendered = fd.render()
    lines = rendered.splitlines()

    assert len(lines) == 2
    assert lines[0].startswith("- LABEL")
    assert lines[1].startswith("+ LABEL")


def test_render_unchanged_declared_subfields_do_not_trigger_a_spurious_change():
    """Only the declared sub-fields that actually differ should trigger a
    change at all — a column whose only declared sub-field is unchanged
    must render as context, not as a no-op '- X / + X' pair."""
    live = [{"name": "LABEL", "datatype": "VARCHAR(100)"}]
    desired = [{"name": "LABEL", "datatype": "VARCHAR(100)"}]
    fd = FieldDiff(live=live, desired=desired)

    rendered = fd.render()

    assert "+" not in rendered
    assert "-" not in rendered
    assert rendered.strip() == "LABEL VARCHAR(100)"


def test_render_with_zero_changes_shows_everything_plainly_not_all_collapsed():
    """Edge case: render() called on a diff with no actual changes at all
    (shouldn't normally happen via compute_plan, since _diff_list_field
    returns None in that case — but render() itself must still behave
    sensibly if called directly). With nothing to anchor a context
    window around, the whole list should show plainly, not collapse
    behind a single unhelpful '... (N unchanged) ...' marker."""
    cols = [_col("A", "VARCHAR(10)"), _col("B", "VARCHAR(10)"), _col("C", "VARCHAR(10)")]
    fd = FieldDiff(live=cols, desired=cols)

    rendered = fd.render(context=2)

    assert "..." not in rendered
    assert rendered.splitlines() == ["  A VARCHAR(10)", "  B VARCHAR(10)", "  C VARCHAR(10)"]


def test_render_identical_column_order_shows_no_changes():
    """A genuine no-op — same columns, same order, same values — renders
    with no +/-/~ lines at all."""
    cols = [_col("A", "VARCHAR(10)"), _col("B", "VARCHAR(10)")]
    fd = FieldDiff(live=cols, desired=cols)

    rendered = fd.render()

    assert "+" not in rendered
    assert "-" not in rendered
    assert "~" not in rendered


def test_render_reordered_columns_shows_moved_item_as_remove_and_add():
    """Documents actual behavior rather than an idealized one: this diff
    has no move-detection (matching how `git diff` itself behaves), so an
    actual reorder — not just a no-op — renders as the moved column being
    removed from its old position and added at its new one, even though
    no field on it actually changed."""
    live = [_col("A", "VARCHAR(10)"), _col("B", "VARCHAR(10)")]
    desired = [_col("B", "VARCHAR(10)"), _col("A", "VARCHAR(10)")]
    fd = FieldDiff(live=live, desired=desired)

    rendered = fd.render()

    assert "-" in rendered or "+" in rendered  # the swap is visible, not silently ignored