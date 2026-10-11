"""Chunking a JSON-encoded single-line body: escaped line breaks must come back.

A body retained from a JSON-encoded chat arrives as ONE line — ``json.dumps`` escaped every
real line break to the two-character sequence ``\\n``. The chunker then sees no line structure
at all, so a git-diff hunk or a log record reaches the extractor as one undifferentiated string
and the model has to guess where a hunk's ``+`` / ``-`` / context lines begin. Measured on a
real retained body (13 453 characters, 122 literal ``\\n``, zero real ones) no chunk was
parseable as a diff and the extractor called two UNCHANGED diff lines "added".

``iter_chunks`` now restores those line breaks on the text it hands to the splitter. The stored
document is not touched, so ``documents.original_text`` stays valid JSON — unlike rewriting the
body upstream of the chunker, which is what an operation validator cannot do without breaking
that contract.

Scope: the restoration happens where a body is treated as plain text, i.e. once it is long
enough to be split (``len(text) > max_chars``). A body that fits in a single chunk is returned
verbatim, as before.
"""

from hindsight_api.engine.retain.fact_extraction import (
    _restore_literal_line_breaks,
    chunk_text,
)

# A git-diff hunk as it reaches us from a JSON-encoded body: one line, escaped line breaks, a
# removed line and an added line. Long enough (and prose-padded) to be split.
DIFF_HUNK = (
    "diff --git a/recs.py b/recs.py\\n"
    "@@ -1,4 +1,5 @@\\n"
    " def wait_until_ready_B():\\n"
    "     time.sleep(60) " + "padding " * 30 + "\\n"
    "-    return A.wait_recs_upstream_B\\n"
    "+    return A.wait_recs_upstream_B.wait_until_ready_B\\n"
    "+    # retry once " + "padding " * 30 + "\\n"
)


def test_restores_escaped_line_breaks():
    assert _restore_literal_line_breaks("l1\\nl2\\nl3") == "l1\nl2\nl3"


def test_keeps_carriage_return_line_breaks_as_one_newline():
    assert _restore_literal_line_breaks("a\\r\\nb\\r\\nc") == "a\nb\nc"


def test_keeps_an_escaped_backslash_escaped():
    """``\\\\n`` is a literal backslash followed by an n, not a line break."""
    assert _restore_literal_line_breaks("a\\\\nb\\nc") == "a\\nb\nc"


def test_leaves_text_with_real_line_breaks_alone():
    text = "a\nb\nc"
    assert _restore_literal_line_breaks(text) == text


def test_leaves_a_single_literal_mention_alone():
    """One ``\\n`` in a one-line path or regex is not a line break."""
    text = "see C:\\tmp and the \\n escape"
    assert _restore_literal_line_breaks(text) == text


def test_is_idempotent():
    once = _restore_literal_line_breaks("a\\nb\\nc")
    assert _restore_literal_line_breaks(once) == once


def test_chunked_body_gains_line_structure():
    """Before: one line, no line breaks in any chunk. After: the body's lines are real lines."""
    assert "\n" not in DIFF_HUNK and DIFF_HUNK.count("\\n") >= 2
    chunks = chunk_text(DIFF_HUNK, 120)
    assert chunks
    # every escaped line break became a real one, and no escaped one is left behind (line breaks
    # that land exactly on a chunk boundary are consumed by the splitter, as with any separator)
    assert sum(chunk.count("\n") for chunk in chunks) >= 2
    assert not any("\\n" in chunk for chunk in chunks), chunks
    # no text is lost by the restoration
    assert "".join(c.replace("\n", "") for c in chunks).count("padding") == DIFF_HUNK.count("padding")


def test_diff_markers_are_separate_lines():
    """A hunk's ``+``/``-`` lines must be reachable as lines, which is what the extractor reads."""
    chunks = chunk_text(DIFF_HUNK, 120)
    lines = [line for chunk in chunks for line in chunk.split("\n")]
    assert any(line.startswith("+    return A.wait_recs_upstream_B") for line in lines), lines
    assert any(line.startswith("-    return A.wait_recs_upstream_B") for line in lines), lines


def test_re_chunking_a_chunk_is_stable():
    """Issue #2301: a chunk that re-splits would inherit one chunk_index and collide on chunk_id."""
    chunks = chunk_text(DIFF_HUNK, 120)
    for chunk in chunks:
        assert chunk_text(chunk, 120) == [chunk]


def test_a_short_body_is_returned_verbatim():
    """Documented scope: a body that fits in one chunk is not re-shaped at all."""
    short = "one line with \\n inside"
    assert chunk_text(short, 120) == [short]


def test_stored_document_is_not_modified():
    """The restoration is a read-only view: the same input still parses as JSON afterwards."""
    import json

    structured = '[{"role": "user", "content": "' + "x" * 400 + '\\n' + "y" * 400 + '"}]'
    assert json.loads(structured) is not None
    chunk_text(structured, 120, structured_chunk_size=60)
    assert json.loads(structured) is not None