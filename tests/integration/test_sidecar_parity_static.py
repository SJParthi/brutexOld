"""brutexd sidecar parity gate — Rust-uniformity Wave 1 (roadmap §3, G0).

Boots BOTH processes over the same tree — the REAL FastAPI app (``build_app``)
on a private loopback port, and the REAL ``brutexd`` binary (built in-test via
cargo) fronting it — then proves, over real sockets:

* **static corpus** — EVERY file under ``brutex/dashboard/static/`` served
  natively by brutexd is sha256-byte-identical to the Starlette mount, with
  equal ``content-type`` / ``content-length`` and equal-ABSENT
  ``cache-control`` (Starlette sets none; documented non-goal: brutexd does
  not replicate ``etag``/``last-modified`` in Wave 1 — full 200s, never
  stale);
* **health** — ``/health`` is the pinned literal (NEW route: verified that
  the FastAPI app serves NO health route today, so with the flag OFF nothing
  changes);
* **proxy transparency** (coordinator ruling 2026-07-11) — for a hostile
  route corpus (200 JSON · 404 · real FastAPI 422 · 307 redirect · chunked
  stream · POST-with-body · encoded query · trailing slash), the front
  response equals the direct response in status, body bytes, and the FULL
  header map minus:

  - the RFC 7230 §6.1 hop-by-hop set (``connection``, ``keep-alive``,
    ``proxy-authenticate``, ``proxy-authorization``, ``te``, ``trailer``,
    ``transfer-encoding``, ``upgrade``) — per-hop connection management /
    framing, re-derived by each hop BY DESIGN (forwarding them literally
    would double-declare framing); and
  - ``date`` — generated per-response by the upstream wall clock, so two
    sequential requests legitimately differ by a second; PRESENCE is
    compared, the value is not (documented volatility carve-out).

  ``x-request-id`` stays IN the comparison: the middleware echoes an inbound
  id, and both arms send the same fixed one.
* **leanness pin** — ``rust/brutexd/Cargo.toml`` declares no lake
  dependencies (duckdb/polars/parquet/arrow): brutexd sits IN FRONT of the
  substrate, it never opens the lake in this wave.

All requests go through ``http.client`` with verbatim request targets — no
client-side URL normalization, no redirect following, no transparent
decompression — so encoded characters and trailing slashes reach the wire
exactly as written.

Skip law (honest): building brutexd needs cargo. Without a Rust toolchain
this module SKIPS loudly; the crate's own integration tests
(``rust/brutexd/tests/integration.rs``) cover the same laws Rust-side, and
CI runs BOTH (the ``rust-brutexd`` job + this suite).
"""

from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import subprocess  # nosec B404 — fixed cargo/brutexd argv, no shell, no user input
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CRATE_DIR = REPO_ROOT / "rust" / "brutexd"
STATIC_DIR = REPO_ROOT / "brutex" / "dashboard" / "static"

#: RFC 7230 §6.1 hop-by-hop headers (see module docstring for the rationale).
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: Per-response wall-clock header — presence compared, value not.
VOLATILE = frozenset({"date"})

#: The pinned /health literal (must match rust/brutexd/src/health.rs).
HEALTH_BODY = b"brutexd ok\n"

pytestmark = pytest.mark.skipif(
    os.environ.get("BRUTEX_BRUTEXD_BIN") is None and shutil.which("cargo") is None,
    reason=(
        "brutexd parity suite needs cargo (or a prebuilt BRUTEX_BRUTEXD_BIN); "
        "the Rust-side laws are still covered by rust/brutexd/tests/integration.rs"
    ),
)


@dataclass(frozen=True)
class Wire:
    """One captured HTTP exchange."""

    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header_map(self) -> dict[str, list[str]]:
        """Case-folded multi-map of the comparable (non-hop, non-volatile) headers."""
        folded: dict[str, list[str]] = {}
        for name, value in self.headers:
            lower = name.lower()
            if lower in HOP_BY_HOP or lower in VOLATILE:
                continue
            folded.setdefault(lower, []).append(value)
        return folded

    def header_names(self) -> set[str]:
        return {name.lower() for name, _ in self.headers}

    def first(self, name: str) -> str | None:
        wanted = name.lower()
        for header, value in self.headers:
            if header.lower() == wanted:
                return value
        return None


def _request(
    port: int,
    method: str,
    target: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Wire:
    """Raw-target HTTP/1.1 request (no normalization, no redirect following)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        sent = {k.lower() for k in (headers or {})}
        if "host" not in sent:
            conn.putheader("Host", f"127.0.0.1:{port}")
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body is not None:
            conn.send(body)
        resp = conn.getresponse()
        return Wire(status=resp.status, headers=list(resp.getheaders()), body=resp.read())
    finally:
        conn.close()


def _wait_ready(port: int, target: str, *, deadline_s: float = 60.0) -> None:
    """Poll until the port answers *target* (any status), else fail loudly."""
    deadline = time.monotonic() + deadline_s
    last: str = "<never connected>"
    while time.monotonic() < deadline:
        try:
            wire = _request(port, "GET", target)
        except OSError as exc:
            last = repr(exc)
            time.sleep(0.05)
        else:
            if wire.status:  # any HTTP answer = the socket is serving
                return
    pytest.fail(f"port {port} never answered {target}: {last}")


def _add_parity_routes(app: Any) -> None:
    """Register the hostile proxied-route fixture set on the REAL app.

    Test-only additive routes under ``/parity/*`` — production routes are
    untouched; the app instance lives only inside this suite's process.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

    async def echo(request: Request) -> JSONResponse:
        payload = await request.body()
        return JSONResponse(
            {
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "host": request.headers.get("host"),
                "hop_marker_seen": "x-per-hop" in request.headers,
                "body": payload.decode("latin-1"),
            }
        )

    # PEP 563 (`from __future__ import annotations`) stringifies the local
    # `Request` annotation, which FastAPI cannot resolve from this module's
    # globals — pin the REAL class object so the parameter is injected as
    # the request, not misread as a required query param.
    echo.__annotations__ = {"request": Request, "return": JSONResponse}

    async def stream_gen() -> AsyncIterator[bytes]:  # pragma: no cover — driven over the wire
        yield b"chunk-one|"
        yield b"chunk-two|"
        yield b"chunk-three"

    async def needs_int(x: int) -> dict[str, int]:
        return {"x": x}  # pragma: no cover — only the 422 arm is driven

    app.add_api_route("/parity/echo", echo, methods=["GET", "POST"])
    app.add_api_route("/parity/needsint", needs_int, methods=["GET"])
    app.add_api_route(
        "/parity/redirect",
        lambda: RedirectResponse(url="/parity/echo", status_code=307),
        methods=["GET"],
    )
    app.add_api_route(
        "/parity/stream",
        lambda: StreamingResponse(stream_gen(), media_type="text/plain"),
        methods=["GET"],
    )


@dataclass(frozen=True)
class Stack:
    """The booted two-process topology under test."""

    front_port: int
    direct_port: int


@pytest.fixture(scope="session")
def brutexd_binary() -> Path:
    """The brutexd binary — BRUTEX_BRUTEXD_BIN override, else cargo build."""
    override = os.environ.get("BRUTEX_BRUTEXD_BIN")
    if override:
        binary = Path(override)
        if not binary.is_file():
            pytest.fail(f"BRUTEX_BRUTEXD_BIN={override!r} does not point at a file")
        return binary
    build = subprocess.run(  # nosec B603 — fixed cargo argv
        ["cargo", "build", "--manifest-path", str(CRATE_DIR / "Cargo.toml")],
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if build.returncode != 0:
        pytest.fail(f"cargo build failed:\n{build.stdout}\n{build.stderr}")
    binary = CRATE_DIR / "target" / "debug" / "brutexd"
    assert binary.is_file(), f"cargo build produced no binary at {binary}"
    return binary


@pytest.fixture(scope="session")
def stack(brutexd_binary: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    """Boot the REAL FastAPI app + the REAL brutexd front over one tree."""
    fastapi = pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    del fastapi
    uvicorn = pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the dashboard app needs duckdb")

    lake_root = tmp_path_factory.mktemp("parity-lake")
    prior_lake = os.environ.get("BRUTEX_LAKE_ROOT")
    os.environ["BRUTEX_LAKE_ROOT"] = str(lake_root)
    try:
        from brutex.dashboard.app import DashboardState, build_app
        from brutex.dashboard.rust_front import allocate_upstream_port

        app = build_app(DashboardState())
        _add_parity_routes(app)

        direct_port = allocate_upstream_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=direct_port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        _wait_ready(direct_port, "/health")  # 404 from FastAPI = serving

        front_port = allocate_upstream_port()
        proc = subprocess.Popen(  # nosec B603 — fixed brutexd argv
            [
                str(brutexd_binary),
                "--listen",
                f"127.0.0.1:{front_port}",
                "--upstream-port",
                str(direct_port),
                "--static-dir",
                str(STATIC_DIR),
            ]
        )
        try:
            _wait_ready(front_port, "/health")
            yield Stack(front_port=front_port, direct_port=direct_port)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover — teardown belt
                proc.kill()
            server.should_exit = True
            thread.join(timeout=15)
    finally:
        if prior_lake is None:
            os.environ.pop("BRUTEX_LAKE_ROOT", None)
        else:  # pragma: no cover — restore path depends on the outer env
            os.environ["BRUTEX_LAKE_ROOT"] = prior_lake


def _both(
    stack: Stack,
    method: str,
    target: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[Wire, Wire]:
    """Fire the SAME request at the front and directly at FastAPI.

    Both arms carry the SAME fixed Host (the front's authority) so any
    Host-derived value the upstream emits (absolute redirect Locations) is
    identical, and the SAME fixed x-request-id so the middleware echo is
    identical — every remaining header difference is a REAL proxy artefact.
    """
    fixed = {
        "Host": f"127.0.0.1:{stack.front_port}",
        "X-Request-ID": "parity-fixed-rid",
    }
    merged = {**fixed, **(headers or {})}
    front = _request(stack.front_port, method, target, body=body, headers=merged)
    direct = _request(stack.direct_port, method, target, body=body, headers=merged)
    return front, direct


def _assert_transparent(front: Wire, direct: Wire, *, context: str) -> None:
    """The coordinator's full-transparency comparator (see module docstring)."""
    assert front.status == direct.status, f"{context}: status drift"
    assert hashlib.sha256(front.body).hexdigest() == hashlib.sha256(direct.body).hexdigest(), (
        f"{context}: body drift\nfront={front.body[:200]!r}\ndirect={direct.body[:200]!r}"
    )
    assert front.header_map() == direct.header_map(), f"{context}: header drift"
    # date: value is wall-clock volatile; PRESENCE must still agree.
    assert ("date" in front.header_names()) == ("date" in direct.header_names()), (
        f"{context}: date presence drift"
    )


# ── (a) static corpus ────────────────────────────────────────────────────────


def test_static_corpus_byte_identical(stack: Stack) -> None:
    """Every file under brutex/dashboard/static/ — sha256 + header subset."""
    corpus = sorted(p.name for p in STATIC_DIR.iterdir() if p.is_file())
    assert corpus, "static corpus must not be empty"
    for name in corpus:
        front, direct = _both(stack, "GET", f"/static/{name}")
        ctx = f"/static/{name}"
        assert front.status == direct.status == 200, ctx
        assert hashlib.sha256(front.body).hexdigest() == (
            hashlib.sha256(direct.body).hexdigest()
        ), f"{ctx}: body drift"
        assert front.first("content-type") == direct.first("content-type"), ctx
        assert front.first("content-length") == direct.first("content-length"), ctx
        # Starlette sets no cache-control; brutexd must not invent one.
        assert front.first("cache-control") is None, ctx
        assert direct.first("cache-control") is None, ctx


def test_static_traversal_refused_by_front(stack: Stack) -> None:
    """Traversal shapes 404 at the front and never leak repo bytes."""
    for target in ("/static/../cli.py", "/static/%2e%2e/cli.py", "/static/a%5Cb.js"):
        front = _request(stack.front_port, "GET", target)
        assert front.status in (400, 404), target
        assert b"def cmd_dashboard" not in front.body, target


# ── (b) health ───────────────────────────────────────────────────────────────


def test_health_is_the_pinned_literal_and_new(stack: Stack) -> None:
    """/health = the pinned brutexd literal; FastAPI has NO health route."""
    front = _request(stack.front_port, "GET", "/health")
    assert front.status == 200
    assert front.body == HEALTH_BODY
    assert front.first("content-type") == "text/plain; charset=utf-8"
    # Design fact pinned: the upstream serves no /health — the route is NEW
    # and exists only under the flag (flag OFF ⇒ nothing serves /health).
    direct = _request(stack.direct_port, "GET", "/health")
    assert direct.status == 404


# ── (c) proxied-route transparency (the coordinator's pin set) ───────────────


def test_real_api_routes_are_proxy_inert(stack: Stack) -> None:
    """Representative REAL routes: JSON payloads byte-inert through the hop."""
    for target in (
        "/api/v1/pull/rate",
        "/api/v1/oneclick/runs",
        "/api/v1/db/candles/datasets",
        "/",
    ):
        front, direct = _both(stack, "GET", target)
        _assert_transparent(front, direct, context=target)


def test_404_and_422_error_bodies_pass_through(stack: Stack) -> None:
    """4xx statuses + JSON error bodies are byte-inert (never re-shaped)."""
    front, direct = _both(stack, "GET", "/no/such/route")
    assert front.status == 404
    _assert_transparent(front, direct, context="404 route")

    front, direct = _both(stack, "GET", "/parity/needsint")  # missing required ?x=
    assert front.status == 422
    assert b"detail" in front.body  # the REAL FastAPI validation shape
    _assert_transparent(front, direct, context="422 validation error")


def test_redirect_passes_through_unfollowed(stack: Stack) -> None:
    """3xx + Location pass through; the front must NOT follow the redirect."""
    front, direct = _both(stack, "GET", "/parity/redirect")
    assert front.status == 307
    assert front.first("location") == "/parity/echo"
    _assert_transparent(front, direct, context="307 redirect")


def test_trailing_slash_passes_verbatim(stack: Stack) -> None:
    """`/parity/echo/` is NOT normalized by the front — FastAPI's own
    redirect_slashes answer (307 + Host-derived Location) comes back
    identically because both arms carry the same Host."""
    front, direct = _both(stack, "GET", "/parity/echo/")
    _assert_transparent(front, direct, context="trailing slash")
    assert front.status == 307  # FastAPI redirect_slashes — proves the front
    assert front.first("location") == direct.first("location")  # did not claim it


def test_encoded_query_and_post_body_roundtrip(stack: Stack) -> None:
    """Encoded query chars + a POST body reach the upstream verbatim."""
    target = "/parity/echo?q=a%20b&sym=NSE%2DNIFTY&flag=1"
    front, direct = _both(stack, "POST", target, body=b"payload \x00\xffbytes")
    _assert_transparent(front, direct, context="encoded query + POST body")
    assert front.status == 200
    # The echo embeds the RAW query string — encoded bytes survived the hop.
    assert b"a%20b" in front.body
    assert b"NSE%2DNIFTY" in front.body
    assert front.body == direct.body


def test_hop_by_hop_request_headers_are_stripped(stack: Stack) -> None:
    """A Connection-named per-hop request header never reaches the upstream."""
    front, _direct = _both(
        stack,
        "GET",
        "/parity/echo",
        headers={"Connection": "x-per-hop", "x-per-hop": "1"},
    )
    assert front.status == 200
    assert b'"hop_marker_seen":false' in front.body.replace(b" ", b"")


def test_streamed_chunked_response_is_inert(stack: Stack) -> None:
    """A chunked StreamingResponse: bytes equal, no fabricated content-length."""
    front, direct = _both(stack, "GET", "/parity/stream")
    assert front.status == direct.status == 200
    assert front.body == direct.body == b"chunk-one|chunk-two|chunk-three"
    # The upstream response is unsized (chunked). The front must not buffer
    # it into a sized one: no content-length on either arm.
    assert direct.first("content-length") is None
    assert front.first("content-length") is None


def test_content_length_consistency_on_sized_responses(stack: Stack) -> None:
    """Sized responses keep an exact, equal content-length through the hop."""
    front, direct = _both(stack, "GET", "/api/v1/pull/rate")
    assert front.first("content-length") == direct.first("content-length")
    assert front.first("content-length") == str(len(front.body))


# ── (d) leanness pin ─────────────────────────────────────────────────────────


def test_brutexd_cargo_toml_declares_no_lake_dependencies() -> None:
    """brutexd fronts the substrate — it never opens the lake (Wave 1 law).

    Scans the crate manifest's non-comment lines for lake-stack crate names;
    the crate must stay lean (coordinator ruling 2026-07-11).
    """
    manifest = (CRATE_DIR / "Cargo.toml").read_text(encoding="utf-8")
    code_lines = [line for line in manifest.splitlines() if not line.lstrip().startswith("#")]
    joined = "\n".join(code_lines).lower()
    for forbidden in ("duckdb", "polars", "parquet", "arrow", "lmdb"):
        assert forbidden not in joined, f"lake dependency {forbidden!r} in brutexd Cargo.toml"
