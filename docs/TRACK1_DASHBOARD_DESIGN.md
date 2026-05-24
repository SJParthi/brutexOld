# BruteX — Dashboard Design Spec (Track 1)

> **Status:** LOCKED 2026-05-24 at v29. DEC-DASHBOARD-V29-001
> (MIGRATION.md entry #222).
>
> **Canonical mockups** (in this repo, not in `/tmp/`):
> - `docs/dashboard/mockups/v29.html` — current locked design
> - `docs/dashboard/mockups/v28.html` — v24 palette + v25/v26/v27 motion stack
>
> **Backend module:** `brutex/dashboard/app.py` (FastAPI + WebSocket fan-out).
>
> **Authority order:**
> 1. This file (`TRACK1_DASHBOARD_DESIGN.md`) — locks design + data contract
> 2. `docs/dashboard/mockups/v29.html` — canonical reference for visual+motion+layout
> 3. `brutex/dashboard/app.py` — implementation that must satisfy the data contract
>
> **Per CLAUDE.md Golden Rule #11 (COMMON · DYNAMIC · SCALABLE · AUTOMATED):**
> the dashboard is **SCALABLE** by design — every number on screen flows
> from `DashboardState` snapshot, never hardcoded. The current v29 mockup
> ships with PLACEHOLDER numbers behind a `PREVIEW DATA` badge until the
> live wiring is complete (see §6 Data Contract).

---

## 1. Single source of truth

| Layer | Lives in | Locked? |
|---|---|---|
| Visual + motion + layout | `docs/dashboard/mockups/v29.html` | ✅ v29 (this PR) |
| Palette tokens | `:root { ... }` block at top of v29.html | ✅ v28-hybrid (§3 below) |
| Animation vocabulary | `@keyframes ...` blocks + `.v2N-*` class names | ✅ v22-v29 indexed (§4 below) |
| Motion-first meaning rules | v26 block (lines ~514-601 of v29.html) | ✅ v26-locked (§5 below) |
| Data contract | This doc §6 + `brutex/dashboard/app.py::DashboardState` | 🟡 schema locked, live wiring in flight |
| Honest-envelope badge | `.preview-badge` in v29.html | ✅ v29.1 (§7 below) |

**Mandatory:** every future dashboard PR MUST update this doc AND the
mockup. The mockup IS the spec for visuals; this doc IS the spec for
data + semantics + locked palette + animation vocabulary.

---

## 2. Why dashboard exists (FR-UI-006)

Per `REQUIREMENTS.md` FR-UI-006 (now superseded by `TRACK1_BACKTEST_SPEC.md` §dashboard):

- **Read-only** snapshot of pipeline state. Operator never edits via
  dashboard; all mutation flows through the orchestrator.
- **Local-only** (`127.0.0.1:9080`) — no auth layer, no public IP.
- **Three audiences** in three modes:
  - **Operator** — dense ops view (sweep IDs, hashes, audit chain status)
  - **Share** — plain-English view for non-traders (jargon hidden,
    plain-English equivalents shown)
  - **Reel** — vertical 9:16 slideshow for social media (4s auto-advance)
- **Honest-envelope first**: any number that isn't live-wired carries
  a visible PREVIEW DATA badge per Golden Rule #1.

---

## 3. Palette tokens (v28 hybrid — locked)

The v28 hybrid palette was chosen over v27's all-gold ("Bloomberg
Terminal neutrality") and v24's vibrant-only ("Robinhood trader") after
the operator's 2026-05-24 review: **trader-familiar green/red base with
gold accent layer on top.**

```css
:root {
  /* Surfaces */
  --bg: #0A0D11;
  --surface: #131722;
  --surface-2: #1A1F2C;
  --surface-3: #232938;
  --border: #1E222D;
  --border-strong: #2B313F;

  /* Text */
  --text: #E5E8EC;
  --text-bright: #FFFFFF;
  --muted: #787B86;

  /* Win tokens — v24 vibrant teal-green (trader-familiar) */
  --green: #26D9B0;
  --green-bright: #4FFFD0;
  --green-dim: #1A8870;

  /* Loss token — v24 red (alarms on big losses; trader-familiar) */
  --red: #FF5C5C;

  /* Accent tokens — drive v25/v26/v27/v29 gold/blue/purple motion stack */
  --yellow: #FFB74D;   /* gold — sparkles, light-beam, KPI sweep, reel ring, PREVIEW badge */
  --blue: #5B8DEF;     /* analytical/calm — funnel mid-stages */
  --purple: #9D7BFF;   /* premium — funnel entry, podium silver, brand-logo gradient */
  --pink: #FF6B9D;     /* reel mode accent */
}
```

**Semantic meaning:**

| Token | Means | Used by |
|---|---|---|
| `--green` | "won / positive P&L / valid signal" | rule cards NET cell, win-row, dir-arrow.up, podium gold tilt |
| `--red` | "lost / negative P&L" | loser rows, drawdown KPI |
| `--yellow` (gold) | "premium / winner highlight / motion accent" | top-1 podium, sparkle, light-beam, KPI sweep, PREVIEW DATA badge, reel auto-ring |
| `--blue` | "neutral analytical" | mid-funnel bars, NIFTY badge, brand-logo gradient |
| `--purple` | "entry-point / luxury" | funnel start row, BANKNIFTY badge, brand-logo gradient |
| `--muted` | "secondary / loss-faded (v26)" | labels, dim text, loser opacity layer |

**Rule:** never hardcode hex values in dashboard CSS — always use a
token. Adding a new color = updating this table first.

---

## 4. Animation vocabulary (v22 → v29 — locked)

Every animation block in v29.html is indexed below. The class-name
convention is `.vNN-{slug}` so version archaeology stays grep-able.

### v22 baseline (top-combos table polish)
- Class: `.top-combos-table` — base table layout.

### v23 (animated funnel pipeline + reel progress dots)
- `@keyframes barGrow` — funnel bar fill from `scaleX(0) → scaleX(1)`.
- `.reel-progress-dots` — bottom-center dot indicator in reel mode.
- `.mode-toggle button.active::after` — live-pulse dot inside active mode pill.

### v24 (viral attraction pack)
- `.slot-reveal` — TikTok-style scramble→lock character animation for combo IDs.
- `.particle-burst .p` — 60-particle burst on load from hero rank pill center.
- `@keyframes cameraShake` — **REMOVED in v27/v28** (caused nausea on long sessions; replaced by KPI scale-pop).
- `.viral-hook` — first-frame HOOK banner (top sticky band).
- `.hero-rank-pill` — glow ring + cumulative attention magnet.

### v25 (motion-first redesign)
- `.funnel25` + `.funnel25-row` + `.funnel25-bar` — clean vertical funnel staircase (replaces overlap-prone SVG).
- `.f25-grid` + `.f25-card` — 9 institutional filter cards.
- `.f25-stack` / `.f25-ruler` / `.f25-blindfold-wrap` / `.f25-trophies` / `.f25-balloon` / `.f25-magnifiers` / `.f25-clocks` / `.f25-vault` / `.f25-cards` — per-filter mini-animation icons.
- `.inst-badge.nifty/banknifty/sensex` — instrument badge tints in trades table.
- `@keyframes goldTrail` — podium gold-card pulsing trail (v25.3).

### v26 (motion-first meaning carriers)
**This is the rule: color is aesthetic; motion + weight carry semantic meaning.**
- `.dir-arrow.up { color: var(--yellow); animation: arrowFlyUp; }` — up-arrow flies up (win signal).
- `.dir-arrow.dn { color: var(--muted); animation: arrowFallDown; opacity: 0.6; }` — down-arrow falls limp + dim (loss signal).
- `.has-sparkle .sk` — 4 staggered gold sparkles rising above winning numbers.
- `td.pos { font-weight: 800; }` / `td.neg { font-weight: 500; opacity: 0.85; }` — winners literally bolder, losers literally lighter (v26.3).
- `.ambient-particles .ap` — subtle gold dust drift across hero (v26.4).
- `.line-tip-beam` — gold halo at equity curve rising tip (v26.5).
- `.reel-slide.s3` — overrides reel-slide red gradient with cool gray (v26.6).

### v27 (animation pack on top of v26)
- `@keyframes equityTipBeam` + `.end-dot.v27-beam` — gold halo pulses + grows at equity curve tip (radius 6→9, drop-shadow 12px→28px).
- `.podium-card.gold.v27-tilt` — 3D parallax tilt on the top-1 podium card under cursor (`rotateX/Y` + `translateZ(28px)`).
- `.reel-auto-ring` — fixed bottom-right 42×42 gold ring counts down 4s per reel slide.
- `@keyframes kpiScalePop` + `@keyframes kpiGlowSweep` + `.card.v27-pop` — KPI numbers bounce in (`scale 0.92 → 1.06 → 1`), then a 110° gold light-sweep wipes across once.
- `.trades-table tbody tr:hover` — row shimmer with gold gradient (1.4s loop).

### v29 (punch pack — 5 fixes for 5-second graspability + honest envelope)

This pack was added 2026-05-24 in response to operator feedback:
"why these sections are still not understandable easily dude i mean see
as a user no one will look at it right max 5 seconds itself".

- **v29.1** `.preview-badge` — gold pulsing badge in mode bar reading `◈ PREVIEW DATA`. Hover tooltip explains: "Numbers shown are placeholder MOCKUP data — engine + 9-filter pipeline are real and standards-compliant, but live sweep wiring is in flight." **Closes Golden Rule #1 honest-envelope gap.**
- **v29.2** `.hero-collapse-line` — 30pt one-liner above the hero prose: "From `33,000,000,000` ideas → `1` winner that pays `+₹88,300/day`". `.big-num` is blue→purple gradient, `.winner-num` is gold gradient with `kpiScalePop` entrance, `.money-num` is green with neon-glow shadow.
- **v29.3** `.funnel-horizontal` — one-glance collapse "33B → 12M → 1.2M → 84K → 2.4K → 410 → 174 → 1" with font sizes shrinking (30pt → 17pt) and the final "1" rendered at 48pt gold with `glowPulse`. The existing vertical staircase (v25.1 `.funnel25`) stays BELOW for drill-down.
- **v29.4** `.f25-grid.v29-staggered` + `.v29-hidden` + `.filter-reveal-toggle` — only first 3 of the 9 institutional filter cards visible initially; "Show 6 more filters ▾" button reveals cards 4-9 with staggered fade-in (70ms per card). Cognitive load drops from 9 → 3 for first 5-second viewers.
- **v29.5** `.rule-card.v29-collapse` + `.v29-rule-toggle` — trigger/market-status/action paragraphs hidden by default in rule cards; only RANK pill + 6 KPI stats visible. The NET KPI (the punchline) is boosted to 32pt with `linear-gradient(135deg, var(--green), var(--green-bright))` (green-to-bright-teal). "Show mechanics ▾" button per card reveals the prose; chevron rotates 180° when expanded.

---

## 5. Motion-first meaning rules (v26 contract)

**Color is aesthetic. Motion + weight carry meaning.**

This rule was added at v26 to support:
1. Color-blind operators (red-green deficiency affects ~8% of men).
2. Screenshot legibility (motion implied by shape even when paused).
3. Three-channel information density (color + motion + weight) instead of one.

| Semantic | Motion carrier | Weight carrier | Color (aesthetic) |
|---|---|---|---|
| Win (positive P&L) | up-arrow flies up + gold sparkle | `font-weight: 800` | green or gold |
| Loss (negative P&L) | down-arrow falls limp, dim | `font-weight: 500; opacity: 0.85` | red or muted gray |
| Highlight (rank #1) | gold sparkle + ambient drift + light-beam tip | `font-size: 32pt` | gold |
| Live (in progress) | pulse animation | (none) | green pulse dot |
| Preview / mockup | pulsing border | (none) | gold (PREVIEW badge) |

**Forbidden in v26+:**
- Red-only semantic (must add downward motion or weight reduction).
- Green-only semantic (must add upward motion or weight increase).
- Pure-color screenshots that lose meaning when grayscaled.

---

## 6. Data contract (DYNAMIC scaling — Golden Rule #11)

Every number on screen MUST come from `DashboardState`. None hardcoded.

### 6.1 Current state schema (`brutex/dashboard/app.py`)

```python
@dataclass(slots=True)
class PhaseSnapshot:
    phase: str            # IngestPhase.value
    status: str           # "pending" | "running" | "done" | "halted"
    total: int = 0
    done: int = 0
    success: int = 0
    no_data: int = 0
    quarantined: int = 0
    retried: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    elapsed_seconds: float | None = None
    message: str | None = None
```

### 6.2 Extension required for v29 dynamic funnel (status: 📋 ROADMAP)

The v29 horizontal funnel + vertical staircase both need:

```python
@dataclass(slots=True)
class SweepFunnelSnapshot:
    """One snapshot of the 9-filter survival pipeline.

    Every value is dynamic — the 33B/12M/1.2M/etc numbers in v29.html
    are PLACEHOLDER until this schema is wired to the live sweep.
    """
    step_0_all_candidates: int           # currently mocked as 33_000_000_000
    step_1_cheap_prefilter: int          # 12_000_000
    step_2_real_edge_test: int           # 1_200_000
    step_3_unseen_data: int              # 84_000
    step_4_best_of_n: int                # 2_400
    step_5_not_overfit: int              # (collapsed into 4-6 group)
    step_6_multi_test: int               # (collapsed)
    step_7_three_windows: int            # 410
    step_8_sealed_lockbox: int           # 174
    step_9_random_shuffle: int           # (filter only)
    winners_top_n: int                   # 1 (the chosen one)
    sweep_id: str                        # for the audit cite
    snapshot_at: str                     # ISO timestamp
```

**The 33B is not a fixed claim** — it's the **maximum** combo-space size
at Phase 10 (730B for Track 2). At Day 1 (12.6M trial space) the funnel
collapses to `12.6M → 1.5M → 150K → 10K → 300 → 8 → 1`. The same
markup renders both — only the numbers change.

### 6.3 Honest envelope per Golden Rule #10

| Field | Live-wired today? | Until wired |
|---|---|---|
| Phase progress (`PhaseSnapshot.percent`) | ✅ wired via `on_pipeline_event` | — |
| Per-phase rates/skip counts | ✅ wired | — |
| Telegram bot stats | ✅ wired via `on_telegram_stats` | — |
| `SweepFunnelSnapshot` (v29 funnel) | ❌ schema-only; mockup numbers in v29.html | PREVIEW DATA badge |
| Top combos table P&L numbers | ❌ mockup | PREVIEW DATA badge |
| Equity curve points | ❌ mockup | PREVIEW DATA badge |

**Rule:** as soon as a field becomes live-wired, the v29 HTML must
either remove its hardcoded value OR mark the section with a smaller
"LIVE" badge so the operator knows that one section is real. The
PREVIEW DATA badge persists until ALL sections in the current view
are live-wired.

### 6.4 Incremental swap-out pattern (Golden Rule #11 — frontend CDSA-I)

**Operator decree 2026-05-24:** *"in the future when we do the fetch
and pull the real data and its concepts design functionalities
requirements then this frontend and its design should work precisely
right even then also i will always say it as common dynamic scalable
incremental approach to always modify and update right dude am i
right bro?"*

**Answer: YES — locked here as the frontend CDSA-I contract.**

The v29 dashboard is **COMMON · DYNAMIC · SCALABLE · INCREMENTAL** on
the frontend the same way Golden Rule #11 mandates it for the engine:

| CDSA-I pillar | How v29 satisfies it on the frontend |
|---|---|
| **COMMON** | One source of truth chain: this doc → `v29.html` → `app.py`. Every dashboard PR updates the spec + mockup together. No copy-paste between mockup HTML and live HTML (when live wiring lands, the mockup file IS the live template — same DOM IDs + class names). |
| **DYNAMIC** | Adding a new KPI / funnel step / filter card never re-architects. New `.f25-card` slot = new `<div>` + entry in the `SweepFunnelSnapshot` schema. New rule card = new `.rule-card.v29-collapse` block + entry in the rules array of the state snapshot. The CSS animation vocabulary (v22-v29) is open-ended — `v30+` blocks extend, never rewrite. |
| **SCALABLE** | Same markup renders Day-1 (12.6M trial space → 1 winner) and Phase-10 (730B → 1). The 33B in the v29 horizontal funnel is the Phase-10 MAXIMUM, not a hardcoded claim — at Day 1 the same `.funnel-horizontal` block renders `12.6M → 1.5M → 150K → 10K → 300 → 8 → 1`. CSS font-size shrink + glow on final winner adapt automatically because they're keyed to position-in-sequence, not value-magnitude. |
| **INCREMENTAL** | **Swap-out happens one field at a time, never big-bang.** See the 6-step incremental swap-out sequence below. The PREVIEW DATA badge tooltip narrows scope on each step until it can be removed entirely. The mockup file (`docs/dashboard/mockups/v29.html`) stays as the visual reference even after live wiring — the design lock survives the data swap. |

#### Incremental swap-out sequence (per field)

When the engine finishes wiring a field, follow this 6-step pattern.
**One field at a time, every step verifiable.**

| Step | Action | Verifiable by |
|---|---|---|
| 1 | Extend `DashboardState` schema in `brutex/dashboard/app.py` with the new field (e.g. add `SweepFunnelSnapshot.step_0_all_candidates: int`). | `mypy --strict brutex` passes; `tests/unit/test_dashboard_state.py` extended. |
| 2 | Wire the WebSocket fan-out + REST endpoint to publish the new field every N seconds. | `tests/integration/test_dashboard_e2e.py` extended. |
| 3 | Replace the field's hardcoded value in `docs/dashboard/mockups/v29.html` with `<span data-bind="step_0_all_candidates">33B</span>` (the `data-bind` attribute is the JS subscriber's hook; the inner text stays as the fallback if the WS connection drops). | `grep -c "data-bind" docs/dashboard/mockups/v29.html` increases by 1. |
| 4 | Add JS subscriber that on WS message updates every `[data-bind="X"]` element's `textContent` to `snapshot.X`. The first such subscriber added covers ALL future fields (write once). | E2E test: load page, send WS message with funnel snapshot, assert DOM text updates. |
| 5 | Narrow the PREVIEW DATA badge tooltip: from "Numbers are placeholder MOCKUP" → "Funnel step 0 is LIVE; remaining fields placeholder." Keep narrowing as more fields wire. | Visual check + crosswalk against this spec. |
| 6 | When ALL fields are live-wired and the tooltip is empty of mockup caveats, remove the PREVIEW DATA badge from the markup entirely. The mockup file becomes the live template (DOM IDs + class names + bindings unchanged from the locked design). | `grep -c "preview-badge" docs/dashboard/mockups/v29.html` returns 0. PR title: "remove PREVIEW DATA badge — all dashboard fields live-wired (DEC-DASHBOARD-LIVE-N)". |

**What this DOESN'T require:**
- Rewriting the mockup HTML structure.
- Touching the v22-v29 CSS animation vocabulary.
- Changing the v26 motion-first meaning rules.
- Reskinning the v28-hybrid palette tokens.
- Re-architecting the three-mode rules (operator/share/reel).

**What this DOES require:**
- One new `data-bind` attribute per field in the mockup HTML.
- One Python schema field per data point in `app.py`.
- One JS line of subscriber code per future field (after the first subscriber is added; the loop is `qsa('[data-bind]').forEach(...)`).
- One MIGRATION.md entry per swap-out PR with the `DEC-DASHBOARD-LIVE-N` ID.

**Forbidden during incremental swap-out:**
- Replacing the mockup file with a brand-new HTML structure.
- Swapping all fields at once ("big-bang" — violates the audit-replay contract per Golden Rule #7).
- Removing the PREVIEW DATA badge while ANY field is still hardcoded.
- Changing the palette tokens or animation classes during a wiring PR (palette/animation changes go in separate `DEC-DASHBOARD-VNN` PRs).

#### Schema-extension example (hypothetical first swap-out / DEC-DASHBOARD-LIVE-1)

If the next wiring PR wires `SweepFunnelSnapshot.step_0_all_candidates` first:

```python
# brutex/dashboard/app.py — schema extension (Step 1)
@dataclass(slots=True)
class SweepFunnelSnapshot:
    """Funnel survival counts. INCREMENTAL: fields wire one at a time."""
    step_0_all_candidates: int    # Step 1 of this swap-out: schema gains the field
    # Remaining fields commented out until their own swap-out PR.
    # step_1_cheap_prefilter: int
    # step_2_real_edge_test: int
    # ...
    sweep_id: str
    snapshot_at: str
```

```html
<!-- docs/dashboard/mockups/v29.html — markup binding (Step 3) -->
<div class="funnel-horizontal">
  <span class="step-num" data-bind="step_0_all_candidates">33B</span>
  <span class="fn-arrow">→</span>
  <span class="step-num">12M</span>  <!-- still hardcoded; future PR -->
  ...
</div>
```

```js
// First subscriber addition (Step 4) — write once, covers all future fields
ws.onmessage = (evt) => {
  const snap = JSON.parse(evt.data);
  document.querySelectorAll('[data-bind]').forEach(el => {
    const key = el.getAttribute('data-bind');
    if (snap[key] !== undefined) {
      el.textContent = formatNumber(snap[key]);  // 33_000_000_000 → "33B"
    }
  });
};
```

After this PR merges:
- Step 0 of the funnel is LIVE.
- Remaining 7 funnel steps + all combo cards + equity curve still placeholder.
- PREVIEW DATA badge tooltip narrowed: "Step 0 LIVE; remaining placeholder."

The next wiring PR (`DEC-DASHBOARD-LIVE-2`) repeats the 6-step pattern
for `step_1_cheap_prefilter`. And so on. **Incremental, never big-bang.**

#### Why this matters

Without this pattern, swap-out would either:
- **Be one giant PR** that rewrites every dashboard binding in one commit (violates audit-replay; if any field is wrong, the whole dashboard is broken).
- **Drift the mockup from the live** so the canonical reference rots and `docs/dashboard/mockups/v29.html` becomes a museum piece while the real dashboard diverges.

The 6-step incremental pattern keeps the mockup AS the live template
(only difference: live has the WS subscriber JS attached). Future
sessions and reviewers see ONE file as the design truth, with a clear
audit trail of which fields wired when (one MIGRATION entry per
swap-out PR).

---

## 7. PREVIEW DATA badge (v29.1) — Golden Rule #1

Class: `.preview-badge` (CSS in v29.html lines ~706-727; HTML in mode bar
lines ~2009-2010).

**Renders as:** `◈ PREVIEW DATA` in gold, gold border, hover tooltip,
3.5s pulse animation.

**Tooltip text (canonical, do not edit casually):**

> Numbers shown are placeholder MOCKUP data — the brute-force engine +
> 9-filter statistical pipeline are real and standards-compliant, but
> the live sweep wiring to this dashboard is still in flight.
> Methodology is institutional-grade; the specific numbers are NOT
> from a real sweep yet.

**Lifecycle:**
1. v29 ships with the badge ALWAYS visible.
2. As `SweepFunnelSnapshot` becomes live-wired (a future PR), the
   tooltip narrows scope: "Equity curve + per-trade table still
   placeholder; funnel is LIVE."
3. When ALL sections are live, the badge is removed entirely
   (not styled green — just gone).

**Forbidden:** removing the badge before ALL sections are live, OR
removing the badge while ANY section uses hardcoded numbers. Catches
the Golden Rule #1 "no hallucination" violation that the
2026-05-24 review identified ("Same pipeline used by Renaissance, Two
Sigma, AQR" — marketing overclaim).

---

## 8. Three-mode rules (operator / share / reel)

Body class flips between `.operator-mode`, `.share-mode`, `.reel-mode`.

| Element | operator | share | reel |
|---|---|---|---|
| `.jargon` (technical names) | shown | hidden | hidden |
| `.plain-eng` (plain English) | hidden | shown | shown |
| `.show-operator-only` (sweep IDs + hashes) | shown | hidden | hidden |
| `.show-share-only` (@handle watermark) | hidden | shown | shown |
| `.reel-progress-dots` | hidden | hidden | shown |
| `.reel-auto-ring` (v27.3) | hidden | hidden | shown |
| Section auto-advance | (manual) | (manual) | 4 sec per slide |

**Reel mode auto-advances** every 4 seconds across the 9:16 vertical
slideshow (v27.3 ring counts down). Stop-on-hover is intentional so
the operator can pause to inspect.

---

## 9. Honest envelope (Golden Rule #10)

**What is REAL in v29:**
- Visual + animation + layout (this spec + v29.html).
- Palette + motion-first meaning carriers.
- Three-mode switching (operator/share/reel).
- FastAPI backend (`brutex/dashboard/app.py`) with `PhaseSnapshot` + WebSocket fan-out.
- Test coverage on `DashboardState.on_pipeline_event` (in `tests/unit/test_dashboard_state.py`).

**What is PLACEHOLDER MOCKUP in v29:**
- Every number on screen except phase progress.
- The 33B funnel head + 1-winner tail.
- All combo IDs, P&L figures, podium rankings.
- Equity curve points.

**What is ROADMAP (closes the mockup gap):**
1. `SweepFunnelSnapshot` data class added to `brutex/dashboard/app.py`.
2. WebSocket fan-out extended to publish funnel snapshot every 5 sec.
3. v29.html JS extended to subscribe + replace hardcoded numbers.
4. PREVIEW DATA badge tooltip narrows scope per section as each wires.
5. Mockup HTML in `docs/dashboard/mockups/v29.html` stays as the
   visual reference even after live wiring (the design lock).

---

## 10. Auto-driver test (Golden Rule #11 plain-English)

> "Auto-bhaiya, kya samjha?" — explain the dashboard in one paragraph
> the operator's auto-driver can repeat.

**Plain-English version:**

> BruteX tests 33 billion trading strategies, then runs 9 statistical
> filters (the same ones top hedge funds use — López de Prado, Bailey,
> White, Hansen papers) to find ones that actually work. The dashboard
> shows the collapse: 33 billion ideas funnel down to 1 winner. The
> winner card shows the rule (when to trade, what to trade, when to
> exit) and the money it made. There's a gold "PREVIEW DATA" pill at
> the top — that means the engine is real but the numbers shown
> right now are placeholders until the live sweep finishes wiring.

This passes the auto-driver test: explains the funnel, explains the
filters, explains the winner, and is honest about what's mock vs live.

---

## 11. Amendment log

| Date | Version | Change | Reason |
|---|---|---|---|
| 2026-05-24 | v29 | LOCKED initial design | DEC-DASHBOARD-V29-001 — operator: "lock this plan and its design and its languages and its tools entirely in the code docs design architecture right dude?" |
| 2026-05-24 | v28 | v24 palette + v25/v26/v27 motion stack | DEC-DASHBOARD-V28-001 — operator: "all our new enhancements and animations and extremely attractive visualisation just in our v24 itself" |
| 2026-05-24 | v27 | Animation pack on top of v26 | equity light-beam, 3D podium tilt, reel ring, KPI scale-pop, row shimmer |
| 2026-05-24 | v26 | Motion-first meaning carriers + culturally neutral palette | gold-on-navy ("Bloomberg Terminal neutrality") + motion = meaning |
| (prior) | v22-v25 | Funnel + filter cards + viral attraction pack | iterative pre-lock |

---

## 12. Cross-references

- `CLAUDE.md` §1 (project identity) + §10 (session etiquette) — reading this doc is part of session start.
- `docs/MIGRATION.md` #222 (DEC-DASHBOARD-V29-001) — the locking decision.
- `docs/STANDARDS_100PCT.md` — section 2 observability dashboard row.
- `brutex/dashboard/app.py` — implementation that satisfies §6 data contract.
- `tests/unit/test_dashboard_state.py` — unit tests for `DashboardState`.

---

## 13. What this doc does NOT cover

- Dashboard backend deployment (`runbooks/dashboard-deploy.md` — TBD).
- WebSocket protocol details (subscriber side — TBD when live-wiring lands).
- Telegram + email + macOS notification routing (separate spec:
  `track2/TRACK2_OBSERVABILITY.md`).
- The actual brute-force engine + filter pipeline (that's
  `TRACK1_BACKTEST_SPEC.md`).
