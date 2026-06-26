# EAP Feedback — Ready-to-send Email

> Send (or reply) to **claude-code-early-access@anthropic.com**
> (contact: Diana Liu, Product Operations Manager, Claude Code).
> Attach the HTML page: `CLAUDE_CODE_EAP_FEEDBACK.html`
> (rename on send to e.g. `BruteX-ClaudeCode-EAP-Feedback.html`).
> Private beta is live 2026-06-26, runs ~1 week (~through 2026-07-03).
> Materials are Confidential per the Trusted Tester Agreement.

---

## Subject

```
Claude Code Projects EAP — Round 1 feedback (BruteX), organized by your 5 topics
```

---

## Body (verbatim — paste as-is)

```
Hi Diana, and the Claude Code team,

Thank you for the access to the Claude Code Projects private beta.

I spent the week stress-testing Projects on a real, demanding repo: BruteX,
a backtesting engine I build almost entirely through Claude Code under a
100%-coverage / mypy-strict gate, a long decision-ledger, and heavy
multi-agent orchestration — a deliberately unforgiving environment.

My round-1 feedback is organized by your five topics — one short takeaway
each below, with the full at-a-glance comparison and 20 itemized findings
in the attached page.

1) Use-case fit — Strong. Long-lived, ledger-driven, multi-agent work is
   the canonical Project case; the one friction is that key state lives in
   prose, not queryable fields.

2) Reliability — Mixed (strong, with sharp edges). Sessions DO finish
   autonomously (a PR was opened, gated, and merged end-to-end this week),
   but a lone agent self-certified a fake 3.57x benchmark — only the
   adversarial reviewer caught the real ~1.17x and blocked it.

3) Memory — Strong. Rules, scope, and rate ceilings carried across
   sessions with zero restating; the only ask is making facts queryable.

4) Proactivity — Mixed. Parallel fan-out and adversarial review run on
   their own, but long background jobs give no progress heartbeat and big
   fan-outs have no up-front cost estimate.

5) Session-management UX — Mixed. Completed-work triage is good; the gaps
   are at the top of the funnel (per-session base-state, which long job is
   mid-flight).

Every item is real and reproducible — nothing padded. Happy to share repro
steps or go deeper on any item. I'll keep sending intermittent feedback as
new findings land across the window.

Thank you again — it's a pleasure to help push this.

Best,
Parthi
(sjparthi93@gmail.com)
```

---

## Send checklist

- [ ] Attach `CLAUDE_CODE_EAP_FEEDBACK.html` (renamed for sharing).
- [ ] Confirm the To: address is `claude-code-early-access@anthropic.com`.
- [ ] Keep the thread Confidential per the Trusted Tester Agreement.
