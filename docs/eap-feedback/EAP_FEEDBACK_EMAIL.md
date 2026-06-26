# EAP Feedback — Ready-to-send Email

> Send (or reply) to **claude-code-early-access@anthropic.com**
> (contact: Diana Liu, Product Operations Manager, Claude Code).
> Attach the HTML page: `CLAUDE_CODE_EAP_FEEDBACK.html`
> (rename on send to e.g. `BruteX-ClaudeCode-EAP-Feedback.html`).
> Private beta is live 2026-06-26, runs ~1 week (~through 2026-07-03).
> Materials are Confidential per the Trusted Tester Agreement.
>
> **How to copy the body cleanly into Gmail:** open this file on GitHub
> (the **rendered** view, not Raw), then select + copy the body section
> below. Copying from the rendered page preserves the blank line between
> each paragraph and bullet; a plain-text paste collapses them.

---

## Subject

Claude Code Projects EAP beta — Day-1 feedback (tested on BruteX as workload)

---

## Body

Hi Diana, and the Claude Code team,

Thank you for the access — it landed early this morning, and I've spent the first hours since putting the Projects beta hard through its paces.

This is feedback on the Claude Code Projects beta itself, tested on one real, demanding project (BruteX — a backtesting engine I build under a 100%-coverage / type-check gate with heavy multi-agent orchestration). Every point below is about the beta's behaviour — not the BruteX code; BruteX is just the workload.

The attached page leads with a feature-by-feature testing verdict (which beta features I exercised, and whether each worked), a 5-topic summary, and the 6 findings most worth acting on. The short version, by your five topics:

- Use-case fit — Strong. Long-lived, multi-agent work is the canonical Project case; the beta handled it. Friction: key state lives in prose, not queryable fields.

- Reliability — Mixed. It finished sessions autonomously (opened, gated, merged a PR end-to-end). But a lone agent self-certified a fake 3.57× benchmark — only its own adversarial reviewer caught the real ~1.17× and blocked it. Single-agent self-certification isn't safe; multi-agent review is.

- Memory — Strong. Rules, scope, and rate-limits carried across sessions with zero restating. Ask: make those facts queryable.

- Proactivity — Mixed. Parallel fan-out and adversarial review run on their own; but long jobs give no progress heartbeat, and big fan-outs no up-front cost estimate.

- Session UX — Mixed. Completed-work triage is good; the gaps are at the top of the funnel (no per-session base-state, no view of which long job is mid-flight).

Every item is real and reproducible — nothing padded, none of it a BruteX-code issue. I'll keep sending more as findings land across the week.

One quick check: is this format useful, or would you prefer it shorter / a form / a quick call? Happy to match whatever's easiest — this is my first round.

Thanks again — a pleasure to help push this.

Best,
Parthi
(sjparthi93@gmail.com)

---

## Send checklist

- [ ] Attach `CLAUDE_CODE_EAP_FEEDBACK.html` (renamed for sharing).
- [ ] Confirm the To: address is `claude-code-early-access@anthropic.com`.
- [ ] Keep the thread Confidential per the Trusted Tester Agreement.
