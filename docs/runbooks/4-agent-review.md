# Runbook — 4-Agent Hostile Review Pattern

> **Target audience:** Claude Code working on BruteX, future operators, and
> reviewers auditing the discipline trail.
> **Time required:** ~30-45 minutes wall-clock per major-review pass
> (4 parallel agents finish in ~5-10 min each; reconciliation takes
> the rest).
> **Prerequisites:** Branch contains the design / spec / code under review,
> committed; `docs/MIGRATION.md` ready to receive a decision-ID entry.

This runbook implements the non-negotiable review gate documented in
`CLAUDE.md §9` (Golden Rule #9 — "Hostile review before code") and
the closing matrix at `docs/TRACK1_PHASE_PLAN.md §"The 4-Agent Hostile
Review Pattern"`. Phase 1 Week 3 deliverable per
`docs/TRACK1_PHASE_PLAN.md §"Phase 1 / Week 3"`.

---

## 1. What this runbook is for

BruteX is a backtest engine for live capital decisions. A design defect
(look-ahead leak, hallucinated rate-limit, wrong SLA claim, missed
NSE microstructure rule) survives all the way to a "trade-this"
candidate strategy if no reviewer catches it. The 4-agent pattern
exists because **a single agent reviewing its own design is
structurally unable to catch its own blind spots** — the model that
wrote the doc shares the same training-data gaps as the model
reviewing it.

Parallel adversarial review by 4 agents with **disjoint priors**
(different tool-sets, different framing prompts, different success
criteria) is the cheapest dependable defense against:

- factual hallucination (Groww API endpoint that doesn't exist,
  fabricated NSE freeze-quantity, invented STT rate)
- design contradiction across docs (Track 1 says skip 09:15, Track 2
  says use 09:15)
- silent scope creep (someone slipped in an option-writing variant)
- over-optimistic SLA claims (10× throughput inflation;
  see `MIGRATION.md` entry #138 footnote)
- statistical defect (DSR with wrong effective-N; embargo too small
  for Mode B horizon)

The 2026-05-15 4-agent review caught **23 distinct findings + 1
consolidation** before any Phase 2 code landed — see
`docs/track2/ASSURANCE_ROUND_2026-05-16.md` for the canonical case
study. Without that review the broker-vs-Groww adapter mismatch,
10× throughput inflation, spot-index tradability error, basis-point
vs absolute-point tolerance bug, and SENSEX freeze-qty contracts-vs-lots
conflation would have shipped.

---

## 2. The 4-agent pattern at a glance

```
   ┌──────────────────────────────────────────────────────────┐
   │  REVIEW SUBJECT: design doc / spec / PR diff / new code  │
   └────────────────────────┬─────────────────────────────────┘
                            │
              ┌─────────────┼─────────────┬──────────────┐
              │             │             │              │
              ▼             ▼             ▼              ▼
       ┌────────────┐┌────────────┐┌────────────┐┌────────────┐
       │  Agent A   ││  Agent B   ││  Agent C   ││  Agent D   │
       │  (Audit)   ││ (Architect)││ (Hostile)  ││ (Reality)  │
       │  Explore   ││   Plan     ││  general-  ││  general-  │
       │            ││            ││  purpose   ││  purpose   │
       └─────┬──────┘└─────┬──────┘└─────┬──────┘└─────┬──────┘
             │             │             │              │
             └─────────────┴──────┬──────┴──────────────┘
                                  ▼
                  ┌───────────────────────────────┐
                  │  Reconciliation by Claude     │
                  │  → patches to spec + code     │
                  │  → MIGRATION.md DEC entry     │
                  │  → PR body §"4-agent hostile  │
                  │     review" subsection        │
                  └───────────────┬───────────────┘
                                  ▼
                          ┌───────────────┐
                          │ Operator sign │
                          │ -off / merge  │
                          └───────────────┘
```

| Agent | Subagent type | Role | Reads | Cannot do |
|---|---|---|---|---|
| **A — Audit** | `Explore` | Repo ground-truth survey: where does the subject actually live? What contradicts it? What's stale? | Tools: `Read` + `Grep` + `Glob` only (no `Edit`, no `Write`) | Cannot author patches; only reports findings |
| **B — Architect** | `Plan` | Forward design check: does the subject make sense end-to-end? Where will it break in 6 months? | Tools: all read tools | Cannot author patches; outputs a design plan/critique |
| **C — Hostile** | `general-purpose` | Adversarial: hallucination hunt, factual claim verification, logic-flaw fishing, OWASP-style trust-boundary review | Tools: all (incl. `WebFetch` for spec verification) | Should NOT make code changes during the review pass |
| **D — Reality** | `general-purpose` | Public-benchmark + hardware-budget sanity: are the numbers physically achievable on stated hardware? Cite public references. | Tools: all (esp. `WebFetch`, `WebSearch`) | Should NOT make code changes during the review pass |

The 4 agents share **only the review subject** — they do NOT see each
other's outputs. This is the disjoint-priors design: 4 sets of
findings converge on the same defect from 4 different angles, raising
the probability of catching a true defect to ~`1-(1-p)^4` where `p`
is the per-agent detection probability.

---

## 3. When to run which variant

| Trigger | Variant | Agents | Latency | Required |
|---|---|---|---|---|
| Major spec change (Track 1/2, ADR-creating PR, vendor pivot, scope expansion) | **Full 4-agent** | A + B + C + D | ~30-45 min | YES — operator-decree (CLAUDE.md §2 rule 9) |
| 18-requirement compliance audit (e.g. honest-audit refresh, 100% standards row reclassification) | **2-agent compliance** | A + C | ~15-20 min | YES — DEC-AUDIT-TRUTH-RECONCILE-001 precedent |
| Refactor PR (no behavior change, renames, internal moves) | **1-agent hostile** | C only | ~5-10 min | YES — DEC-PROTOCOL-001 |
| Pure formatting / docs typo / link fix | none | — | 0 | discretionary; PR body still cites "no 4-agent needed (scope rationale)" |
| **Phase-gate PR** (end-of-phase sign-off — Phase 1, Phase 2, Phase 3, Phase 4) | **Full 4-agent** | A + B + C + D | ~30-45 min | YES — non-negotiable per phase plan |

When in doubt, use a heavier variant. The cost of an extra agent is
minutes of wall-clock; the cost of a missed defect is days of rework
plus a potential live-capital loss in Track 2.

**Branch-specific suppression:** the 1-agent-hostile pattern still
applies to refactors. **Never skip the 4-agent gate on phase-end PRs.**

---

## 4. Agent prompts (copy-paste templates)

Each prompt below is **self-contained** — pasted verbatim into the
`Agent` tool's `prompt` field. The 4 agents run in parallel in the
**same message** (4 `Agent` calls in one tool block). The reviewing
Claude prepares the prompts, fires them all, then waits for the
returns before reconciling.

### 4.1 Agent A — Audit (subagent_type: `Explore`)

```
You are the AUDIT agent in a 4-agent hostile review of BruteX. Your
job is repo ground-truth: find what's actually in the code/docs RIGHT
NOW that contradicts, supports, or is stale relative to the review
subject. You may NOT propose patches — only report findings.

Review subject: <one-paragraph description of the doc/spec/diff/PR>

Specific things to grep for:
1. Every file path the subject cites — does that file actually exist?
   Does it actually say what the subject claims it says?
2. Every decision ID (DEC-*) referenced — does it exist in MIGRATION.md?
   What does it actually decide vs what the subject claims it decides?
3. Every count/cardinality claim (e.g. "52 conditions", "24 invariants",
   "5 r/s rate limit", "21 timeframes") — is the claim consistent with
   the corresponding source-of-truth file (CLAUDE.md §6, TRACK1_*.md,
   docs/STANDARDS_100PCT_HONEST_AUDIT.md)?
4. Stale references — anything pointing to retired stubs
   (REQUIREMENTS.md, ROADMAP.md, REQUIREMENTS_SIMPLE.md,
   adapters.md), retired adapters (Dhan), removed concepts.
5. Untested code paths the subject claims are tested.

Output format:
- A bulleted list of findings, severity-graded:
  * CRITICAL: factual claim contradicted by file
  * HIGH: count/citation/scope discrepancy
  * MID: stale reference or incomplete sentence
  * LOW: nit / style
- For each finding cite the file path + line number where the
  contradiction lives, and the file path + line number where the
  ground truth lives. No file-path = invalid finding.
- End with: "Total findings: N CRITICAL / N HIGH / N MID / N LOW".

If you find zero findings, end with: "Total findings: 0 — agent A
concludes the subject is consistent with current repo state."

Keep your output under 1500 words. Cite ruthlessly. Hallucinate
nothing.
```

### 4.2 Agent B — Architect (subagent_type: `Plan`)

```
You are the ARCHITECT agent in a 4-agent hostile review of BruteX. Your
job is forward design check: does the subject hold up end-to-end?
What's the 6-month failure mode? What invariant breaks if we extend
this in the obvious direction? You may NOT author patches.

Review subject: <one-paragraph description of the doc/spec/diff/PR>

Specific design questions to answer:
1. End-to-end flow: trace one execution from the subject's entry
   point to the persisted result. List every hop. Which hop is
   fragile? Which is over-coupled? Which lacks an inverse / round-trip
   check?
2. The 5-pillar test (CLAUDE.md §2 rule 11): does the subject obey
   COMPREHENSIVE / COMMON / DYNAMIC / SCALABLE / AUTOMATED? Cite the
   specific evidence (or lack of) for each pillar.
3. Honest-envelope test (CLAUDE.md §2 rule 10): is there an SLA
   claim? Hardware envelope named? Public benchmark cited? If no,
   that's a finding.
4. Reproducibility (CLAUDE.md §2 rule 4): does the subject preserve
   the `blake3(strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖
   canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖
   code_commit_hash)` reproducibility hash chain? Or does it
   introduce hidden state that breaks bit-for-bit replay?
5. O(1) discipline (CLAUDE.md §2 rule 5): does the subject add any
   O(N) or O(N log N) hot path? Cite the hot-path file.
6. Audit trail (CLAUDE.md §2 rule 8): does every material action
   append to the hash-chained JSONL audit log?

Output format:
- One paragraph per design question, ≤4 sentences each
- Bulleted "concerns" list at the end, severity-graded
  CRITICAL / HIGH / MID / LOW with rationale
- End with: "Architect verdict: PROCEED | PROCEED WITH FIXES | BLOCK"
  and one-sentence justification

Keep your output under 1500 words. Cite file paths. Reference
specific lines when calling out specific concerns.
```

### 4.3 Agent C — Hostile (subagent_type: `general-purpose`)

```
You are the HOSTILE agent in a 4-agent hostile review of BruteX. Your
job is to find every defect. Assume the author is wrong; require them
to prove every claim. Hallucination hunt + factual verification +
logic-flaw fishing. You may NOT change code during the review.

Review subject: <one-paragraph description of the doc/spec/diff/PR>

Specific things to verify hostilely:
1. EVERY factual claim about Groww / NSE / BSE / NIFTY / BANKNIFTY /
   SENSEX / INDIA VIX / brokerage / taxes — verify against the named
   primary source. If the source can't be reached or doesn't say
   what the subject claims, that's a CRITICAL finding. Use WebFetch
   if needed. Cite primary URLs.
2. Every rate-limit / quota / latency / throughput / cardinality
   number — is it physically achievable? Cite a comparable system's
   public benchmark if claiming throughput.
3. Every code snippet — does it actually compile / parse / pass
   ruff + mypy? Does it match the imports? Are the type annotations
   correct?
4. Every "see file_path" / "see MIGRATION.md entry #N" — does the
   reference actually exist and actually support the claim?
5. Cross-doc contradiction: does the subject contradict CLAUDE.md
   §1 (scope lock) / §2 (12 golden rules) / §3 (branching) / §4
   (coding standards) / §6 (Groww facts) / §7 (microstructure
   facts) / §8 (hardware envelope)? If yes, that's CRITICAL.
6. Scope creep: does the subject add any capability that wasn't
   explicitly authorized by an existing operator decree or
   DEC-* entry? If yes, that's CRITICAL.
7. Statistical / methodological flaws: look-ahead bias, train/test
   contamination, p-hacking risk, multiple-testing correction
   misuse, DSR effective-N miscalculation, embargo too small for
   trade horizon.
8. Security / OWASP: untrusted input, command injection, path
   traversal, secrets in logs, secrets in env-var defaults.

Output format:
- Numbered findings, each with:
  * Severity: CRITICAL / HIGH / MID / LOW
  * Claim being challenged (quote the subject verbatim)
  * Why it's wrong (primary citation or logic)
  * Proposed fix (one sentence; not a patch)
- End with: "Hostile verdict: N CRITICAL / N HIGH / N MID / N LOW
  findings. Subject MUST FIX [list] before merge."

Keep your output under 2000 words. Cite primary sources. If a claim
genuinely checks out, say so explicitly. Do not invent defects.
```

### 4.4 Agent D — Reality (subagent_type: `general-purpose`)

```
You are the REALITY-CHECK agent in a 4-agent hostile review of BruteX.
Your job is the public-benchmark + hardware-budget sanity gate. Every
SLA claim, throughput claim, latency claim, capacity claim, must be
defensible against a comparable open-source / institutional
reference. You may NOT change code during the review.

Review subject: <one-paragraph description of the doc/spec/diff/PR>

Specific reality checks:
1. Hardware envelope: CLAUDE.md §8 names Apple M4 Pro 14-core / 48 GB
   RAM / 8 TB SSD as the production reference. Does the subject
   claim performance achievable on THAT hardware? If a different
   envelope is claimed, is it stated explicitly per Golden Rule #10?
2. Throughput / latency: any number claimed (combos/sec, bars/sec,
   trials/sec, ops/sec, μs p99) MUST be cross-referenced against at
   least one public benchmark (vectorbt, NautilusTrader, Jesse,
   QuantConnect, Backtrader, Zipline, hummingbot, freqtrade) OR a
   first-principles memory-bandwidth calculation citing
   `docs/TRACK1_PERFORMANCE_BUDGET.md`. If neither, that's CRITICAL.
3. Sweep wall-time: any total wall-time claim ("X-day production
   sweep", "Y trials in Z hours") must be derivable from the
   throughput claim × the cardinality, with an explicit speedup
   factor for parallelism. If not, CRITICAL.
4. Network / API: any external-vendor SLA (Groww 5 r/s, 100k/day)
   must match the verified table in CLAUDE.md §6. If the subject
   claims a different rate, it must cite the source.
5. Statistical sample size: any "N trades", "lockbox window", "DSR
   threshold" claim must be derivable from the data scope (rolling
   2021-01-01 → today, ~5 years × 3 instruments × 21 TFs × signal
   firing rate). If the subject claims an N that exceeds the
   available data, CRITICAL.
6. Storage / memory: any "X GB" or "Y TB" claim must check against
   the 48 GB RAM / 8 TB SSD envelope. Out-of-memory risk if total
   working set > 48 GB.

Output format:
- One paragraph per reality check, with the claim, the cross-reference
  (URL or file:line), and the verdict (PASS / FAIL / NEEDS-WIDER-MARGIN)
- Bulleted final summary with severity-graded findings
- End with: "Reality verdict: N CRITICAL / N HIGH / N MID / N LOW. No
  performance claim survives without a public-benchmark or memory-
  bandwidth derivation cite."

Keep your output under 2000 words. Cite specific URLs and BruteX file
paths. Distrust round numbers ("100K/sec", "1M/sec", "10× faster") —
demand the calculation.
```

---

## 5. Reconciliation (the human-loop step)

After all 4 agents return, the reviewing Claude (not an agent) must:

1. **Deduplicate findings.** Multiple agents often catch the same
   defect from different angles — that's a strong signal, not a
   double-count. Merge same-root-cause findings under a single ID,
   noting which agents found it.

2. **Sort by severity.** CRITICAL > HIGH > MID > LOW. Within
   severity, sort by "fixes-on-branch-now" (cheap) vs "needs-new-PR"
   (expensive).

3. **Triage each finding into one of:**
   - **FIX-IN-PR** — patch lands in the same PR, before push
   - **NEW-PR** — separate focused PR (file the link in PR body)
   - **DEFERRED** — explicit DEC-* with rationale + future-PR target
   - **REJECTED** — agent was wrong; rebut with primary citation

4. **Patch the subject** — apply the FIX-IN-PR set to the actual
   file. Re-run lint / type-check / smoke before committing.

5. **Write the MIGRATION entry** — append a new `DEC-*` to
   `docs/MIGRATION.md` with:
   - Decision ID (`DEC-<TOPIC>-<NNN>`)
   - One-paragraph context (operator directive that triggered review)
   - Findings summary (count + severity per agent)
   - Resolution table (finding → triage → file path)
   - Builds-on + closes citations

6. **Stamp the PR body** with the 4-agent block — quote each agent's
   verdict line + cite the MIGRATION entry. This is the
   `4-agent hostile review` Z+ marker per
   `scripts/ci/zplus-pr-guard.sh`.

7. **NEVER hide a CRITICAL finding.** Even if the operator is in a
   hurry, every CRITICAL becomes either a same-PR fix or a tracked
   NEW-PR — never a silent drop.

---

## 6. Example: the 2026-05-15 4-agent review

Concrete walkthrough — see `docs/track2/ASSURANCE_ROUND_2026-05-16.md`
for the canonical case study. The review subject: Track 1 backtest spec
v2 (with the broker→Groww migration) + Track 2 master spec.

| Step | Output |
|---|---|
| 1. Subject framed | "Track 1 v2 spec + Track 2 master spec — final assurance pass before any Phase 2 code lands" |
| 2. 4 agents fired in parallel | Agent A (Explore) → cross-doc consistency · Agent B (Plan) → hostile senior-quant review · Agent C (general-purpose) → primary-source verification of 5 UNVERIFIED items · Agent D (general-purpose) → 2024-2026 institutional methodology currency |
| 3. Findings returned | A: 0 contradictions · B: 15 flaws (4 CRITICAL + 5 HIGH + 6 MID) · C: 4 of 5 UNVERIFIED items LOCKED + 1 fact-corrected · D: 3 methodology upgrades |
| 4. Reconciliation | 23 distinct findings + 1 consolidation → 23 decision IDs created (DEC-LOCK-001, DEC-DSR-001, DEC-RISK-013/014/015/016, DEC-STAT-001/002/003, DEC-COST-001/002/003/004, DEC-REPRO-001, DEC-PLUGIN-002, DEC-INFRA-001, DEC-CAL-001, DEC-TAX-009, DEC-OBS-009, DEC-DOCS-001) |
| 5. Patches applied | TRACK1_BACKTEST_SPEC.md (Stage 4.7 lockbox, Stage 4 embargo) + TRACK2_RISK_MGMT.md (§1.2, §2A, §4.3, §7.1) + COSTS_VERIFIED.md (§4, §13) + CALCULATOR_SPEC.md (§4, §4.2, §16) + 6 more |
| 6. MIGRATION entries | #115 .. #137 (the entire range was this review pass) |
| 7. PR-body 4-agent block | `MIGRATION.md` entries cited per finding; reviewer signed off |

**Pattern this case study proves:** without the 4-agent pass, BruteX
would have shipped the broker-vs-Groww adapter mismatch (catastrophic
in Phase 2 when first call fails), the 10× throughput inflation
(catastrophic in Phase 4 when the production sweep actually takes 10×
longer), and the SENSEX freeze-qty contracts-vs-lots conflation
(catastrophic in Track 2 when the operator hits an unexpected
order-rejection at the 50th lot). All caught **before** code touch.

---

## 7. Sign-off gate (operator-side)

After the reconciliation step, the PR is ready for operator review.
The operator does NOT need to re-run the 4 agents — but they MUST
verify:

- [ ] PR body has all 6 Z+ marker phrases per
  `scripts/ci/zplus-pr-guard.sh`
- [ ] PR body has a `## 4-agent hostile review` subsection citing all
  4 agent verdicts (or 2 for compliance variant; or 1 for refactor)
- [ ] PR body has a `## Honest envelope` subsection with at least one
  ✅ REAL / 🟡 PARTIAL / 📋 ROADMAP qualifier
- [ ] `docs/MIGRATION.md` has a new DEC entry with the citation chain
- [ ] CI is green (or has documented non-blocking flake per the
  `m4pro-absolute-throughput` / `Benchmarks` pattern in
  `docs/runbooks/github-branch-protection-setup.md §3`)
- [ ] No CRITICAL finding is unresolved — every one is FIX-IN-PR,
  NEW-PR-with-link, or DEFERRED-with-DEC

If all 6 boxes check, the operator squashes. If any unchecks,
fix → re-push → re-run CI → re-verify.

---

## 8. What the runbook is NOT

- **NOT** a substitute for unit / property / golden / integration /
  bench tests. The 4-agent review is the *design* gate; the test
  pyramid is the *code* gate. Both are required.
- **NOT** a license to skip the spec read. Every reviewing Claude
  MUST still read `CLAUDE.md` + `TRACK1_BACKTEST_SPEC.md` + the
  last 10 `MIGRATION.md` decisions before triggering the agents.
- **NOT** an excuse for the operator to skip their own verify pass.
  The operator's eyes on the diff are the final gate.
- **NOT** automatically run by CI. The 4-agent pass is a Claude-
  initiated discipline; CI only enforces the Z+ marker presence
  via `zplus-pr-guard.sh`.

---

## 9. Iteration history

| Date | Change |
|------|--------|
| 2026-05-27 | Initial runbook landed (DEC-PHASE1-WEEK3-RUNBOOK-001) per Phase 1 Week 3 deliverable; documents the pattern already used on the 2026-05-15 assurance round (`docs/track2/ASSURANCE_ROUND_2026-05-16.md`) and codified in `CLAUDE.md §2 rule 9`. |

---

## 10. Cross-references

- `CLAUDE.md §2 rule 9` — Golden Rule mandating hostile review
- `CLAUDE.md §9` — "When in doubt" workflow with the 4-agent step
- `docs/TRACK1_PHASE_PLAN.md §"The 4-Agent Hostile Review Pattern"` —
  closing summary of the pattern at the end of every phase
- `docs/track2/ASSURANCE_ROUND_2026-05-16.md` — canonical case study
- `docs/MIGRATION.md` entries #115 .. #137 — the 2026-05-15 review pass
- `docs/STANDARDS_100PCT.md §17` — deep-research protocol matrix
- `scripts/ci/zplus-pr-guard.sh` — the marker-presence enforcement
  this runbook informs
