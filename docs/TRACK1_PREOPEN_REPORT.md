# BruteX Pre-Open Report — at a glance

> **Plain-English guide + NSE ↔ BruteX comparison.** No jargon. If you can read
> a stock-market app, you can read this. Locked by **DEC-PREOPEN-REPORT-001**,
> **DEC-PREOPEN-AMPERSAND-001**, **DEC-PREOPEN-XLSX-STYLE-001**.

---

## 1. What is it, in one line?

A **one-click Excel** of the NSE "pre-open" snapshot — for any date range and any
stocks you've pulled — built straight from your own data lake, with numbers that
**match NSE to the paisa**.

Open the dashboard → **Reports** → pick **From / To** → tick the symbols →
**Download .xlsx**. Your friend opens it in Excel / Numbers / Google Sheets and
sorts it however they like.

---

## 2. The five numbers (what each column means)

| Column        | Plain English                                              | Where it comes from                                            |
|---------------|------------------------------------------------------------|----------------------------------------------------------------|
| **DATE**      | The trading day this row is about.                         | The lake's bar date (IST).                                     |
| **SYMBOL**    | The stock's NSE ticker (e.g. `RELIANCE`, `M&M`).           | The lake partition name.                                       |
| **PREV. CLOSE** | Yesterday's *official* closing price.                    | The prior trading day's **1-day** close.                       |
| **IEP**       | This morning's **9:15 opening price** (the pre-open price).| The day's **first 1-minute** bar's open.                       |
| **CHNG**      | How much it moved at open. `IEP − PREV. CLOSE`.            | Computed.                                                      |
| **%CHNG**     | The same move as a percentage. `CHNG ÷ PREV. CLOSE × 100`. | Computed.                                                      |

**Proof it matches NSE** (operator's CSV, COALINDIA, 27-May-2026):
`PREV. CLOSE 458.15 · IEP 429.00 · CHNG −29.15 · %CHNG −6.36` — reproduced to the
paisa. (Money shows 2 decimals; **%CHNG keeps full precision**, never rounded
into the ranking — DEC-MONEY-2DP-001.)

---

## 3. NSE pre-open file ↔ BruteX report — side by side

| Thing                | NSE's own file (`MW-Pre-Open-Market-….csv`) | BruteX `.xlsx`                                  | Same? |
|----------------------|---------------------------------------------|-------------------------------------------------|:-----:|
| SYMBOL               | ✅                                          | ✅                                              | ✅ |
| PREV. CLOSE          | ✅                                          | ✅ (paisa-exact)                                | ✅ |
| IEP                  | ✅                                          | ✅ (paisa-exact)                                | ✅ |
| CHNG / %CHNG         | ✅                                          | ✅ (paisa-exact)                                | ✅ |
| **Bold** header      | ✅                                          | ✅ (DEC-PREOPEN-XLSX-STYLE-001)                 | ✅ |
| **Bold** SYMBOL name | ✅                                          | ✅                                              | ✅ |
| Titled sheet w/ date | ✅ one day in the title                     | ✅ **date range** in the title banner           | ✅* |
| Covers many days     | ❌ one day per file                         | ✅ **any range** (extra **DATE** column)        | ➕ |
| FINAL / VALUE / FFM CAP / 52-week H-L | ✅ extra columns          | ❌ not in scope (the 5 pre-open numbers only)   | — |
| Source               | scraped from NSE                            | **your lake** (offline, reproducible)           | ➕ |

`*` See the date question below — same *look*, but BruteX adds a per-row date so a
multi-day file is never ambiguous.

---

## 4. "But how do you provide the date?" — the honest answer

NSE's file is a **single day**, so it puts the date once, in the title. BruteX
exports a **range of days**, so it shows the date **two** ways that work together:

| Where      | What it shows                              | Why it's there                                    |
|------------|--------------------------------------------|---------------------------------------------------|
| **Title row** (row 1) | `Pre-Open Market: 2026-01-01 to 2026-06-03` | Mirrors NSE's titled look; states the whole range at a glance. |
| **DATE column** (per row) | `2026-05-27`, `2026-05-28`, …      | Every row is self-describing — sort/filter by day, no guessing. |

So you get the NSE *appearance* (bold, titled) **and** an unambiguous date on
every single row. Pick one day = pick the same From/To and the title says so.

---

## 5. The bug we fixed — `M&M` (and friends) used to crash the download

| | Before (the 500) | After the fix |
|---|---|---|
| Download a range that includes **M&M** | ❌ **Internal Server Error** — the whole file failed | ✅ Clean `.xlsx`, M&M included |
| Why | The reader's symbol check rejected the `&` that the writer had happily saved | The reader now accepts `&` (still blocks anything unsafe) |
| Who was affected | `M&M`, `M&MFIN`, `GVT&D` — real NSE F&O stocks with `&` in the name | All of them work now |
| Bonus | — | The candle **viewer** also stopped choking on these symbols |

**Why it was safe to fix:** `&` can't be used to break out of a folder or trick
the database query — so allowing it carries zero risk, it just matches the names
NSE actually uses. (DEC-PREOPEN-AMPERSAND-001.)

---

## 6. Why you can trust the numbers (no illusion)

| Promise                  | How it's guaranteed                                                          |
|--------------------------|------------------------------------------------------------------------------|
| **Matches NSE**          | PREV. CLOSE / IEP / CHNG / %CHNG reproduced to the paisa vs the operator CSV. |
| **Same file every time** | Deterministic bytes (fixed zip metadata) — re-download = byte-identical.      |
| **Opens everywhere**     | Verified with a strict OOXML reader (Excel-grade): bold, merge, widths, freeze. |
| **No surprise install**  | Pure-stdlib writer — no `openpyxl`/`xlsxwriter` runtime dependency to vet.     |
| **Fast**                 | O(1) work per row; one columnar lake pass per symbol (same path as `/db`).     |

---

## 7. Heads-up (honest limits — CLAUDE.md §10)

* The report only lists symbols **already pulled into your lake** with **both**
  1-minute **and** 1-day data. Pull more on the **Coverage / Database** page to
  widen it.
* Numbers are **lake-derived** (they *match* NSE for these five columns) — they
  are **not** scraped live from NSE.
* The four extra NSE columns (FINAL, VALUE, FFM CAP, 52-week High/Low) are **out
  of scope** — this report is the five pre-open numbers only.

---

## 8. How to use it (30 seconds)

1. Open the dashboard, click **Reports**.
2. Choose **From** and **To** dates.
3. Tick the symbols (or leave all ticked to export everything).
4. Click **Download .xlsx** — done.

The file's first row tells you the range; every row tells you its own date; the
header and stock names are **bold**; the columns are sized so nothing is cramped.
