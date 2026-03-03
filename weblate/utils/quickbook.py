# Copyright © Boost Organization <boost@boost.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
QuickBook (.qbk) parsing and PO conversion utilities.

Implements an in-process parser that converts QuickBook documentation markup
to and from PO format.  There is no third-party converter (like po4a) for
QuickBook, so the full conversion logic lives here.

Translatable constructs extracted:
    paragraphs (including those containing inline [@url], [link …], etc.),
    ordered/unordered list blocks, headings [h1..h6] and generic [heading …],
    section titles [section], admonitions ([note], [warning], [tip],
    [caution], [important], [blurb]), block-quotes [:…], table titles and
    prose cells [table], and variable list items [variablelist].

Non-translatable constructs (copied verbatim):
    code blocks (lines indented with space/tab), [pre …], [/ comments],
    [include …], [import …], [def …], [template …], [quickbook …],
    anchors [#…], images [$…], source-mode switches ([c++] etc.),
    conditional generation [? …], [endsect], table cells containing only
    code fences (``` … ```) or bare bracket references.

Inline markup ([*bold], ['italic], [@url text], [funcref …], etc.) is
preserved verbatim inside msgid/msgstr so translators see it and keep it.
Inline elements that wrap onto their own line (e.g. a bare [@url …] line
inside a paragraph) are now correctly treated as part of the surrounding
paragraph rather than breaking it.

Sections whose body contains further translatable blocks are parsed
recursively (depth-limited) so nested paragraphs and headings are also
extracted.

Reversibility (PO → QBK):
    The reconstruction algorithm replaces each translatable span in the
    original template with the corresponding translation from the PO store,
    copying everything else (bracket wrappers, code blocks, blank lines, …)
    character-for-character.  Segments that have no translation fall back to
    the original English text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from translate.storage.pypo import pofile

# Text that consists only of QuickBook macro references and punctuation is not
# translatable prose; it is a rendered identifier placeholder.
_QBK_MACRO_ONLY_RE = re.compile(r"^(?:__\w+__[\s,;.]*)+$")


# ---------------------------------------------------------------------------
# Grammar constants
# ---------------------------------------------------------------------------

# Block-level bracket keywords whose entire content is non-translatable.
_SKIP_KEYWORDS: frozenset[str] = frozenset(
    {
        "/",           # [/ comment]
        "include",     # [include file.qbk]
        "import",      # [import file.qbk]
        "def",         # [def macro_name value]
        "template",    # [template …]
        "quickbook",   # [quickbook 1.x] version declaration
        "br",          # [br] deprecated line-break
        "pre",         # [pre preformatted / code-like block]
        "endsect",     # [endsect]
        "xinclude",    # [xinclude …]
        "if",          # [if symbol]
        "elif",        # [elif symbol]
        "else",        # [else]
        "endif",       # [endif]
        # source-mode switches
        "c++",
        "python",
        "ruby",
        "teletype",
        "xml",
        "javascript",
        # cross-reference links (appear inline inside paragraphs, not as
        # standalone blocks; the surrounding paragraph is the unit)
        "funcref",
        "classref",
        "memberref",
        "enumref",
        "macroref",
        "conceptref",
        "headerref",
        "globalref",
        "link",
    }
)

# Single-character bracket-openers that produce non-translatable blocks.
# ":"  (blockquote) is intentionally absent; it IS translatable.
_SKIP_SINGLE_CHARS: frozenset[str] = frozenset({"/", "#", "$", "@", "?"})

# Numbered headings [h1..h6] plus the generic [heading …] form.
_HEADING_KEYWORDS: frozenset[str] = frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "heading"}
)

_ADMONITION_KEYWORDS: frozenset[str] = frozenset(
    {"note", "warning", "tip", "caution", "important", "blurb"}
)

# Block-level keywords that terminate a paragraph when they appear at the
# start of a line.  Inline / phrase-level keywords such as [@url …],
# [link …], [*bold], [funcref …], etc. do NOT appear here; they are allowed
# to wrap onto their own line while still belonging to the surrounding
# paragraph.
_PARA_BREAK_KEYWORDS: frozenset[str] = frozenset(
    {
        "section", "endsect",
        "h1", "h2", "h3", "h4", "h5", "h6", "heading",
        "note", "warning", "tip", "caution", "important", "blurb",
        "table", "variablelist",
        "pre", "include", "import", "def", "template", "quickbook", "xinclude",
        "if", "elif", "else", "endif",
        "c++", "python", "ruby", "teletype", "xml", "javascript",
        "/",
    }
)

# Single-character openers that always break a paragraph.
# "@" (URL link) is absent because it is an inline element.
_PARA_BREAK_SINGLE_CHARS: frozenset[str] = frozenset({"/", "#", "$", "?", ":"})


# ---------------------------------------------------------------------------
# Bracket utilities
# ---------------------------------------------------------------------------


def _find_bracket_end(text: str, start: int) -> int:
    """Return the index of the ``]`` that closes the ``[`` at *text[start]*.

    Handles:

    * Nested brackets: ``[outer [inner] still outer]``
    * Triple-quote raw escapes: ``'''anything including [ and ]'''``
    * Single backslash escapes: ``\\[`` ``\\]``

    Returns ``-1`` if no matching bracket is found before the end of *text*.
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        # Triple-quote raw escape: everything inside ''' … ''' is opaque.
        if text[i : i + 3] == "'''":
            i += 3
            while i < n and text[i : i + 3] != "'''":
                i += 1
            i += 3
            continue
        # Backslash escapes the next character.
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_bracket_keyword(text: str) -> tuple[str, int]:
    """Parse keyword and content-start offset from a bracket block string.

    *text* spans the full bracket including the surrounding ``[`` and ``]``.

    Returns ``(keyword, content_offset)`` where *content_offset* is the index
    within *text* at which the keyword's body begins (i.e. past the keyword
    token, an optional ``:id`` suffix, and leading whitespace / one newline).
    """
    i = 1  # skip opening '['
    n = len(text)

    # Single-character special keywords: /, #, $, @, ?, :
    if i < n and text[i] in ("/", "#", "$", "@", "?", ":"):
        kw = text[i]
        i += 1
        while i < n and text[i] in (" ", "\t"):
            i += 1
        return kw, i

    # Multi-character keyword: read until whitespace, ], or :
    kw_start = i
    while i < n and text[i] not in (" ", "\t", "\n", "]", ":"):
        i += 1
    kw = text[kw_start:i].lower()

    # Optional :id suffix (e.g. ``section:my_anchor``)
    if i < n and text[i] == ":":
        i += 1
        while i < n and text[i] not in (" ", "\t", "\n", "]"):
            i += 1  # skip the id token

    # Skip trailing spaces / tabs after keyword or :id, then one optional newline.
    while i < n and text[i] in (" ", "\t"):
        i += 1
    if i < n and text[i] == "\n":
        i += 1

    return kw, i


# ---------------------------------------------------------------------------
# Segment data model
# ---------------------------------------------------------------------------


@dataclass
class _Seg:
    """One translatable span within a QuickBook document."""

    text_start: int  # absolute char offset of the first translatable character
    text_end: int    # exclusive end offset (points just past the last char)
    line: int        # 1-based line number of the containing block start
    seg_type: str    # 'paragraph', 'list', 'heading', 'section-title', …
    msgid: str       # normalised translatable text (PO msgid)
    no_wrap: bool    # True → add ``no-wrap`` type-comment to the PO unit
    context: str = ""  # PO ``#. type:`` annotation


# ---------------------------------------------------------------------------
# Cell-prose helpers (used by table / variablelist parser)
# ---------------------------------------------------------------------------


def _has_prose(text: str) -> bool:
    """Return True if *text* contains translatable prose outside bracket markup.

    Used to distinguish cells with human-readable description text from cells
    that contain only bracket references (``[link …]``, ``[@url …]``, etc.),
    empty/whitespace content, or QuickBook macro references (``__name__``)
    that expand to rendered identifiers and are not translatable.
    """
    # Collect characters that are outside all bracket blocks.
    bare_chars: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "[":
            end = _find_bracket_end(text, i)
            if end != -1:
                bare_chars.append(" ")  # bracket replaced by space placeholder
                i = end + 1
                continue
        bare_chars.append(text[i])
        i += 1
    bare = "".join(bare_chars).strip()
    if not bare:
        return False
    # QuickBook macro references such as ``__message__`` are identifier
    # placeholders, not human-readable prose.
    if _QBK_MACRO_ONLY_RE.match(bare):
        return False
    return True


def _clean_cell_text(text: str) -> str:
    """Prepare raw cell content as a PO msgid.

    Steps:
    * Strip backtick code fences (````` ``` … ``` `````) — those lines are code.
    * Strip each line's leading/trailing whitespace (cell indentation is
      formatting, not significant).
    * Within each paragraph, join soft-wrapped lines with a single space so
      that the msgid reads as one continuous sentence.
    * Preserve blank lines between distinct paragraphs within the cell
      (represented as ``\\n\\n`` in the msgid).
    """
    lines = text.split("\n")
    paragraphs: list[str] = []
    current_para: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped == "```":
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped:
            current_para.append(stripped)
        else:
            if current_para:
                paragraphs.append(" ".join(current_para))
                current_para = []
    if current_para:
        paragraphs.append(" ".join(current_para))
    return "\n\n".join(p for p in paragraphs if p)


def _extract_fence_content_segs(
    content: str,
    cell_body_start: int,
    cell_body_end: int,
    bracket_line: int,
    kw: str,
) -> list[_Seg]:
    """Extract translatable content from backtick code fences inside a table cell.

    When a cell contains ``` … ``` fences, the code *between* the fence lines
    is extracted as a translatable segment.  ``text_start``/``text_end`` point
    to just the fence content so that ``po_to_qbk`` replaces only the content
    while leaving the ``` markers in place.

    This allows doc-comment lines (``///``, ``//``) and similar in-code
    documentation to be translated without disturbing the surrounding markup.
    """
    segs: list[_Seg] = []
    in_fence = False
    fence_content_start: int | None = None

    i = cell_body_start
    while i <= cell_body_end:
        # Find the end of the current line within the cell body.
        eol = i
        while eol < cell_body_end and content[eol] != "\n":
            eol += 1

        line_stripped = content[i:eol].strip()

        if line_stripped == "```":
            if not in_fence:
                in_fence = True
                # Content starts on the next line (after the \n that follows the fence).
                fence_content_start = eol + 1 if eol < cell_body_end else eol
            else:
                in_fence = False
                fence_content_end = i  # points to the first char of the closing fence line
                if fence_content_start is not None and fence_content_end > fence_content_start:
                    raw_code = content[fence_content_start:fence_content_end]
                    # Strip per-line indentation; join non-empty lines.
                    code_lines = [ln.strip() for ln in raw_code.split("\n") if ln.strip()]
                    cleaned_code = "\n".join(code_lines)
                    if cleaned_code:
                        segs.append(
                            _Seg(
                                fence_content_start,
                                fence_content_end,
                                bracket_line,
                                kw,
                                cleaned_code,
                                no_wrap=True,
                                context=f"{kw} code",
                            )
                        )
                fence_content_start = None

        # Advance past this line (and the \n if present).
        i = eol + 1 if eol < cell_body_end else cell_body_end + 1

    return segs


def _parse_table_inner(
    content: str,
    inner_abs_start: int,
    inner_abs_end: int,
    bracket_line: int,
    kw: str,
    _depth: int,
) -> list[_Seg]:
    """Parse a ``[table …]`` or ``[variablelist …]`` body into fine-grained segments.

    Extracts:

    * An optional **title** — the (non-bracket) text on the first line.
    * **Prose cells** — cell bodies ``[…]`` nested inside row brackets ``[…]``
      that contain translatable human-readable text.
    * **Code cells** — cells whose content is a backtick code fence
      (````` ``` … ``` `````).  The content *between* the fence markers is
      extracted as a segment so that doc-comment lines (``///``, ``//``) can
      be translated while the fence markers stay in place.

    The ``text_start``/``text_end`` offsets for prose cells span the full
    cell body; for code-fence cells they span only the content between the
    fence lines so that reconstruction leaves the ````` ``` ````` markers
    intact.
    """
    inner = content[inner_abs_start:inner_abs_end]
    segs: list[_Seg] = []

    # ── Optional title: non-bracket text on the first line ──────────────────
    nl = inner.find("\n")
    title_raw = inner[:nl] if nl != -1 else inner
    title = title_raw.strip()
    if title and not title.startswith("["):
        lead = inner.index(title)
        segs.append(
            _Seg(
                inner_abs_start + lead,
                inner_abs_start + lead + len(title),
                bracket_line,
                f"{kw}-title",
                title,
                no_wrap=True,
                context=f"{kw} title",
            )
        )
    if nl == -1:
        return segs

    # ── Row / cell scanning ──────────────────────────────────────────────────
    # Walk the table body looking for top-level bracket blocks (rows).
    # Rows contain nested bracket blocks (cells).
    i = inner_abs_start + nl + 1
    while i < inner_abs_end:
        ch = content[i]
        if ch in (" ", "\t", "\n"):
            i += 1
            continue
        if ch != "[":
            i += 1
            continue

        row_end = _find_bracket_end(content, i)
        if row_end == -1 or row_end > inner_abs_end:
            i += 1
            continue

        # Walk the row to find cell brackets.
        ci = i + 1  # skip the opening '['
        while ci < row_end:
            cc = content[ci]
            if cc in (" ", "\t", "\n"):
                ci += 1
                continue
            if cc != "[":
                ci += 1
                continue

            cell_end = _find_bracket_end(content, ci)
            if cell_end == -1 or cell_end > row_end:
                ci += 1
                continue

            cell_body_start = ci + 1
            cell_body_end = cell_end
            raw_cell = content[cell_body_start:cell_body_end]
            cleaned = _clean_cell_text(raw_cell)
            if cleaned:
                segs.append(
                    _Seg(
                        cell_body_start,
                        cell_body_end,
                        bracket_line,
                        kw,
                        cleaned,
                        no_wrap=True,
                        context=f"{kw} cell",
                    )
                )
            else:
                # No prose — check for code-fence content (e.g. doc-comment blocks).
                segs.extend(
                    _extract_fence_content_segs(
                        content, cell_body_start, cell_body_end, bracket_line, kw
                    )
                )
            ci = cell_end + 1

        i = row_end + 1

    return segs


# ---------------------------------------------------------------------------
# Parser: QBK string → list[_Seg]
# ---------------------------------------------------------------------------


def _parse_qbk(
    content: str,
    start: int = 0,
    stop: int | None = None,
    _depth: int = 0,
) -> list[_Seg]:
    """Parse *content[start:stop]* and return all translatable segments.

    The function calls itself recursively (depth-capped at 10) for block
    elements whose bodies may contain further translatable blocks (e.g.
    ``[section …]``, ``[note …]``).

    All returned offsets are absolute positions within the original *content*
    string so that callers can manipulate the string directly.
    """
    if stop is None:
        stop = len(content)
    if _depth > 10:
        return []

    segments: list[_Seg] = []
    i = start
    # Compute the starting line number from the characters before *start*.
    line = content[:start].count("\n") + 1

    while i < stop:
        ch = content[i]

        # ── skip bare newlines ────────────────────────────────────────────────
        if ch == "\n":
            line += 1
            i += 1
            continue

        # ── code block: line begins with space or tab ─────────────────────────
        # Only treat as a code block if we are at the very start of a line.
        if ch in (" ", "\t") and (i == 0 or content[i - 1] == "\n"):
            # Consume all consecutive indented or blank lines.
            while i < stop:
                while i < stop and content[i] != "\n":
                    i += 1
                if i >= stop:
                    break
                i += 1
                line += 1
                # Stop when the next line is neither blank nor indented.
                if i < stop and content[i] not in (" ", "\t", "\n"):
                    break
            continue

        # ── triple-quote raw escape block ─────────────────────────────────────
        if content[i : i + 3] == "'''":
            i += 3
            while i < stop and content[i : i + 3] != "'''":
                if content[i] == "\n":
                    line += 1
                i += 1
            i += 3  # consume closing '''
            continue

        # ── bracket block ─────────────────────────────────────────────────────
        if ch == "[":
            bracket_start = i
            bracket_line = line
            end = _find_bracket_end(content, i)
            if end == -1 or end >= stop:
                # Malformed or outside our region: skip to end of line.
                while i < stop and content[i] != "\n":
                    i += 1
                continue

            block_text = content[bracket_start : end + 1]
            kw, content_off = _parse_bracket_keyword(block_text)
            line += block_text.count("\n")
            i = end + 1

            # ── non-translatable keywords ──────────────────────────────────
            if kw in _SKIP_KEYWORDS or kw in _SKIP_SINGLE_CHARS:
                continue

            # ── extract inner text boundaries ──────────────────────────────
            # raw_inner = everything between the keyword body and the closing ']'
            raw_inner = block_text[content_off:-1]
            lstrip_n = len(raw_inner) - len(raw_inner.lstrip())
            rstrip_n = len(raw_inner) - len(raw_inner.rstrip())
            inner = raw_inner.strip()
            if not inner:
                continue

            inner_abs_start = bracket_start + content_off + lstrip_n
            inner_abs_end = bracket_start + len(block_text) - 1 - rstrip_n
            inner_multiline = "\n" in inner

            # ── headings [h1..h6] and generic [heading …] ─────────────────
            if kw in _HEADING_KEYWORDS:
                # Numbered: kw = "h1"…"h6" → context "heading 1"…"heading 6".
                # Generic:  kw = "heading"  → context "heading".
                ctx = f"heading {kw[1]}" if kw != "heading" else "heading"
                segments.append(
                    _Seg(
                        inner_abs_start,
                        inner_abs_end,
                        bracket_line,
                        "heading",
                        inner,
                        no_wrap=True,
                        context=ctx,
                    )
                )
                continue

            # ── sections [section:id Title\nbody…] ─────────────────────────
            if kw == "section":
                if inner_multiline:
                    # Title = first line; body = the rest (recurse into it).
                    nl_pos = inner.index("\n")
                    raw_title_line = inner[:nl_pos]
                    title = raw_title_line.strip()
                    if title:
                        title_lead = raw_title_line.index(title)
                        title_abs_start = inner_abs_start + title_lead
                        title_abs_end = title_abs_start + len(title)
                        segments.append(
                            _Seg(
                                title_abs_start,
                                title_abs_end,
                                bracket_line,
                                "section-title",
                                title,
                                no_wrap=True,
                                context="section title",
                            )
                        )
                    # Recurse into the section body.
                    body_abs_start = inner_abs_start + nl_pos + 1
                    if body_abs_start < inner_abs_end:
                        segments.extend(
                            _parse_qbk(
                                content, body_abs_start, inner_abs_end, _depth + 1
                            )
                        )
                else:
                    # Single-line: the whole inner text is the title.
                    segments.append(
                        _Seg(
                            inner_abs_start,
                            inner_abs_end,
                            bracket_line,
                            "section-title",
                            inner,
                            no_wrap=True,
                            context="section title",
                        )
                    )
                continue

            # ── admonitions [note …], [warning …], etc. ───────────────────
            if kw in _ADMONITION_KEYWORDS:
                if inner_multiline:
                    # Body may contain full block elements: recurse.
                    segments.extend(
                        _parse_qbk(
                            content, inner_abs_start, inner_abs_end, _depth + 1
                        )
                    )
                else:
                    segments.append(
                        _Seg(
                            inner_abs_start,
                            inner_abs_end,
                            bracket_line,
                            "admonition",
                            inner,
                            no_wrap=False,
                            context=kw,
                        )
                    )
                continue

            # ── block quote [:text] ────────────────────────────────────────
            if kw == ":":
                if inner_multiline:
                    segments.extend(
                        _parse_qbk(
                            content, inner_abs_start, inner_abs_end, _depth + 1
                        )
                    )
                else:
                    segments.append(
                        _Seg(
                            inner_abs_start,
                            inner_abs_end,
                            bracket_line,
                            "blockquote",
                            inner,
                            no_wrap=False,
                            context="blockquote",
                        )
                    )
                continue

            # ── tables and variable lists (cell-level parsing) ────────────
            if kw in ("table", "variablelist"):
                segments.extend(
                    _parse_table_inner(
                        content,
                        inner_abs_start,
                        inner_abs_end,
                        bracket_line,
                        kw,
                        _depth,
                    )
                )
                continue

            # Unknown keyword at block level → skip.
            continue

        # ── paragraph / list block ────────────────────────────────────────────
        para_start = i
        para_line = line

        while i < stop:
            # Find the end of the current line.
            eol = i
            while eol < stop and content[eol] != "\n":
                eol += 1
            line_text = content[i:eol]

            if not line_text.strip():                    # blank line → end of para
                break
            if line_text and line_text[0] in (" ", "\t"):   # code block next
                break
            if line_text.startswith("'''"):              # raw escape next
                break
            if line_text.startswith("["):
                # Only break for block-level constructs.  Inline / phrase-level
                # elements ([@url …], [link …], [*bold], [funcref …], etc.) may
                # appear at the start of a wrapped line and still belong to the
                # enclosing paragraph.
                bracket_end = _find_bracket_end(line_text, 0)
                if bracket_end != -1:
                    para_kw, _ = _parse_bracket_keyword(line_text[:bracket_end + 1])
                else:
                    # Bracket extends beyond this line → treat as block-level.
                    para_kw, _ = _parse_bracket_keyword(line_text + "]")
                if para_kw in _PARA_BREAK_KEYWORDS or para_kw in _PARA_BREAK_SINGLE_CHARS:
                    break
                # Inline bracket — fall through and keep accumulating.

            i = eol
            if i < stop:
                i += 1   # consume '\n'
                line += 1

        stripped = content[para_start:i].rstrip()
        if stripped and _has_prose(stripped):
            first_non_ws = stripped.lstrip()[0]
            is_list = first_non_ws in ("*", "#")
            if is_list:
                # Keep newlines: each line is a structural list item.
                msgid = stripped
            else:
                # Prose paragraph: join soft-wrapped source lines with a
                # single space so the translator sees one complete sentence.
                msgid = " ".join(
                    ln.strip() for ln in stripped.split("\n") if ln.strip()
                )
            segments.append(
                _Seg(
                    para_start,
                    para_start + len(stripped),
                    para_line,
                    "list" if is_list else "paragraph",
                    msgid,
                    no_wrap=False,
                    context="list" if is_list else "paragraph",
                )
            )

    return segments


# ---------------------------------------------------------------------------
# QBK → PO
# ---------------------------------------------------------------------------


def qbk_to_po(
    content: str, filename: str, existing_units: Any = None
) -> pofile:
    """Convert a QuickBook document string to a ``pofile`` store.

    *filename* is used in PO location comments (``#: filename:lineno``).

    *existing_units* is an optional iterable of Weblate ``Unit`` objects whose
    existing translations are merged into the result (same pattern as
    ``AsciiDocFormat._merge_translations``).  Units not found via file
    extraction but present in the database are added so no translation is lost.
    """
    store = pofile()
    store.updateheader(add=True, x_accelerator_marker=None, x_previous_msgid=None)

    segments = _parse_qbk(content)

    # One PO unit per unique msgid.  If the same source text appears at
    # multiple locations the extra locations are appended to the same unit
    # (standard PO behaviour).
    unit_by_msgid: dict[str, Any] = {}

    for seg in segments:
        if not seg.msgid:
            continue
        location = f"{filename}:{seg.line}"
        if seg.msgid in unit_by_msgid:
            unit_by_msgid[seg.msgid].addlocation(location)
            continue

        unit = store.addsourceunit(seg.msgid)
        unit.addlocation(location)
        if seg.context:
            unit.addnote(f"type: {seg.context}", "developer")
        if seg.no_wrap:
            unit.settypecomment("no-wrap", True)
        unit_by_msgid[seg.msgid] = unit

    # Merge translations that exist in the Weblate database but may have been
    # missed by the file extractor (e.g. removed blocks, formatting changes).
    if existing_units:
        store_index: dict[tuple[str, str], Any] = {
            (u.source, u.getcontext()): u
            for u in store.units
            if not u.isheader()
        }
        for ex_unit in existing_units:
            sources = ex_unit.get_source_plurals()
            if not sources:
                continue
            src = sources[0]
            ctx = ex_unit.context or ""
            if (src, ctx) not in store_index:
                new_unit = store.addsourceunit(src)
                if ctx:
                    new_unit.setcontext(ctx)
                new_unit.target = ex_unit.target
                from weblate.utils.state import STATE_FUZZY  # noqa: PLC0415
                if ex_unit.state == STATE_FUZZY:
                    new_unit.markfuzzy(True)
                store_index[(src, ctx)] = new_unit

    return store


# ---------------------------------------------------------------------------
# PO → QBK
# ---------------------------------------------------------------------------


def po_to_qbk(template_content: str, po_store: Any, filename: str) -> str:  # noqa: ARG001
    """Apply translations from *po_store* to *template_content*.

    Returns the fully translated QuickBook document as a string.  Each
    translatable span identified by the parser is replaced with the
    corresponding ``msgstr`` from *po_store*.  Segments that have no
    translation keep their original source text (English fallback).

    The *filename* argument is accepted for API symmetry and future use.
    """
    # Build msgid → target lookup (skip empty translations).
    translations: dict[str, str] = {
        unit.source: unit.target
        for unit in po_store.units
        if not unit.isheader() and unit.source and unit.target
    }
    if not translations:
        return template_content

    # Parse the template once; sort segments by their position so we can
    # walk through the document left-to-right.
    segments = sorted(_parse_qbk(template_content), key=lambda s: s.text_start)

    parts: list[str] = []
    pos = 0
    for seg in segments:
        # Copy everything in the original file up to the translatable text
        # (this includes bracket wrappers, blank lines, code blocks, etc.).
        if seg.text_start > pos:
            parts.append(template_content[pos : seg.text_start])

        translation = translations.get(seg.msgid)
        if translation:
            parts.append(translation)
        else:
            # No translation available: keep the original source text.
            parts.append(template_content[seg.text_start : seg.text_end])

        pos = seg.text_end

    # Copy the remainder of the document (anything after the last segment).
    parts.append(template_content[pos:])
    return "".join(parts)
