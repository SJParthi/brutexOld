# pyright: reportPrivateUsage=none
"""A FAITHFUL wheel-free ``brutex_ops`` stand-in (DEC-NIGHTLY-RUST-CORE-001).

Python re-implementations of the `rust/brutex-ops` nightly kernels over the
SAME flat FFI argument shapes, installed into ``sys.modules['brutex_ops']``
by the seam tests (the ``test_lake_rust_stats.py`` fake-crate idiom) so the
flag-ON routing, burn-in and parity laws are exercised WITHOUT the compiled
wheel (the main CI test job never installs it — the facade's own contract).

Faithfulness matters: the burn-in bit-compares every fake return against
the live Python oracle, so a drift HERE fails the seam suites loudly —
which makes this module a wheel-free cross-check of the kernel logic shape,
not just a stub.
"""

from __future__ import annotations

import re
import types
from typing import Final

_BREAKERS: Final[frozenset[str]] = frozenset({"refused", "failed"})

_AT_RE: Final[re.Pattern[str]] = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$", re.ASCII)

#: The crate's input-envelope refusal marker, mirrored VERBATIM from
#: ``rust/brutex-ops/src/nightly.rs::INPUT_REFUSED_MARKER`` (and from the
#: FFI face's ``format!("{INPUT_REFUSED_MARKER}: {detail}")`` shape), so the
#: wheel-free arm exercises the SAME CLASS-C classification path the real
#: crate does (DEC-RUST-STRICT-NO-FALLBACK-001,
#: ``brutex/nightly/nightly_rust.py::_INPUT_REFUSED_MARKER``). Drift here
#: would silently turn a CLASS-C degrade into a CLASS-A refusal in the
#: wheel-free suites, so the literal is pinned on both sides.
_INPUT_REFUSED_MARKER: Final[str] = "input outside the crate envelope"


def nightly_first_breaker_index(statuses: list[str]) -> int | None:
    """Index of the first breaker status, else None."""
    for index, status in enumerate(statuses):
        if status in _BREAKERS:
            return index
    return None


def nightly_exit_code(
    statuses: list[str], exit_hints: list[int | None], has_tripwires: bool
) -> int:
    """The exit-code law over flat outcome rows."""
    if len(statuses) != len(exit_hints):
        raise ValueError("statuses/exit_hints length mismatch")
    index = nightly_first_breaker_index(statuses)
    if index is None:
        base = 0
    elif statuses[index] == "refused":
        base = 1
    else:
        hint = exit_hints[index]
        base = 2 if hint is None else hint
    return max(base, 2) if has_tripwires else base


def nightly_audit_reconcile_detail(
    nightly_id: str,
    events: list[tuple[str, str | None]],
    outcome_links: list[str],
    outcome_attempted: list[bool],
    link_starts_repr: str,
    attempted_sorted_repr: str,
    terminal_repr: str,
    declared_repr: str,
) -> str | None:
    """The T2 reconcile decision over the flat projection."""
    if len(outcome_links) != len(outcome_attempted):
        raise ValueError("outcome_links/outcome_attempted length mismatch")
    starts = sum(1 for action, _ in events if action == "NIGHTLY_START")
    problems: list[str] = []
    if starts != 1:
        problems.append(f"NIGHTLY_START x{starts} (want 1)")
    attempted = {
        link for link, was_attempted in zip(outcome_links, outcome_attempted) if was_attempted
    }
    named_starts = [link for action, link in events if action == "LINK_START" and link is not None]
    if len(named_starts) != len(set(named_starts)) or not set(named_starts) <= attempted:
        problems.append(
            f"LINK_START {link_starts_repr} not a subset of attempted {attempted_sorted_repr}"
        )
    terminal = [
        link
        for action, link in events
        if action in ("LINK_DONE", "LINK_BLOCKED") and link is not None
    ]
    if sorted(outcome_links) != sorted(terminal):
        problems.append(f"LINK_DONE/BLOCKED {terminal_repr} != declared {declared_repr}")
    if not problems:
        return None
    return f"nightly {nightly_id} audit does not reconcile: " + "; ".join(problems)


def nightly_count_text(value: int | None) -> str:
    """'?' for None — never a fabricated 0."""
    return "?" if value is None else str(value)


def nightly_verdict(statuses: list[str], suspect: bool) -> str:
    """The OK/SUSPECT/REFUSED/FAILED label."""
    index = nightly_first_breaker_index(statuses)
    if index is not None:
        return "REFUSED" if statuses[index] == "refused" else "FAILED"
    return "SUSPECT" if suspect else "OK"


def nightly_summary_lines(
    nightly_id: str,
    started_utc: float,
    ended_utc: float,
    links: list[tuple[str, str, str | None]],
    counts: list[tuple[str, int | None]],
    tripwires: list[tuple[str, str]],
    honesty_notes: list[str],
    dry_run: bool,
    suspect: bool,
) -> tuple[list[str], str]:
    """The Telegram body builder over the flat report projection."""
    statuses = [status for _, status, _ in links]
    verdict = nightly_verdict(statuses, suspect)
    duration = max(0.0, ended_utc - started_utc)
    hours, rem = divmod(int(duration), 3600)
    minutes = rem // 60
    breaker = nightly_first_breaker_index(statuses)
    headline = {
        "OK": "✅ OK",
        "SUSPECT": "⚠️ SUSPECT",
        "REFUSED": (f"⛔ REFUSED at {links[breaker][0]}" if breaker is not None else "⛔ REFUSED"),
        "FAILED": (f"❌ FAILED at {links[breaker][0]}" if breaker is not None else "❌ FAILED"),
    }[verdict]
    lines = [f"🌙 Nightly {nightly_id} — {headline} in {hours}:{minutes:02d}."]
    count_map = dict(counts)
    parts: list[str] = []
    for link, status_token, blocked_by in links:
        status = status_token
        if status_token == "blocked" and blocked_by is not None:
            status = f"blocked({blocked_by})"
        if link == "backup" and status_token != "blocked":
            status += f" ({nightly_count_text(count_map.get('backup_files'))} files)"
        elif link == "pull" and status_token != "blocked":
            status += f" ({nightly_count_text(count_map.get('pull_bars'))} bars)"
        elif link == "prebuild" and status_token != "blocked":
            status += f" ({nightly_count_text(count_map.get('prebuild_built'))} built)"
        elif link == "sweep" and status_token != "blocked":
            status += (
                f" ({nightly_count_text(count_map.get('sweep_run'))} run · "
                f"{nightly_count_text(count_map.get('sweep_skipped_cache'))} skipped-cache · "
                f"{nightly_count_text(count_map.get('sweep_failed'))} failed)"
            )
        parts.append(f"{link} {status}")
    lines.append(" · ".join(parts))
    for name, detail in tripwires:
        lines.append(f"⚠️ tripwire {name}: {detail}")
    if any(link == "backup" and status == "skipped" for link, status, _ in links) and not dry_run:
        lines.append("⚠️ backup SKIPPED by operator flag — stores unprotected tonight.")
    for note in honesty_notes:
        lines.append(f"ℹ️ {note}")
    return lines, verdict


def nightly_xml_escape(text: str) -> str:
    """The chained-replace law ('&' first)."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def nightly_render_launchd_plist(
    label: str,
    program_arguments: list[str],
    hour: int,
    minute: int,
    env_vars: list[tuple[str, str]],
    out_log: str,
    err_log: str,
) -> str:
    """The plist render over pre-formatted log paths + raw env pairs."""
    argv_xml = "".join(
        f"    <string>{nightly_xml_escape(token)}</string>\n" for token in program_arguments
    )
    env_xml = ""
    if env_vars:
        rows = "".join(
            f"    <key>{nightly_xml_escape(key)}</key>"
            f"<string>{nightly_xml_escape(value)}</string>\n"
            for key, value in sorted(env_vars)
        )
        env_xml = f"  <key>EnvironmentVariables</key><dict>\n{rows}  </dict>\n"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>{nightly_xml_escape(label)}</string>\n"
        f"  <key>ProgramArguments</key><array>\n{argv_xml}  </array>\n"
        "  <key>StartCalendarInterval</key><dict>\n"
        f"    <key>Hour</key><integer>{hour}</integer>"
        f"<key>Minute</key><integer>{minute}</integer>\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key><false/>\n"
        f"{env_xml}"
        f"  <key>StandardOutPath</key><string>{nightly_xml_escape(out_log)}</string>\n"
        f"  <key>StandardErrorPath</key><string>{nightly_xml_escape(err_log)}</string>\n"
        "  <key>ProcessType</key><string>Background</string>\n"
        "  <key>ThrottleInterval</key><integer>30</integer>\n"
        "</dict></plist>\n"
    )


def nightly_parse_at(text: str) -> tuple[int, int] | None:
    """The parse decision — None on a clean no-match; ValueError on non-ASCII."""
    if not text.isascii():
        raise ValueError(f"{_INPUT_REFUSED_MARKER}: non-ASCII --at input — python oracle decides")
    match = _AT_RE.match(text.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def nightly_pmset_wake_command(hour: int, minute: int, lead_minutes: int) -> str:
    """The midnight-wrapping wake command."""
    total = (hour * 60 + minute - lead_minutes) % (24 * 60)
    return f"sudo pmset repeat wakeorpoweron MTWRFSU {total // 60:02d}:{total % 60:02d}:00"


def nightly_seconds_since_fire(
    hour: int,
    minute: int,
    now_hour: int,
    now_minute: int,
    now_second: int,
    now_microsecond: int,
) -> float:
    """Seconds from the most recent fire instant, over intraday components."""
    for value, low, high in (
        (hour, 0, 23),
        (minute, 0, 59),
        (now_hour, 0, 23),
        (now_minute, 0, 59),
        (now_second, 0, 59),
        (now_microsecond, 0, 999_999),
    ):
        if not low <= value <= high:
            raise ValueError(f"{_INPUT_REFUSED_MARKER}: component {value} outside {low}..={high}")
    now_us = (now_hour * 3600 + now_minute * 60 + now_second) * 1_000_000 + now_microsecond
    fire_us = (hour * 3600 + minute * 60) * 1_000_000
    delta = now_us - fire_us
    if delta < 0:
        delta += 86_400 * 1_000_000
    return delta / 10**6


#: Every kernel the facade's API guard requires, name → implementation.
FAITHFUL_KERNELS: Final[dict[str, object]] = {
    "nightly_first_breaker_index": nightly_first_breaker_index,
    "nightly_exit_code": nightly_exit_code,
    "nightly_audit_reconcile_detail": nightly_audit_reconcile_detail,
    "nightly_count_text": nightly_count_text,
    "nightly_verdict": nightly_verdict,
    "nightly_summary_lines": nightly_summary_lines,
    "nightly_xml_escape": nightly_xml_escape,
    "nightly_render_launchd_plist": nightly_render_launchd_plist,
    "nightly_parse_at": nightly_parse_at,
    "nightly_pmset_wake_command": nightly_pmset_wake_command,
    "nightly_seconds_since_fire": nightly_seconds_since_fire,
}


def build_fake_crate(**overrides: object) -> types.ModuleType:
    """A ``brutex_ops``-shaped module: faithful kernels + per-test overrides.

    Every :data:`FAITHFUL_KERNELS` name is installed (so the facade's
    API-surface guard passes), then ``overrides`` replace individual
    kernels (a bomb, a wrong-value poison, a raising fault — the
    ``test_lake_rust_stats.py`` fake-crate idiom).
    """
    module = types.ModuleType("brutex_ops")
    module.__dict__["__version__"] = "0.1.0-fake"
    for name, function in FAITHFUL_KERNELS.items():
        setattr(module, name, function)
    for name, function in overrides.items():
        setattr(module, name, function)
    return module
