# Definition of Ready — the plan-first gate

> **Operator decree, 2026-06-05:** *"until or unless thoroughly designing /
> deriving an extensive plan by covering everything we should never ever go
> ahead with implementation."*
>
> **Decision:** DEC-DEFINITION-OF-READY-001 (MIGRATION.md #484).

This repo already has a **Definition of *Done*** — the `ci-ok` merge gates
(`docs/MERGE_READINESS_GUARANTEE.md`). Those catch defects *after* code
exists. They do **not** catch the failure the operator fears most:
*"a situation / functionality nobody thought to cover."* You cannot test
your way out of a scenario you never imagined.

The **Definition of Ready** is the complement: a mechanical gate that
refuses to accept *any* implementation until the plan has, in writing,
enumerated every scenario and failure mode, named the real-run proof, and
stated the rollback. **Plan-first, not plan-maybe.**

---

## The honest boundary (Golden Rule #10)

This gate guarantees the **existence and completeness** of a plan. It does
**not**, and cannot, guarantee the plan is *correct* — that the author
imagined the *right* scenarios. No machine can grade the wisdom of a plan.

So this is **not** a promise of zero bugs. It is a promise that:

> Nothing is built blind. Every change arrives with its scenarios, its
> failure modes, its proof-of-working, and its undo — all written down
> *before* the code is accepted, where a reviewer can see a gap.

That is the achievable guarantee. Anyone promising more is not being honest
with you.

---

## The 5 required sections

Every PR body MUST contain these five headings (matched case-insensitively
as substrings by `scripts/ci/definition-of-ready-guard.sh`). A blank section
is a planning gap and the gate **fails closed**.

| # | Section marker | What it must answer |
|---|----------------|---------------------|
| 1 | **Scenarios covered** | Every path: happy, edge, **empty/zero**, **failure**, and **concurrent**. List them — do not hand-wave "all cases". |
| 2 | **Failure modes** | For each way it can fail (bad input, missing file, network down, partial write, race), what does the system *do*? Raise? Retry? Exit non-zero? |
| 3 | **Real-run proof** | The **exact command** that demonstrates it working against reality — not "unit tests pass". e.g. `make run` boots + serves 200; `brutex sweep --demo` emits a result row. |
| 4 | **Rollback plan** | How to undo this safely. `git revert`? A migration-down? A feature flag? |
| 5 | **Out of scope** | What this PR explicitly does **not** do — so silent scope-creep (Golden Rule #3) is impossible. |

A ready-to-copy block lives in `.github/PULL_REQUEST_TEMPLATE.md`.

---

## Enforcement

- **Script:** `scripts/ci/definition-of-ready-guard.sh` — reads the PR body,
  exits 1 (fail-closed) if any of the 5 markers is absent.
- **CI job:** `definition-of-ready` in `.github/workflows/ci.yml`, gated on
  `pull_request` events (skipped on raw pushes, which have no PR body).
- **Tests:** `tests/redteam/test_redteam_definition_of_ready_guard.py` —
  asserts the fail-closed + override-audit + bot-exempt contract (12 cases).
- **Exemptions:** `renovate[bot]` / `dependabot[bot]` are author-exempt
  (they open only dependency-bump PRs and structurally cannot carry a plan),
  mirroring DEC-P0-058. Every exemption is audit-logged.
- **Operator override:** `OPERATOR_OVERRIDE=true OPERATOR_OVERRIDE_REASON="<why>"`
  — bypass is appended to `scripts/ci/pre-merge-overrides.log` for review.

### Rollout

Per the same advisory→hard ratchet used for vulture / duplication
(DEC-P0-001/003): the job is wired to **run and report**. Whether it is a
**hard `ci-ok` gate** (blocks merge) or **advisory** is one line — its
presence in `ci-ok.needs`. See MIGRATION.md #484 for the current tier and
the flip condition (in-flight PRs adopt the section first, then it goes
hard, so no open PR is blocked retroactively without warning).
