from __future__ import annotations

import pytest

from code4me2_agent.hunks import MAX_REVISE_HUNKS, RevisionOffer, apply_hunks, diff_hunks


def _numbered(count: int, newline: str = "\n") -> str:
    return "".join(f"line {number}{newline}" for number in range(1, count + 1))


# Three changes far enough apart (more than 2 x 3 context lines) to be three hunks.
OLD = _numbered(40)
NEW = (
    OLD.replace("line 3\n", "LINE three\n")
    .replace("line 20\n", "line 20\nadded a\nadded b\n")
    .replace("line 35\n", "")
)


def test_hunks_are_numbered_and_labelled_in_file_order():
    hunks = diff_hunks(OLD, NEW)

    assert [hunk.index for hunk in hunks] == [0, 1, 2]
    assert [hunk.label for hunk in hunks] == [
        "L3 −1 +1 · LINE three",
        # An insertion is labelled with the current line it goes before.
        "L21 −0 +2 · added a",
        # Nothing added: the removed line names the hunk.
        "L35 −1 +0 · line 35",
    ]
    assert [(hunk.old_start, hunk.old_end, hunk.removed, hunk.added) for hunk in hunks] == [
        (3, 3, 1, 1),
        (21, 20, 0, 2),
        (35, 35, 1, 0),
    ]
    assert [(hunk.new_start, hunk.new_end) for hunk in hunks] == [(3, 3), (21, 22), (37, 36)]


def test_keep_all_none_and_a_subset():
    assert apply_hunks(OLD, NEW, [0, 1, 2]) == NEW
    assert apply_hunks(OLD, NEW, []) == OLD
    kept_middle = apply_hunks(OLD, NEW, [1])
    assert kept_middle == OLD.replace("line 20\n", "line 20\nadded a\nadded b\n")
    kept_outer = apply_hunks(OLD, NEW, {0, 2})
    assert kept_outer == OLD.replace("line 3\n", "LINE three\n").replace("line 35\n", "")
    # Unknown indexes are ignored.
    assert apply_hunks(OLD, NEW, [7, -1]) == OLD


def test_nearby_changes_form_one_hunk_spanning_both():
    old = _numbered(12)
    new = old.replace("line 4\n", "four\n").replace("line 7\n", "seven\nseven b\n")

    (hunk,) = diff_hunks(old, new)

    assert hunk.label == "L4–7 −2 +3 · four"
    assert apply_hunks(old, new, [0]) == new


def test_crlf_endings_survive_partial_application():
    old = _numbered(20, "\r\n")
    new = old.replace("line 2\r\n", "two\r\n").replace("line 18\r\n", "eighteen\r\n")

    hunks = diff_hunks(old, new)
    merged = apply_hunks(old, new, [1])

    assert len(hunks) == 2
    assert merged == old.replace("line 18\r\n", "eighteen\r\n")
    assert "\r\n" in merged and "\n" not in merged.replace("\r\n", "")


def test_missing_final_newline_is_preserved():
    old = _numbered(19) + "line 20"
    new = old.replace("line 1\n", "one\n").replace("line 20", "twenty")

    assert apply_hunks(old, new, [0]) == old.replace("line 1\n", "one\n")
    assert apply_hunks(old, new, [1]) == _numbered(19) + "twenty"
    assert apply_hunks(old, new, [0, 1]) == new


def test_label_text_is_collapsed_and_trimmed():
    old = _numbered(3)
    new = old.replace("line 2\n", "\n   \t" + "x" * 80 + "   y\n")

    (hunk,) = diff_hunks(old, new)

    text = hunk.label.split(" · ", 1)[1]
    assert len(text) == 60 and text.endswith("…") and text.startswith("x" * 59)


def test_identical_text_has_no_hunks_and_is_deterministic():
    assert diff_hunks(OLD, OLD) == []
    assert apply_hunks(OLD, OLD, [0]) == OLD
    assert diff_hunks(OLD, NEW) == diff_hunks(OLD, NEW)
    assert diff_hunks("", "") == []


def test_offer_lists_only_two_to_twenty_hunks():
    many_old = _numbered(MAX_REVISE_HUNKS * 10 + 10)
    lines = many_old.splitlines(keepends=True)

    def changed(count: int) -> str:
        edited = list(lines)
        for position in range(count):
            edited[position * 10 + 5] = f"changed {position}\n"
        return "".join(edited)

    assert len(RevisionOffer("a.py", many_old, changed(MAX_REVISE_HUNKS)).selectable_hunks) == MAX_REVISE_HUNKS
    too_many = RevisionOffer("a.py", many_old, changed(MAX_REVISE_HUNKS + 1))
    assert len(too_many.hunks) == MAX_REVISE_HUNKS + 1
    assert too_many.selectable_hunks == ()  # instructions only
    assert RevisionOffer("a.py", many_old, changed(1)).selectable_hunks == ()
    # A new file is one insertion; no change at all offers nothing either.
    new_file = RevisionOffer("b.py", None, "a\nb\n")
    assert len(new_file.hunks) == 1 and new_file.selectable_hunks == ()
    assert RevisionOffer().hunks == ()


@pytest.mark.parametrize("keep", [[], [0], [1], [0, 1]])
def test_partial_application_round_trips_through_a_second_diff(keep):
    old = _numbered(30)
    new = old.replace("line 2\n", "two\n").replace("line 25\n", "twenty-five\n")

    merged = apply_hunks(old, new, keep)

    # What remains to change is exactly the hunks that were not kept.
    assert len(diff_hunks(merged, new)) == 2 - len(keep)
