# Security Policy

BruteX is a **proprietary, closed research repository** (see [`LICENSE`](./LICENSE)).
This policy explains how to report a security vulnerability responsibly.

## Supported versions

| Version | Supported |
|---------|-----------|
| `main`  | ✅ Yes    |
| any other branch / tag | ❌ No |

Only the `main` branch receives security fixes. Feature branches are
development snapshots and are not separately patched.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security reports.** A public
issue discloses the problem before a fix is available.

Instead, use **GitHub's private vulnerability reporting** for this repository:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability** (GitHub Security Advisories).
3. Fill in the private advisory form with a description, reproduction steps,
   affected component, and (if known) suggested remediation.

This routes the report privately to the maintainer through GitHub's
coordinated-disclosure workflow. Do not include live secrets, tokens, or
credentials in the report — describe the class of exposure instead.

## What to expect

- **Acknowledgement:** best-effort, typically within a few business days.
- **Assessment & fix:** handled privately via the GitHub advisory; a timeline
  is agreed case by case. This is a single-maintainer research project, so
  no fixed response-time SLA is guaranteed.
- **Disclosure:** coordinated — details are made public (if at all) only after
  a fix has landed on `main`, via the same GitHub advisory.

## Scope

- **In scope:** the source in this repository (the `brutex/` package, the
  Rust crates under `rust/`, the CI workflows under `.github/`, and the
  operator scripts under `scripts/`).
- **Out of scope:** third-party services and dependencies (report those to
  their own maintainers), and any deployment/infrastructure not contained in
  this repository.

Thank you for helping keep BruteX secure.
