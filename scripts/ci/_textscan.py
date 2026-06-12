"""Shared markdown code-span stripping for the PR-body gate scripts.

DEC-TEXTSCAN-COMMON-001: the single source of truth (CLAUDE.md §11
COMMON) for removing fenced blocks and inline code spans before
keyword/ID scanning. Consumed by ``validate_no_hallucination.py``
(which strips fences BEFORE paragraph-splitting — the alignment
contract of DEC-SCANNER-PARAGRAPH-ALIGN-001) and
``validate_dec_ids.py`` (which strips both span kinds from the whole
body before extracting DEC-IDs, so documentation examples inside
backticks are not treated as live citations).
"""

from __future__ import annotations

import re

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def strip_fenced_blocks(text: str) -> str:
    """Remove fenced code blocks (which may span paragraphs) from *text*."""
    return _FENCED_CODE_RE.sub(" ", text)


def strip_inline_code(text: str) -> str:
    """Remove inline code spans from *text* (single-backtick runs)."""
    return _INLINE_CODE_RE.sub(" ", text)


def strip_code_spans(text: str) -> str:
    """Remove fenced blocks AND inline code spans from *text*.

    Order matters: fences first, so a backtick run inside a fence cannot
    confuse the inline pattern.
    """
    return strip_inline_code(strip_fenced_blocks(text))
