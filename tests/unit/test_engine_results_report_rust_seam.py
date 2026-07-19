"""Rust-seam pins for the results_report render-law conversion (slice 2c phase B).

DEC-RUST-CASCADE-RESULTS-001: the two Phase-A-flagged decisions, pinned here
(NEVER by regenerating the frozen parity fixture):

* **decision (iv)** — ``float(pbo)`` accepts CPython's FULL float-literal
  grammar (``"inf"`` / ``"nan"`` / underscores), and the ``.4f`` render of a
  non-finite double is CPython's spelling (``inf`` / ``nan`` — the crate
  special-cases NaN, which Rust would otherwise render ``NaN``). The frozen
  corpus captures only the four finite coercions; these pins freeze the
  law's non-finite arm against the Rust host (mirrored by the
  ``py_fixed_non_finite_matches_cpython`` cargo test).
* **decision (iii)** — string-typed values never cross the FFI as Rust
  UTF-8: fragments are stringified by CPython ``str()`` and joined by
  ``str.join``, so a lone-surrogate label MINTED BY A JSON ``\\ud800``
  ESCAPE inside a real sidecar file flows through the whole render
  byte-identically (the frozen corpus pins a surrogate INSTRUMENT on the
  leaderboard; this pins the sidecar-label path).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brutex.engine.results_report import _render_sidecar_group, render_cascade_sidecar


def _group(pool_pbo: object) -> dict[str, object]:
    return {
        "instrument": "NIFTY",
        "tf": "1minute",
        "report": {
            "n_input": 3,
            "n_survived": 2,
            "pool_pbo": pool_pbo,
            "dsr_n_source": "leaderboard",
            "dsr_n_trials": 12,
        },
    }


class TestFloatPboNonFiniteLaw:
    """Decision (iv): ``f"{float(pbo):.4f}"`` over the full float grammar."""

    @pytest.mark.parametrize(
        ("pbo", "rendered"),
        [
            ("inf", "pool PBO=inf"),
            ("-inf", "pool PBO=-inf"),
            ("nan", "pool PBO=nan"),  # NEVER Rust's "NaN" spelling
            ("-nan", "pool PBO=nan"),  # CPython ignores the NaN sign bit
            ("1_5", "pool PBO=15.0000"),  # underscore grammar coerces
            ("  0.25\n", "pool PBO=0.2500"),  # float() strips whitespace
        ],
    )
    def test_group_line_renders_cpython_spelling(self, pbo: str, rendered: str) -> None:
        assert rendered in _render_sidecar_group(_group(pbo))

    def test_sidecar_file_with_inf_pbo_renders_not_unreadable(self, tmp_path: Path) -> None:
        # End-to-end through the shim's file read + the crate render: an
        # "inf" pbo is a LEGAL coercion, never an UNREADABLE funnel.
        sidecar = tmp_path / "cascade" / "cascade-report.json"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text(json.dumps({"version": 1, "groups": [_group("inf")]}), encoding="utf-8")
        out = render_cascade_sidecar(tmp_path)
        assert "pool PBO=inf" in out
        assert "UNREADABLE" not in out


class TestSurrogateSidecarLabel:
    """Decision (iii): a JSON ``\\ud800``-escape label survives the render."""

    def test_lone_surrogate_instrument_label_passes_through(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "cascade" / "cascade-report.json"
        sidecar.parent.mkdir(parents=True)
        # json.dumps would escape it back; write the escape form directly so
        # json.loads mints a REAL lone-surrogate str inside the crate.
        sidecar.write_text(
            '{"version": 1, "groups": [{"instrument": "BAD-\\ud800-LABEL", "tf": "1m", '
            '"report": {"n_input": 1, "n_survived": 1, "pool_pbo": 0.25}}]}',
            encoding="utf-8",
        )
        out = render_cascade_sidecar(tmp_path)
        assert "BAD-\ud800-LABEL 1m" in out
        assert "pool PBO=0.2500" in out

    def test_group_line_direct_call_with_surrogate_label(self) -> None:
        entry = json.loads('{"instrument": "\\ud800", "tf": "x", "report": {}}')
        line = _render_sidecar_group(entry)
        assert line.startswith("  \ud800 x  ")


class TestShimReadArm:
    """The shim-side sidecar READ arm (the split's Python half).

    The deleted body wrapped ``read_text`` AND ``json.loads`` in ONE
    ``except (OSError, ValueError)``; the conversion split keeps the file
    read (and its decode/OSError ``UNREADABLE`` arm) shim-side while the
    crate owns the ``json.loads`` arm. An invalid-UTF-8 sidecar raises
    ``UnicodeDecodeError`` (⊂ ``ValueError``) from ``read_text`` — the
    rendered section is byte-identical to the pre-split law's.
    """

    def test_invalid_utf8_sidecar_renders_unreadable_decode_arm(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "cascade" / "cascade-report.json"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_bytes(b'{"version": 1, "groups": [\xff\xfe]}')
        out = render_cascade_sidecar(tmp_path)
        assert out.startswith(f"Stage-4 cascade sidecar — {sidecar}\n  UNREADABLE: ")
        assert "codec can't decode byte" in out
