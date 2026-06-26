# BruteX — What's Done, What's Cooking, What's Next

> A plain-English, customer-facing scoreboard. Every **Finished** row is real, merged, and verified
> against the decision ledger (`docs/MIGRATION.md`). **Current** rows are being worked on right now
> (in CI / not yet merged). **Future** rows are planned and honestly **not yet built**.
>
> _Refreshed 2026-06-26. The "PR" column is the ledger entry (`docs/MIGRATION.md #NNN`) — the
> authoritative record of each change. Honesty rule (Golden Rule #10): nothing is claimed "done"
> unless it actually merged. As of this refresh the whole resilience + latency batch has merged, so
> the **Current** section is empty; the ~5× pull speedup remains a **Future** item (it rewrites
> stored bytes and is gated on a formal ADR for reproducibility)._

**BruteX in one line:** a research engine that records every single minute of India's index & F&O
market history (2020 → today), shows you what's actually moving right now, and brute-force tests
trading ideas at massive scale — built so **nothing is ever slow, nothing is ever lost, and you
always know what's happening.**

---

## ✅ Finished — shipped and live

| What changed (plain English) | Why you care | Status | PR |
|---|---|---|---|
| The data download no longer crashes if the disk fills up — it stops cleanly and tells you, leaving no half-written or corrupt files. | A full disk can never wreck the data you already pulled. | ✅ Merged | #885 |
| The download log's "Fetches" column was renamed and re-explained so a failed window can't be mis-read as "barely tried" when it actually retried hard behind the scenes. | The status screen tells the truth about what really happened. | ✅ Merged | #887 |
| Your phone gets an instant alert the moment a download stops for any reason (disk full, out of memory, quota). | You hear about a stopped download in real time, not hours later. | ✅ Merged | #888 |
| The tamper-proof activity log is now flushed all the way to disk before each step counts as done, and only ever writes clean, standard data. | Even a sudden power-cut can't lose or corrupt your audit trail. | ✅ Merged | #890 |
| Feedback on the Claude Code tool itself is now kept in the repo so it can be gathered and sent in. | We keep improving the way the product is built. | ✅ Merged | #892 |
| A full-scale download test harness was added that fakes the broker so we can prove the pull keeps up with its allowed speed — and the test fails automatically if it ever falls behind. | The download speed is now guarded by an automatic safety net, not just hope. | ✅ Merged | #895 |
| A faster way to write each downloaded file that keeps the data byte-for-byte identical — a safe first step toward much quicker pulls. | Downloads start getting faster without changing a single number in your data. | ✅ Merged | #891 |
| A live "pull health" monitor that can tell whether a slow download is **our** software holding it back or the broker simply rejecting requests. | When a pull is slow, you'll know exactly whose fault it is — so it gets fixed, not guessed. | ✅ Merged | #893 |
| Smart caching for the dashboard so the Coverage, Completeness and Mapping pages stop re-scanning everything on every refresh. | The dashboard stays snappy even while a big download is running. | ✅ Merged | #894 |
| Your EAP feedback evidence doc + product scoreboard, published to GitHub. | A clear, shareable record for the Claude Code team. | ✅ Merged | #896 |
| CI auto-retries flaky crates.io downloads so transient network blips stop failing builds. | Our build pipeline stops breaking over random network hiccups. | ✅ Merged | #897 |

---

## ⚙️ Current — being built right now (in CI, not merged yet)

| What changed (plain English) | Why you care | Status | PR |
|---|---|---|---|
| _Nothing in flight — the whole resilience + dashboard-latency batch above has merged._ | — | — | — |

---

## 🔮 Future — planned, honestly not built yet

| What changed (plain English) | Why you care | Status | PR |
|---|---|---|---|
| Hash the raw Groww response on every pull and store it alongside the data, so each pull PROVES the stored lake equals exactly what the broker sent — real-time tamper/drift detection. | Mathematical proof your data is never altered. | 🟡 In progress | — |
| The big one: a new file format that should make downloads roughly 5× faster — but it rewrites how data is stored, so it needs a formal sign-off first to guarantee old results still reproduce exactly. | Much faster downloads, with a promise that nothing already saved ever changes. | 🔮 Not started (needs ADR sign-off) | — |
| Plugging the new "pull health" monitor directly into the live download so it watches every real pull automatically. | The slowdown detector becomes always-on, not a manual check. | 🔮 Not started | — |
| Making dashboard pages swap content in place instead of fully reloading — the last big source of page lag. | The final "nothing is ever slow" fix for the whole dashboard. | 🔮 Not started | — |
| Closing the remaining gaps in the download failure-matrix test (e.g. proving every file was written to the exact expected depth). | Even stronger automatic proof that downloads are complete and correct. | 🔮 Not started | — |

---

**Legend:** ✅ Merged & live · ⚙️ In flight (in CI, not merged) · 🟡 In progress (PR opening) · 🔮 Planned, not yet built.

> Honesty footer (Golden Rule #10): the ~5× pull speedup is **not done** — it rewrites stored bytes
> and is gated on a formal ADR for reproducibility (Golden Rule #7). The pull-health monitor has
> **merged** but is **not yet wired into the live pull** (that wiring is a Future item). The in-place
> dashboard nav-swap (the dominant remaining page-lag fix) is **planned, not built**. The raw-data
> integrity layer is **in progress, not merged**. The brute-force engine trades 3 indices
> (NIFTY / BANKNIFTY / SENSEX); the ~750-name lake is reference data, never changed silently.
> If a row and the ledger ever disagree, the ledger (`docs/MIGRATION.md`) wins.
