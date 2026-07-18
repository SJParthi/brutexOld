"""``brutex.meta`` — the decision-precision annotation layer.

DEC-META-LABEL-ANNOTATE-001 (meta-labeling P(win), AFML Ch. 3/10) +
DEC-ABSTAIN-GATE-STREAM-001 (the abstention-first gate stream) — both
ANNOTATION-FIRST by the operator's 2026-07-18 winning-completeness ruling:
every output lands in ``.meta.json`` sidecars beside the results-depth trio,
NOTHING here gates, re-ranks, or touches any existing artifact byte. The
in-sim gating arm is designed + dormant (``docs/design/meta/
ABSTAIN_INSIM_DESIGN.md``); a set ``BRUTEX_ABSTAIN_GATE_INSIM`` refuses
loudly at the sweep launch.

Module map (each 100% line+branch on its own gate):

* :mod:`brutex.meta.features` — the fixed documented per-trade feature
  vector (intercept · vix_entry + missing indicator · entry-hour one-hots ·
  trend-regime one-hots · event-day flag).
* :mod:`brutex.meta.logistic` — the OFFLINE ridge-IRLS logistic fit (the
  sanctioned offline-statistics allowance) + the pure-Python inference
  ORACLE mirrored token-for-token by the Rust kernel.
* :mod:`brutex.meta.cpcv_fit` — strictly OUT-OF-FOLD P(win) via the
  existing :func:`brutex.metrics.cpcv.cpcv_splits` purge/embargo machinery
  (REUSED, never re-minted).
* :mod:`brutex.meta.threshold` — the τ sweep over the fixed 0.50→0.95 grid.
* :mod:`brutex.meta.abstain` — the abstention gate vocabulary + pure reason
  function + the dormant in-sim refusal knob (stdlib-only leaf).
* :mod:`brutex.meta.meta_rust` — the default-OFF ``BRUTEX_META_GATE`` facade
  over the ``rust/brutex-meta`` kernels (burn-in bit-compare +
  ship-the-oracle autorevert).
* :mod:`brutex.meta.annotate` — the ``brutex meta-annotate`` pass writing
  the ``<stem>.meta.json`` fourth sidecar + the run-level summary.

Deliberately re-exports NOTHING: consumers import the leaf they need (the
``brutex.crossverify`` package convention) so the stdlib-only refusal leaf
(:mod:`brutex.meta.abstain`) stays importable at the CLI chokepoint without
pulling numpy/polars into the sweep launch path.
"""
