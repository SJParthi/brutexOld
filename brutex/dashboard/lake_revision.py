"""Cross-process lake freshness token — ONE O(1) ``stat`` of the window ledger.

DEC-HARDENING-LEFTOVERS-001. The dashboard's freshness signal is the in-process
:class:`~brutex.dashboard.summary_cache.SummaryCache` generation counter, bumped
by :meth:`~brutex.dashboard.summary_cache.SummaryCache.mark_dirty` at the
dashboard's OWN write sites (the background pull job, materialize, sweep). A CLI
``brutex fetch`` writing into the SAME ``BRUTEX_LAKE_ROOT`` runs in a **separate
process** — it never touches that int, so every generation-keyed cache (``/db``
totals + dataset discovery, the F&O/spot cascades, ``/coverage/windows`` counts,
``/movers``, coverage/completeness/mapping) served pre-CLI-write data until a
dashboard restart.

This module supplies the missing cross-process signal: a cheap **content token**
derived from ``os.stat`` of the durable window-ledger Parquet
(``<lake_root>/window_ledger/ledger.parquet``). Every production pull —
dashboard AND CLI — checkpoints that ledger per window
(DEC-CLI-LEDGER-CHECKPOINT-001), so a pull from ANY process moves the token.
:class:`~brutex.dashboard.summary_cache.SummaryCache` polls the token via its
``revision_probe`` seam and bumps its generation when the token changes,
invalidating every generation-keyed cache for free.

Honest envelope (Golden Rule #10 — stated, not hidden)
------------------------------------------------------
* The token moves ONLY when a writer touches the window ledger. **All production
  pulls do** (the per-window checkpoint of DEC-CLI-LEDGER-CHECKPOINT-001 plus the
  end-of-pull save). An out-of-band raw-Parquet write that BYPASSES the ledger
  (e.g. hand-copying a partition file into ``bars/``) is **NOT detected** —
  explicitly out of scope; detecting it would need an O(files) tree walk per
  probe (not fabricated as O(1)) or the write-time lake manifest already named
  as future work (#867/#1025).
* The probe is a single ``os.stat`` — O(1) in lake depth, safe to call on every
  generation read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import structlog

_LOG = structlog.get_logger("brutex.dashboard.lake_revision")

#: Stable sentinel token for a lake whose window ledger does not exist yet (a
#: fresh/empty lake, or one populated before the ledger existed). Stable so a
#: missing ledger never oscillates the token: the FIRST pull that checkpoints
#: the ledger moves ``absent`` → a real stat token exactly once.
ABSENT_TOKEN: Final[str] = "absent"  # noqa: S105  # nosec B105 — a freshness sentinel, not a secret


def lake_revision(lake_root: Path) -> str | None:
    """Return an O(1) content token for *lake_root*'s window ledger, or ``None``.

    The token is ``"<st_mtime_ns>:<st_size>"`` of
    ``<lake_root>/window_ledger/ledger.parquet`` (resolved via the COMMON
    :func:`brutex.data.window_ledger_store.ledger_path` — the ONE place that
    path is defined, CLAUDE.md §11). Any writer that checkpoints the ledger —
    the dashboard pull job or a CLI ``brutex fetch`` in another process — moves
    the mtime (and usually the size), so the token changes.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/`` /
        ``window_ledger/``).

    Returns
    -------
    str | None
        * a stat token ``"<mtime_ns>:<size>"`` when the ledger file exists;
        * :data:`ABSENT_TOKEN` (a stable sentinel) when it does not exist yet;
        * ``None`` — "unknown, no signal" — on any OTHER :class:`OSError`
          (permissions, I/O error), logged as a structlog warning. ``None``
          tells the caller to make NO freshness decision (never a spurious
          invalidation, never a masked error).
    """
    from brutex.data.window_ledger_store import ledger_path

    target = ledger_path(lake_root)
    try:
        st = os.stat(target)
    except FileNotFoundError:
        return ABSENT_TOKEN
    except OSError as exc:
        _LOG.warning(
            "lake_revision.stat_failed",
            ledger_path=str(target),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


__all__ = ["ABSENT_TOKEN", "lake_revision"]
