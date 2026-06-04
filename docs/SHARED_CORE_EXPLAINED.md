# One Rulebook, Two Players — the simple version 🧠

> The plain-English companion to `SHARED_CORE_DESIGN.md` (which is the
> engineer version). Same facts, zero jargon. If you read only one doc, read
> this one.

---

## The whole idea in one line

**We want ONE "market-reading rulebook" that both your apps share — so what you
*test* is exactly what you *trade*.**

---

## The analogy (30 seconds) 🍳

You run **two kitchens**:

- **BruteX** = your **test kitchen** — tries thousands of recipes on yesterday's
  ingredients to find the winners.
- **TickVault** = your **real restaurant** — cooks live for paying customers.

Both must follow the **exact same recipe**. If the test kitchen's recipe is even
*slightly* different from the restaurant's, then *"this dish was a hit in
testing"* becomes a **lie** — the restaurant is serving something else.

> The "recipe" = the **indicators** (EMA, pivots, Fib…) + the **52 yes/no market
> checks** (e.g. "is price above the average?"). That's the rulebook.

---

## What you have today 📸

| | **BruteX** (test kitchen) | **TickVault** (real restaurant) |
|---|---|---|
| Language it speaks | **Python** | **Rust** (different language!) |
| Has the 52-check rulebook? | ✅ Yes | ❌ No — uses different tools |
| Its job | Test strategies on history | Trade live, ultra-fast |
| Today's risk | — | Could read the market *differently* → test ≠ live |

---

## Why this matters (the money reason) ⚠️

| If the two drift apart… | …then |
|---|---|
| Backtest says *"this strategy makes money"* | Live trades on slightly different signals |
| You trust the backtest, put in real money | Reality differs from the test |
| **Result** | **You lose money on a result that was never real** |

> ✅ **One shared rulebook = your backtest finally tells the truth about live.**
> That is the entire point.

---

## The 3 ways to share the rulebook — THE comparison 🛠️

| | **① One shared book** ⭐ | **② Two books + inspector** | **③ One template, auto-printed** |
|---|---|---|---|
| It's like… | Both read the **same physical book** | Each keeps a copy; a **daily inspector** checks they're word-for-word identical | A **master template** that auto-prints both copies |
| Write a new rule how many times? | **Once** ✍️ | **Twice** ✍️✍️ | **Once** ✍️ |
| Can they secretly drift apart? | **Never** | Only until the next check (caught, not prevented) | **Never** |
| Live speed | ⚡ Fastest | ⚡ Fast | ⚡ Fast |
| Effort to build | 🔴 Big | 🟡 Medium | 🟠 Medium-big |
| New tool to babysit forever? | **No** | **No** | **Yes** (the "printer") |
| Truly "change once → both get it"? | ✅ **Yes** | ❌ No (you change it twice) | ✅ Yes |
| **Verdict** | ⭐ **Recommended** | decent backup | not recommended |

---

## What I recommend — and why, simply 🏆

**Option ① — one shared rulebook.**

- It's the **only** option where you truly write a rule **once** and **both** apps
  get it.
- It **can't drift** — same book = same rules, always.
- It **reuses tools both apps already have** (no new gadget to maintain).

**The honest catch:** it's the **most work up front**, and it **reverses two old
"keep them separate" decisions** — so we do it **openly** (with a logged decision
in both repos), never sneakily.

**The smart safety net 🪢:** before switching, we **photocopy today's exact
rulebook as an "answer key,"** and require the new shared book to match that
answer key **perfectly** before anything switches over. So it's a **checked
move, not a gamble.**

---

## What happens next — no surprises 👣

| Step | What it does | Touches | Risk |
|---|---|---|---|
| **0. Answer key** | Freeze today's 52 checks as a locked "answer key" | BruteX only | **None** (just a snapshot) |
| **1. Build shared book** | Write the rulebook in the shared language; prove it matches the answer key | new shared library | Low (gated by the key) |
| **2. BruteX switches** | BruteX reads the shared book instead of its own copy | BruteX | Low (all tests must stay green) |
| **3. TickVault switches** | The live engine reads the same shared book | TickVault | Needs your call on *how* |
| **4. Lock it in** | Both always use the latest; a guard **fails the build** if they ever drift | both | The guarantee becomes **automatic** |

> 🟢 **Step 0 is a no-regret freebie** — pure, BruteX-only, changes no behaviour,
> and it's useful no matter which option you end up choosing. That's the first
> thing I'd build.

---

## The only 2 questions I need from you ❓

1. **In TickVault**, should the 52-check rulebook **REPLACE** its current
   strategy setup, or run **ALONGSIDE** it?
2. **Where should the shared rulebook live** — its **own home** (recommended), or
   **inside** one of the two apps?

> Answer those two and I can start **Step 0** immediately (the safe freebie),
> then move through the steps one mergeable piece at a time.
