# ADR-0024 — Minimal reproducible backtest core (scope minimization: 3 indices + INDIA VIX; no S3)

<!-- ADR NUMBER PROVENANCE (collision avoidance, 2026-07-15): 0020 is contended
     by slice-8 PR #1435; 0021 + 0022 are claimed in-flight by the orchestration
     + Dhan PR stack (PRs #1492/#1501/#1551); 0023 was claimed by the fills
     track on 2026-07-15 (coordinator arrival-order ruling). This ADR therefore
     takes 0024 per the coordinator assignment set (0021 orchestration · 0022
     Dhan stack · 0023 fills · 0024 removal); the coordinator ratifies numbers
     at landing. First-to-LAND wins ADR numbers (house rule). -->

**Decision IDs:** `DEC-SCOPE-MINIMAL-CORE-001` (MIGRATION.md #1835, the umbrella) +
`DEC-UNIVERSE-INDEX-VIX-ONLY-001` (#1836) + `DEC-STOCK-FNO-INGEST-RETIRE-001`
(#1837) + `DEC-CROSSVERIFY-S3-RETIRE-001` (#1838) + `DEC-BACKUP-LOCAL-ONLY-001`
(#1839).
**Date:** 2026-07-15.
**Status:** **PROPOSED** (operator-confirmed for authoring; acceptance = the
operator's merge of this PR). Provenance, stated honestly and never
overclaimed: operator directive 2026-07-15 (relayed via the coordinator
session; operator-confirmed in-session 2026-07-15 — the operator replied
"proceed" to an explicit description of exactly this PR: the minimal-core
scope decision PR, ADR + DEC #1835–#1839, opened as a draft for their
review). This is NOT recorded as a bare "operator decree"/"rulings final";
the operator's merge of this draft PR is the definitive acceptance act.
**Builds on:** `DEC-ENGINE-SPOT-ONLY-001` (MIGRATION.md #1790 — the live
engine-scope law this ADR extends from the ENGINE to the LAKE/ingest/ops
surfaces), the adversarially-verified removal inventory (2026-07-15, 8 claims
re-checked by verifiers C1–C8: 7 PARTIAL corrected + 1 CONFIRMED; base
main @ d6ac9f8, re-confirmed zero `brutex/` drift to this branch's base
c22495cb), and the merge-train phasing rules (run BEHIND the active ~67-PR
draft train; land the DECISION first, docs-only).

---

## 1. Context

BruteX today carries a far wider surface than the research core the operator
runs: a ~750-underlying lake universe (the 3 brute-force indices + INDIA VIX +
~212 F&O-eligible stocks per DEC-P0-063, MIGRATION.md #200, + ~540
NIFTY-Total-Market reference names per DEC-NTM-GROWW-REPOINT-001, MIGRATION.md
#694), single-stock F&O ingest, a daily TickVault cross-verification producer
that performs BruteX's only S3 WRITE (DEC-CROSSVERIFY-PRODUCER-001 +
DEC-CROSSVERIFY-S3-WRITE-POSTURE-001, MIGRATION.md #1776–#1777; spot-only
contract amendment DEC-CROSSVERIFY-SPOT-ONLY-001 #1788; withheld-regate
DEC-CROSSVERIFY-WITHHELD-REGATE-001 #1789), and an off-box restic backup
targeting S3 Object-Lock / B2 (DEC-BACKUP-OFFBOX-001, MIGRATION.md #1596 /
ADR-0017).

The operator directive of 2026-07-15 (relayed via the coordinator session;
operator-confirmed in-session 2026-07-15) reduces the system to a **minimal
reproducible backtest core**: the engine AND the lake serve exactly
NIFTY 50 / BANKNIFTY / SENSEX plus NSE-INDIAVIX (reference-only), the 3
indices' F&O stays in the lake, all stock breadth and every S3 dependency go,
and the Groww SSM-based auth broker stays (DEC-AWS-ONLY-CREDS-001, MIGRATION.md
#1065). DEC-ENGINE-SPOT-ONLY-001 (MIGRATION.md #1790) already narrowed the
ENGINE to the 3 spot indices; this ADR is the same discipline applied to the
DATA/ops perimeter.

The factual basis is the removal-track inventory (37 rows: 7 REMOVE ·
8 NARROW · 10 CHANGE · 12 KEEP), which went through an adversarial
verification round (verifiers C1–C8) that corrected 7 of its 8 load-bearing
claims before this ADR was authored — the corrected facts (not the raw
inventory) are what §2/§4 cite. Process constraint: main is a busy serialized
merge train (~67 open draft PRs); the scope DECISION lands now as this
docs-only PR (zero code contention), and the code lands in Phase B after the
train drains, per the governance phasing rule.

## 2. Decision (the load-bearing resolutions)

**Target end state (the minimal core):**

1. **Universe = 3 indices + INDIA VIX.** Engine + lake fetch/admission scope is
   exactly `NSE-NIFTY`, `NSE-BANKNIFTY`, `BSE-SENSEX` + `NSE-INDIAVIX`
   (reference-only, per CLAUDE.md §2 Golden Rule #3 — VIX is stamped on every
   observable trade per DEC-VIX-STAMP-WIRE-001, MIGRATION.md #1645, and never
   in the vocabulary). The ~212 F&O-eligible stocks + ~540 NTM reference names
   are retired from admission (DEC-UNIVERSE-INDEX-VIX-ONLY-001, #1836).
2. **Index F&O RETAINED in the lake.** The 3 indices' futures + options
   metadata/candles/Greeks ingest continues; ONLY single-stock F&O ingest is
   retired (DEC-STOCK-FNO-INGEST-RETIRE-001, #1837 — consciously superseding
   DEC-P0-063, MIGRATION.md #200). Index-F&O pullability rides the invariant
   that the 3 index rows stay `reference_only=FALSE`
   (`is_option_pullable`, `brutex/universe.py:568-600`) — NOT
   `fno_eligible_underlyings` (indices are never members) — and Phase B pins
   it with a regression test.
3. **No S3 anywhere.** The crossverify producer (BruteX's only S3 write,
   `brutex/crossverify/s3_publish.py`) is removed whole-package
   (DEC-CROSSVERIFY-S3-RETIRE-001, #1838); the backup remote target (S3
   Object-Lock / B2) is retired in favour of LOCAL-ONLY restic
   (DEC-BACKUP-LOCAL-ONLY-001, #1839 — superseding-in-part
   DEC-BACKUP-OFFBOX-001, MIGRATION.md #1596 / ADR-0017).
4. **SSM auth STAYS — "no S3" ≠ "no AWS".** The Groww token broker is
   AWS-SSM-only and read-only (DEC-AWS-ONLY-CREDS-001 #1065 +
   DEC-BROKER-READONLY-REAUTH-REREAD-001 #1072, both UNAFFECTED). KEEP set:
   `brutex/config/credentials_aws.py`, `brutex/adapters/groww/token_store_aws.py`,
   `brutex/adapters/groww/aws_client.py`, `deploy/aws_token_minter/`, the
   `up.sh` STS/SSM health checks, the `token-broker` extra + the mypy
   boto3/botocore overrides. Pinned Phase-B precondition (assurance-verified):
   BOTH the `token-broker` (SSM) and `crossverify` (S3) extras pin boto3 and
   SHARE the mypy boto3/botocore override — any boto3/S3 scrub is scoped to
   the `crossverify` extra + `crossverify/s3_publish.py` + the backup S3/B2
   remote path ONLY; the `token-broker` extra, its boto3 pin, and the shared
   mypy overrides survive verbatim (pyproject.toml:103-105 · :116-118 ·
   :340-341), else the Groww SSM credential read bricks.
5. **Isolated dual vendors.** Groww remains the Track-1 historical vendor
   (DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009); the `Dhan feed track` (in-flight
   sibling PR stack #1492/#1501/#1551 — its decision IDs are that track's, not
   decided here) proceeds as an ISOLATED vendor lane. Nothing in this ADR
   widens or narrows that track.
6. **Vocabulary untouched — sibling-owned.** This decision touches ZERO
   vocabulary files. The VWAP bits 52/53 question (tombstone design in a
   66-bit layout) is owned by the in-flight vocabulary sibling track
   (`vocab-66` / `DEC-VOCAB-VWAP-TOMBSTONE` family — in-flight, undecided
   here); any bit removal would re-key every run identity (Golden Rule #4)
   and is explicitly NOT part of this ADR.
7. **Lake DATA untouched.** This is a CODE + admission removal only; stored
   stock/F&O partitions under `bars/` remain on disk until the operator wipes
   them directly. Lake-walk read surfaces (preopen discovery, movers candidate
   scans) self-narrow only AFTER that wipe — disclosed, not hidden.

## 3. Why these choices (rationale)

- **Admission predicates, not store pruning (durability).** Pruning store rows
  alone is reversed by ANY instrument-master reload — including the startup
  auto-heal (`brutex/cli.py:3222`, `should_reload_instrument_master`), which
  re-admits ~750 names from the vendor CSV. The load-bearing narrowing is the
  admission predicates: `brutex/universe.py:977` (`is_universe_underlying`),
  `brutex/dashboard/universe_query.py:73` (`classify_instrument`),
  `brutex/adapters/groww/instruments.py:166` (`is_in_scope_row`) — verifiers
  C4/C5. Everything else follows from those three.
- **The VIX-scope key.** The narrowing key is `INDEX_UNDERLYINGS`/`V1_INDICES`
  (`brutex/universe.py:140-150` — the 3 indices PLUS INDIAVIX), NEVER
  `BRUTE_FORCE_INDEX_UNDERLYINGS` (`brutex/universe.py:222`, the 3-symbol
  engine set). Keying on the engine set silently kills INDIA VIX at admission
  and every pull enumeration; `build_vix_stamper` then degrades to a WARN +
  unstamped trades (a SILENT Golden-Rule-#3 violation only
  `doctor_live.py:1434` would flag) — verifier C8.
- **Local-only backup over full deletion.** The restic repo target is one env
  line (`BRUTEX_BACKUP_REPO=<local path>`; `brutex/config/backup_env.py:52`,
  exported verbatim as `RESTIC_REPOSITORY` at
  `brutex/backup/restic_runner.py:233` with no scheme validation — a local
  path just works), which keeps the health-hub backup lane
  (`brutex/dashboard/health_hub.py:75-79`, read-only `evaluate_deadman`
  reuse) and the `brutex backup` CLI wired with minimal edits, while S3/B2 is
  gone by configuration. Full deletion is the documented fallback variant
  (§4.5 row 5) — larger than it looks because the health-hub imports are
  MODULE-LEVEL (deletion without editing `health_hub.py` breaks the whole
  dashboard import — verifier C3).
- **Decision-first, code-behind-the-train.** Landing docs-only now records the
  scope law without contending for merge-train rebase slots; Phase-B deletion
  PRs then implement an in-repo operator-accepted ADR, each re-based off
  live-main-max just before its slot.

## 4. Consequences

**The three sharpest risks + mitigations:**

1. **Admission-predicate durability (re-widening).** Risk: a Phase-B PR that
   prunes data without narrowing the predicates re-widens on the next master
   reload (the `cli.py:3222` auto-heal). Mitigation: Phase-B PR-3 is ATOMIC —
   predicates + the four NTM module deletions + enumeration callers + both
   regression pins land in ONE PR (#1836 binds this as an acceptance
   obligation).
2. **The VIX-scope key (silent VIX death).** Risk: narrowing onto the engine
   trio kills INDIAVIX admission silently (WARN-only degrade). Mitigation:
   #1836 names the safe key (`INDEX_UNDERLYINGS`/`V1_INDICES`) and binds a
   VIX-admission regression pin + an index-F&O `reference_only=FALSE`
   pullability pin as Phase-B test obligations.
3. **Import-not-lane breakage.** Risk: deleting `brutex/backup/` outright
   breaks the dashboard at import (`health_hub.py:75-79` module-level
   imports), and deleting crossverify without BOTH `app.py` surfaces (routes
   + the pull-pipeline tail at `app.py:255-261`/`:1244-1245`/`:3092-3240`/
   `:5146-5156`) + the `pull_scope.py:331-469` tail seam leaves a broken
   tree. Mitigation: #1838/#1839 enumerate every surface; every deletion PR
   carries a post-deletion boot smoke + `brutex --help` + import-smoke
   acceptance gate.

**Phase-B PR sequence** (after the merge train drains; coordinator signals;
each PR re-audits against whatever has landed from in-flight PRs
#1482/#1492/#1501/#1522 at its slot):
- **PR-1 — crossverify whole-package removal** (#1838; self-contained; the
  only S3 write dies first).
- **PR-2 — backup remote → local-only** (#1839; smallest change completing
  "no S3").
- **PR-3 — ATOMIC universe narrowing** (#1836: admission predicates + four
  NTM module deletions + enumeration callers + the two pins, ONE PR).
- **PR-4 — stock-arm leaf cleanup** (#1837 consumers: `spot_drift.py`,
  `mapping_integrity(+_csv).py`, greeks/movers/cascade stock arms, routes,
  templates — pure consumers of PR-3, safe last).

**Superseded register (all CONSCIOUS supersessions, never silent —
Golden Rule #3):**

| Superseded decision | Where | By |
|---|---|---|
| DEC-P0-063 (200 NSE F&O stocks lake ingest) | MIGRATION.md #200 | #1837 |
| DEC-NTM-GROWW-REPOINT-001 (~750 NTM reference universe) | MIGRATION.md #694 | #1836 |
| The crossverify family (producer · S3 posture · day-derive · spot-only · withheld-regate) | MIGRATION.md #1776–#1778, #1788–#1789 | #1838 |
| DEC-BACKUP-OFFBOX-001 / ADR-0017 (offbox restic) — IN PART (remote target only) | MIGRATION.md #1596 | #1839 |
| The F&O-stock mapping/economics-integrity audit family (DEC-MAPPING-INTEGRITY-RECONCILE-001 · DEC-MAPPING-INTEGRITY-EXPORT-001 · DEC-INSTRUMENT-MASTER-INTEGRITY-001 · DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001) — retires with the stock arm in Phase-B PR-4; `economics_integrity.py`'s fate (retire vs retain-trivial over the 3 indices) is decided at PR-4 | MIGRATION.md #870 · #873 · #874 · #875 | #1837 |
| The legacy UNMERGED branch `claude/s3-reference-export` (bf3ff327; PR #1289 closed-unmerged 2026-07-09; DEC-REFERENCE-EXPORT-S3-001/#1605 exists only branch-side — zero main-side code or ledger residue; functionally superseded by the landed crossverify producer #1776) — NEVER MERGE; the remote branch is deleted in Phase-B PR-1 | branch-side only (no main ledger entry) | #1838 |
| Building on (not superseding): DEC-ENGINE-SPOT-ONLY-001 | MIGRATION.md #1790 | #1835 |

**Accepted losses (stated plainly):**
- **(a) The TickVault daily CSV handoff ENDS.** Removing crossverify
  terminates the per-symbol CSV publish to `tv-prod-cold/crossverify/*`
  entirely (MIGRATION.md #1776/#1788); TickVault must not expect those
  objects afterwards. Also lost: the crossverify same-day priority-pull path
  (its `pull_leg.py` performs a real vendor pull) — the nightly remains the
  only automated pull.
- **(b) INDEX fold-class drift recovery (the removal report's item 9).**
  Deleting `spot_drift.py` also
  removes the only case/space/hyphen FOLD-class drift recovery for INDEX spot
  dirs (`brutex/data/lake_keys.py:36-41`); the alias ladder covers only the
  alias class. Not an observed production class (writers use canonical keys)
  — accepted and recorded honestly in #1837.

## 4.5 Edge-case register (scenario → policy → owning phase)

| # | Scenario | Policy | Owner |
|---|---|---|---|
| 1 | Master reload / startup auto-heal re-admits stocks after store pruning | Predicates are the law; PR-3 atomicity; auto-heal covered by the predicate edit | PR-3 (#1836) |
| 2 | INDIAVIX dropped by an engine-trio-keyed narrowing | Safe key = `INDEX_UNDERLYINGS`/`V1_INDICES`; VIX-admission pin | PR-3 (#1836) |
| 3 | Index F&O silently loses pullability | `reference_only=FALSE` invariant pin (`brutex/universe.py:568-600`) | PR-3 (#1836) |
| 4 | Deleting crossverify strands an installed launchd job | Runbook note: uninstall `com.brutex.crossverify` BEFORE upgrading (the command ceases to exist) | PR-1 (#1838) |
| 5 | Operator later chooses backup FULL deletion | Documented fallback variant in #1839 (9 modules + `health_hub.py` lane edit + `cli.py:2983-3059` + ~13 test files) | PR-2 (#1839) |
| 6 | An S3 sweep deletes SSM-auth code | Acceptance gate: import-smoke proves `load_groww_aws_credentials` + `ParameterStoreTokenStore` + `readonly_reread` resolve; grep confirms no SSM-read module deleted | PR-1/PR-2 |
| 7 | ci.yml gates dangle on deleted modules | Acceptance gate: grep ci.yml for deleted basenames (0 hits) + no coverage-gate warn-skip; doctest floor 55→53 in the NTM PR | every Phase-B PR |
| 8 | Dangling MIGRATION/CLAUDE.md cites after deletion | Acceptance gate: citation-accuracy scanner + `validate_dec_ids.py` post-deletion; dangling cites re-pointed or retired | every Phase-B PR |
| 9 | Old stock partitions still render pre-wipe | Disclosed: lake-walk surfaces self-narrow only after the operator's data wipe (§2 item 7) | operator |
| 10 | In-flight sibling PR lands adjacent code | Every Phase-B PR re-audits against landed PRs #1482/#1492/#1501/#1522 at its slot | every Phase-B PR |

## 5. Phased implementation (smallest-first; each its own PR, 100% strict gates)

Phase A = THIS PR (docs-only: this ADR + MIGRATION #1835–#1839 + the ADR-0017
status note + the CLAUDE.md reconcile; ships DRAFT for the operator's review;
zero code/behavior change). Phase B = PR-1 → PR-2 → PR-3 → PR-4 as in §4,
each behind the drained merge train, each carrying the §4.5 acceptance gates,
each 100% line+branch on its touched modules per CLAUDE.md Golden Rule #12.

## 6. Honest envelope (Golden Rule #10 — what is NOT claimed)

- This PR changes ZERO code and ZERO behavior — it records decisions. Nothing
  is removed, narrowed, or reconfigured by merging it.
- Acceptance is the OPERATOR'S MERGE of this draft PR; until then every
  decision here is PROPOSED (operator-confirmed for authoring only).
- Lake DATA is untouched by the whole track — the operator wipes stored
  partitions directly; read surfaces self-narrow only after that wipe.
- The vocabulary (incl. VWAP bits 52/53) is sibling-owned and undecided here;
  no vocabulary file is touched by this ADR or its Phase-B PRs.
- The `Dhan feed track` is an isolated in-flight sibling; this ADR neither
  authorizes nor constrains it beyond "isolated dual vendors".
- No wall-clock/throughput claim is made for the narrowed system; the §8
  hardware envelope + sweep SLA statements in CLAUDE.md are unchanged.
- The file:line citations herein were adversarially verified at main @
  d6ac9f8 (zero `brutex/` drift to this branch's base c22495cb); Phase-B PRs
  re-verify at their own slots — cites here are point-in-time.

## References

- MIGRATION.md #1835–#1839 (this ADR's five DECs); #1790
  (DEC-ENGINE-SPOT-ONLY-001, the supersession-language template); #200 ·
  #694 · #1776–#1778 · #1788–#1789 · #1596 (the superseded register); #1065 ·
  #1072 (the SSM-auth pledge, UNAFFECTED); #1612 (the nightly chain, whose
  LOCAL backup leg `brutex/nightly/backup.py` imports nothing from
  `brutex/backup/` and survives any backup decision); #1645 (VIX stamping).
- ADR-0017 (`docs/adr/0017-offbox-backup-restic.md`) — superseded-in-part
  status note appended by this PR.
- The removal-track inventory + adversarial verification round (verifiers
  C1–C8), session scratchpad `removal-report.md`, 2026-07-15.
