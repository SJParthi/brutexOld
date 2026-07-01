# Runbook — Sweep Auto-Restart (Supervisor + OS Service)

> **Target audience:** the operator running BruteX unattended, and Claude Code
> maintaining the automation trail.
> **Time required:** ~5 minutes (one-time install, then ZERO clicks per day).
> **Decision:** DEC-SUPERVISOR-AUTORESTART-001 (MIGRATION.md #1052).

This runbook closes the ONE real §11 **AUTOMATED** gap the sweep watchdogs left
open. The two existing watchdogs
(`brutex/observability/watchdog.py`, `brutex/orchestrator/watchdog.py`) are pure
**detectors** — they DETECT a hung worker / a retry storm and fire callbacks,
but nothing **relaunches** the process after a hard crash / reboot. Before this,
the operator had to re-run the sweep command by hand.

## What now closes the gap (two complementary layers)

| Layer | Survives | Ships as |
|-------|----------|----------|
| **`brutex supervisor`** (in-process actor) | a **CHILD crash** while the supervisor itself is alive | `brutex/orchestrator/supervisor.py` + the `supervisor` CLI subcommand |
| **launchd / systemd** (OS service) | the **supervisor / machine dying** — crash, **reboot**, logout | the templates in `docs/runbooks/supervisor-templates/` |

Neither alone is enough: the OS service relaunches `brutex supervisor` after a
reboot; the supervisor relaunches `brutex sweep-daemon` after an in-session
crash. Together they give hands-off survival across both failure classes.

Because the sweep's **checkpoint auto-resume** (DEC-SWEEP-REALTIME-GUARANTEE-001)
already makes a relaunch **bit-identical** (chaos-tested), every relaunch simply
resumes from the checkpoint — the supervisor only has to DO the relaunch, not
re-implement resume.

---

## The `brutex supervisor` command

```
brutex supervisor [supervisor flags] -- [sweep-daemon flags]
```

Everything after `--` is forwarded verbatim to `brutex sweep-daemon`. It spawns
the daemon as a **child**, monitors it, and:

* **child exits 0** (the sweep genuinely completed) → **STOP**, no relaunch;
* **child exits non-zero OR dies by signal** → **RELAUNCH** after exponential
  backoff;
* **operator SIGTERM/SIGINT** reaches the supervisor → shut the child down
  **gracefully** (SIGTERM → grace wait → SIGKILL), **no relaunch**;
* **`--max-restarts` consecutive crashes** → **give up loudly** (audit row +
  non-zero exit) so a crash-loop never spins forever;
* a child that **stayed up** longer than `--reset-after-uptime-seconds` before
  dying **resets** the backoff (a long-lived crash is not a crash loop).

Every restart / clean-exit / operator-stop / give-up appends one **hash-chained
audit row** (`<lake>/sweeps/audit/supervisor.jsonl`), so restarts are observable
(Golden Rule #8) and verifiable with `brutex audit verify`.

### Supervisor flags (all optional; defaults in parentheses)

| Flag | Meaning | Default |
|------|---------|---------|
| `--backoff-base-seconds` | delay before the first relaunch | `1.0` |
| `--backoff-factor` | multiplier per consecutive crash | `2.0` |
| `--backoff-cap-seconds` | ceiling the backoff can never exceed | `300.0` |
| `--reset-after-uptime-seconds` | uptime above which a death resets the backoff | `60.0` |
| `--max-restarts` | consecutive crashes before a loud give-up | `10` |
| `--shutdown-grace-seconds` | wait after SIGTERM before SIGKILL on stop | `10.0` |
| `--lake-root` | lake root (for the supervisor audit log path) | env / CWD lake |

### Try it in the foreground first

```
brutex supervisor -- --schedule daily@02:30
# ... Ctrl-C (SIGINT) → the child is shut down gracefully, no relaunch.
```

The backoff exponent, the cap, and the reset window are all in the `--help`;
tune them to your crash tolerance before wiring the OS service.

---

> **Always-on `--schedule` mode only.** Both OS templates below run
> `brutex supervisor -- --schedule …` and relaunch ONLY on a crash / reboot
> (launchd `KeepAlive={SuccessfulExit=false}` / systemd `Restart=on-failure`), NOT
> on a clean exit 0. Do **not** run a one-shot `brutex supervisor -- --once` under
> either template: `--once` completes and exits 0, and an unconditional keep-alive
> (`KeepAlive=true` / `Restart=always`) would relaunch the finished sweep forever —
> a spin loop. Run `--once` by hand or from cron/a systemd timer instead.

## macOS — launchd (`KeepAlive={SuccessfulExit=false}`)

The template is `docs/runbooks/supervisor-templates/com.brutex.supervisor.plist`.
`KeepAlive={SuccessfulExit=false}` makes launchd relaunch the supervisor on a
NON-ZERO exit (a crash) or reboot / logout, but NOT on a clean exit 0 (so a
genuinely completed sweep stops instead of spin-looping).

1. **Fill in the placeholders.** Copy the template and replace every
   `{{PLACEHOLDER}}` with an ABSOLUTE path / value:
   * `{{BRUTEX_VENV_BIN}}` — the venv `bin` dir (holds the `brutex` script),
     e.g. `/Users/you/bruteX/.venv/bin`
   * `{{BRUTEX_REPO_DIR}}` — the checkout root, e.g. `/Users/you/bruteX`
   * `{{BRUTEX_LAKE_ROOT}}` — your lake root, e.g. `/Volumes/BruteX/lake`

2. **Install + load:**
   ```
   cp docs/runbooks/supervisor-templates/com.brutex.supervisor.plist \
      ~/Library/LaunchAgents/com.brutex.supervisor.plist
   launchctl load -w ~/Library/LaunchAgents/com.brutex.supervisor.plist
   ```
   `RunAtLoad` + `KeepAlive={SuccessfulExit=false}` start it immediately and
   relaunch it across a crash / reboot (but not a clean exit 0).
   `ThrottleInterval=30` is launchd's own crash-loop throttle (on top of the
   supervisor's exponential backoff).

3. **Stop / uninstall** (SIGTERM → graceful child shutdown, no relaunch):
   ```
   launchctl stop  com.brutex.supervisor
   launchctl unload -w ~/Library/LaunchAgents/com.brutex.supervisor.plist
   ```

Logs land at `{{BRUTEX_LAKE_ROOT}}/logs/supervisor.{out,err}.log` — rotate/prune
them yourself; launchd does not.

---

## Linux — systemd (`Restart=on-failure`)

The template is `docs/runbooks/supervisor-templates/brutex-supervisor.service`.
`Restart=on-failure` makes systemd relaunch the supervisor on a NON-ZERO exit (a
crash) or a reboot, but NOT on a clean exit 0 (so a completed sweep stops instead
of spin-looping).

1. **Fill in the placeholders** (same paths as above, plus `{{BRUTEX_USER}}` —
   the account owning `~/.brutex` + the lake; omit `User=` for a `--user` unit).

2. **Install (system service, needs root):**
   ```
   sudo cp docs/runbooks/supervisor-templates/brutex-supervisor.service \
        /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now brutex-supervisor.service
   ```

   **Or a user service (no root):**
   ```
   cp docs/runbooks/supervisor-templates/brutex-supervisor.service \
      ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now brutex-supervisor.service
   loginctl enable-linger "$USER"   # keep it up across reboots without a login
   ```

3. **Status / logs / stop:**
   ```
   systemctl status brutex-supervisor.service     # add --user for a user unit
   journalctl -u brutex-supervisor.service -f      # add --user for a user unit
   systemctl stop brutex-supervisor.service        # SIGTERM → graceful, no relaunch
   ```

   `RestartSec=30` is systemd's own crash-loop throttle; `KillSignal=SIGTERM` +
   `TimeoutStopSec=30` give the supervisor a graceful window to shut its child
   down before systemd SIGKILLs the group.

---

## Verifying it works

* **Foreground:** run `brutex supervisor -- --schedule daily@02:30`, then in
  another terminal `kill <sweep-daemon-pid>` (the CHILD, not the supervisor).
  The supervisor logs `child exited unexpectedly … relaunch #1` and spawns a
  fresh daemon after the backoff. Ctrl-C the supervisor → it stops cleanly.
* **Under the OS service:** `kill` the child; the supervisor relaunches it.
  Then `kill` the SUPERVISOR itself; launchd/systemd relaunches the supervisor,
  which resumes the daemon from the checkpoint — bit-identically.
* **Audit trail:** `brutex audit verify <lake>/sweeps/audit/supervisor.jsonl`
  confirms the hash-chain of `SUPERVISOR_RESTART` / `SUPERVISOR_CLEAN_EXIT` /
  `SUPERVISOR_OPERATOR_STOP` / `SUPERVISOR_GIVE_UP` rows is intact.

## Honest envelope (Golden Rule #10)

* A **wedged, unkillable** child (an uninterruptible-sleep kernel hang) cannot be
  force-killed by any userspace supervisor; `shutdown_child` escalates SIGTERM →
  SIGKILL and reports honestly if the process still will not die.
* The OS templates are **config the operator installs once** — they are not
  code-tested in CI (they touch the machine's service manager). The in-process
  supervisor loop IS 100%-line+branch tested (fake child + injected clock) plus
  one real-subprocess integration test.
* This bounds WHEN a relaunch happens, not the sweep's `O(Σ work)` — a resumed
  sweep still costs the §8 wall-time envelope. No "millions in minutes" claim.
