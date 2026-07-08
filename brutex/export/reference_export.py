"""Post-pull Groww 1-minute reference export to S3 (DEC-REFERENCE-EXPORT-S3-001).

After every completed BruteX pull, publish the day's CLEANED, STORED 1-minute
OHLCV — one Parquet file per spot instrument — to an external S3 prefix so
TickVault can read it as a SECOND, independent reference source for its
~770-instrument cross-verification.

The exported bytes are the **cleaned, stored lake bars**, read back through the
same keep-FIRST dedup gate every ``/movers`` session reader uses
(:func:`~brutex.data.lake_day_glob.read_day_intraday_frame`) — i.e. POST
heal / dedup / session-filter, with prices already snapped to the 2-decimal
tick grid at the lake-write boundary (DEC-SPOT-PRICE-2DP-001). The RAW vendor
payload is NEVER re-fetched or re-exported (Golden Rule #1 — a reference source
must be the same bytes the engine trusts, not a second vendor round-trip).

S3 layout (documented for the TickVault reader)::

    s3://<bucket>/groww-reference/<YYYY-MM-DD>/<groww_symbol>.parquet   (one per instrument)
    s3://<bucket>/groww-reference/<YYYY-MM-DD>/manifest.json            (written LAST)

* ``<bucket>`` is resolved fail-loud from ``$BRUTEX_REFERENCE_EXPORT_BUCKET``
  (:func:`resolve_export_bucket`). There is NO default bucket name — BruteX
  never guesses an external bucket (Golden Rule #1).
* ``<groww_symbol>`` is the lake symbol directory name, which already encodes
  the exchange (``NSE-NIFTY``, ``BSE-SENSEX``) so it is globally unique AND
  S3-key-safe (:func:`~brutex.data.partition.build_partition_path` forbids the
  path metacharacters).
* Each Parquet holds exactly the day's session rows, projected to the stable
  :data:`REFERENCE_EXPORT_COLUMNS` (``timestamp`` + OHLCV + ``open_interest``).
* ``manifest.json`` — instrument count, per-file row count + sha256, and the
  export timestamp — is written LAST. A single ``PutObject`` is atomic, so a
  consumer that reads a present manifest whose ``files`` all exist is looking at
  a COMPLETE export; a partial export leaves the per-file objects without a
  matching manifest and is detectable as incomplete (never a false-OK).
  **Consumer contract — sha256 verification is REQUIRED, not existence-only**:
  on a same-day RE-export there is a torn window (see below) during which the
  PRIOR manifest is still present while per-file objects are being overwritten
  one by one; an existence-only check would false-OK a mixed old/new day,
  whereas verifying each Parquet's bytes against the manifest's per-file
  ``sha256`` detects the tear (old manifest hash vs new object bytes) and the
  reader must retry until the manifest and objects agree.

Idempotence: a same-day re-export overwrites the per-file objects and rewrites
the manifest last with fresh hashes — bit-for-bit determined by the stored lake.
HONEST torn-window limit (Golden Rule #10): during a RE-export the PRIOR day's
manifest persists while the per-file objects are overwritten one-by-one, so a
consumer reading mid-re-export can see old manifest ``sha256`` values against
new object bytes. This is why the consumer contract above REQUIRES per-file
sha256 verification (a mismatch = torn read → retry later), not a bare
manifest-plus-objects existence check. The manifest-LAST ordering bounds the
window (a manifest is only ever replaced by a manifest describing fully-written
objects); it does not eliminate it — S3 has no multi-object transaction.

Cost honesty (Golden Rule #10): the export is O(instruments) per day with a
per-file streaming write (one instrument's day frame in memory at a time — no
whole-day blowup); it is NOT O(1). The scope is the SPOT segments
(:data:`REFERENCE_EXPORT_SEGMENTS` = ``INDEX`` + ``CASH``) — the ~770 underlying
instruments TickVault cross-verifies; the F&O contract tree (tens of thousands
of partitions) is deliberately OUT OF SCOPE and documented as such.

Scope/fence: this module NEVER touches the brute-force engine, the sweep path,
or a dashboard template. It reads the lake and writes to one external prefix.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

import structlog

from brutex.data.lake_day_glob import read_day_intraday_frame
from brutex.data.partition import VALID_EXCHANGES

if TYPE_CHECKING:  # pragma: no cover — typing-only imports, never executed
    from collections.abc import Mapping, Sequence

    import polars as pl

_LOG = structlog.get_logger("brutex.export.reference_export")

#: Environment variable naming the destination S3 bucket. No default — a bucket
#: name is NEVER guessed (Golden Rule #1); an unset/blank value is a loud config
#: error (:class:`ReferenceExportConfigError`).
REFERENCE_EXPORT_BUCKET_ENV: Final[str] = "BRUTEX_REFERENCE_EXPORT_BUCKET"

#: Environment variable gating the POST-PULL export seam. Default OFF — the
#: export needs an external bucket + the ``boto3`` extra, so it is opt-in
#: (unlike the always-on Greeks enrich). Junk raises (fail-loud env convention).
REFERENCE_EXPORT_ENABLED_ENV: Final[str] = "BRUTEX_REFERENCE_EXPORT"

#: Environment variable naming the AWS region for the real S3 client build.
REFERENCE_EXPORT_REGION_ENV: Final[str] = "BRUTEX_REFERENCE_EXPORT_REGION"

#: Default AWS region (matches the Groww token broker's ``ap-south-1``).
DEFAULT_REFERENCE_EXPORT_REGION: Final[str] = "ap-south-1"

#: Top-level key prefix under the bucket. The TickVault reader keys on this.
REFERENCE_EXPORT_PREFIX: Final[str] = "groww-reference"

#: The manifest object name written LAST under each day prefix.
MANIFEST_NAME: Final[str] = "manifest.json"

#: The lake interval whose stored bars are exported (1-minute is the ONLY
#: vendor-fetched granularity; DEC-1MINUTE-ONLY-DERIVE-1DAY-001).
REFERENCE_EXPORT_INTERVAL: Final[str] = "1minute"

#: The spot segments in scope — the ~770 underlying instruments TickVault
#: cross-verifies. ``FNO`` (the contract tree) is deliberately excluded.
REFERENCE_EXPORT_SEGMENTS: Final[tuple[str, ...]] = ("INDEX", "CASH")

#: The stable per-file column projection (order fixed for a deterministic
#: consumer contract). Every stored bar carries these columns (the shared
#: ``ParquetBarSink`` schema); ``open_interest`` is ``null`` for spot rows.
REFERENCE_EXPORT_COLUMNS: Final[tuple[str, ...]] = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
)

#: A stable source tag stamped into the manifest so the consumer can assert
#: provenance.
REFERENCE_EXPORT_SOURCE_TAG: Final[str] = "brutex-groww-1minute-reference"

#: IST zone for the day filter (fixed +05:30, no DST).
_IST: Final[str] = "Asia/Kolkata"

_MANIFEST_CONTENT_TYPE: Final[str] = "application/json"
_PARQUET_CONTENT_TYPE: Final[str] = "application/octet-stream"

#: ``on``/``off`` env tokens for :func:`resolve_reference_export_enabled`.
_ENV_ON_TOKENS: Final[frozenset[str]] = frozenset({"1", "on", "true", "yes"})
_ENV_OFF_TOKENS: Final[frozenset[str]] = frozenset({"", "0", "off", "false", "no"})


class ReferenceExportError(Exception):
    """Base error for the reference-export surface."""


class ReferenceExportConfigError(ReferenceExportError):
    """The export is misconfigured — e.g. the destination bucket is unset.

    Raised fail-loud rather than guessing a bucket name (Golden Rule #1).
    """


@runtime_checkable
class S3Uploader(Protocol):
    """The narrow S3 write surface the export calls — one ``put_object``.

    Dependency-free (imports NOTHING) so the module is importable with ``boto3``
    absent and a deterministic in-memory fake
    (``tests/fakes/fake_s3_uploader.py``) drives every branch to 100%
    line+branch with NO boto3 install and NO network — the proven AWS-fake
    pattern of :class:`~brutex.adapters.groww.aws_client.AwsClient`. BruteX is
    WRITE-ONLY to the one export prefix (a bounded IAM policy on the bucket
    side); there is no read/list/delete method here.
    """

    def put_object(self, *, key: str, body: bytes, content_type: str) -> None:
        """Write *body* to ``s3://<bound-bucket>/<key>``, overwriting.

        Raises on any transport/permission error (the seam fences it).
        """
        raise NotImplementedError  # structural Protocol stub — never executed


@dataclass(frozen=True, slots=True)
class ReferenceFileEntry:
    """One exported instrument file's manifest record."""

    symbol: str
    exchange: str
    segment: str
    key: str
    rows: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ReferenceExportReport:
    """The outcome of a single day's reference export."""

    export_date: date
    bucket: str
    day_prefix: str
    manifest_key: str
    generated_at_utc: datetime
    files: tuple[ReferenceFileEntry, ...]
    skipped_symbols: tuple[str, ...]

    @property
    def instrument_count(self) -> int:
        """The number of instrument files written (excludes the manifest)."""
        return len(self.files)


@dataclass(frozen=True, slots=True)
class _SpotSeries:
    """A discovered spot 1-minute partition's coordinates."""

    exchange: str
    segment: str
    symbol: str


def resolve_export_bucket(env: Mapping[str, str] | None = None) -> str:
    """Resolve the destination S3 bucket from the environment — fail-loud.

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    str
        The non-blank bucket name.

    Raises
    ------
    ReferenceExportConfigError
        When ``$BRUTEX_REFERENCE_EXPORT_BUCKET`` is unset or blank. A bucket
        name is NEVER guessed (Golden Rule #1).
    """
    mapping = os.environ if env is None else env
    bucket = mapping.get(REFERENCE_EXPORT_BUCKET_ENV, "").strip()
    if not bucket:
        raise ReferenceExportConfigError(
            f"{REFERENCE_EXPORT_BUCKET_ENV} is unset — set it to the TickVault "
            "reference-archive bucket the export writes to (BruteX never guesses "
            "an external bucket name)"
        )
    return bucket


def resolve_export_region(env: Mapping[str, str] | None = None) -> str | None:
    """Resolve the optional AWS region override — stripped, blank → ``None``.

    The ONE region-env read shared by both CLI call sites (round-4 refutation
    fix: each site read ``os.environ.get(...) or None`` UNstripped, so a
    whitespace-padded ``BRUTEX_REFERENCE_EXPORT_REGION=" us-east-1 "`` — or a
    value of pure whitespace — reached ``boto3.client(region_name=...)``
    verbatim and botocore raised ``InvalidRegionError`` out of the shim).
    Blank / whitespace-only / unset all resolve to ``None`` so
    :func:`build_real_s3_uploader` falls back to
    :data:`DEFAULT_REFERENCE_EXPORT_REGION`. A syntactically malformed but
    non-blank region still reaches the client build — that failure is converted
    to :class:`ReferenceExportConfigError` there (the ONE fence type).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    str | None
        The stripped region name, or ``None`` when unset/blank.
    """
    mapping = os.environ if env is None else env
    region = mapping.get(REFERENCE_EXPORT_REGION_ENV, "").strip()
    return region or None


def resolve_reference_export_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the ``BRUTEX_REFERENCE_EXPORT`` post-pull-seam gate — fail-loud.

    unset / ``""`` / ``"0"`` / ``"off"`` / ``"false"`` / ``"no"`` → ``False``
    (default OFF — the export is opt-in: it needs an external bucket + the
    ``boto3`` extra); ``"1"`` / ``"on"`` / ``"true"`` / ``"yes"`` → ``True``
    (case-insensitive, whitespace-stripped). Anything else raises — junk is
    never silently coerced (the DEC-RAM-BUDGETS-001 fail-loud env convention),
    and the CLI resolves this BEFORE any vendor work so a junk value can never
    fail an already-completed pull.

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    bool
        ``True`` when the post-pull reference-export seam is enabled.

    Raises
    ------
    ValueError
        If the value is set to anything outside the recognised token sets.
    """
    mapping = os.environ if env is None else env
    token = mapping.get(REFERENCE_EXPORT_ENABLED_ENV, "").strip().lower()
    if token in _ENV_ON_TOKENS:
        return True
    if token in _ENV_OFF_TOKENS:
        return False
    raise ValueError(
        f"{REFERENCE_EXPORT_ENABLED_ENV}={token!r} is not a recognised switch value; "
        "use 1/on/true/yes or 0/off/false/no"
    )


def discover_reference_instruments(
    lake_root: Path, *, segments: Sequence[str] = REFERENCE_EXPORT_SEGMENTS
) -> list[_SpotSeries]:
    """Discover the spot 1-minute partitions in scope — a wheel-free lake walk.

    Walks ``<lake>/bars/<exch>/<seg>/<symbol>/1minute`` for the in-scope spot
    *segments*, keeping each symbol whose ``1minute`` partition holds at least
    one ``.parquet``. Deterministically ordered (exchange, segment, symbol).
    O(spot partitions); no Parquet is opened here.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    segments
        Spot segments to include; defaults to :data:`REFERENCE_EXPORT_SEGMENTS`.

    Returns
    -------
    list[_SpotSeries]
        The discovered series coordinates, sorted; ``[]`` when ``bars/`` is
        absent.
    """
    seg_set = frozenset(segments)
    bars = lake_root / "bars"
    if not bars.is_dir():
        return []
    found: list[_SpotSeries] = []
    for exch_dir in sorted(p for p in bars.iterdir() if p.is_dir()):
        if exch_dir.name not in VALID_EXCHANGES:
            continue
        for seg_dir in sorted(p for p in exch_dir.iterdir() if p.is_dir()):
            if seg_dir.name not in seg_set:
                continue
            for sym_dir in sorted(p for p in seg_dir.iterdir() if p.is_dir()):
                tf_dir = sym_dir / REFERENCE_EXPORT_INTERVAL
                if not tf_dir.is_dir() or not any(tf_dir.rglob("*.parquet")):
                    continue
                found.append(
                    _SpotSeries(exchange=exch_dir.name, segment=seg_dir.name, symbol=sym_dir.name)
                )
    return found


def _read_clean_day_frame(
    lake_root: Path, series: _SpotSeries, export_date: date
) -> pl.DataFrame | None:
    """Read *series*'s cleaned, stored 1-minute bars for *export_date*, or ``None``.

    The bytes come back through the keep-FIRST dedup gate
    (:func:`~brutex.data.lake_day_glob.read_day_intraday_frame`), filtered to
    the exact IST calendar day and projected to :data:`REFERENCE_EXPORT_COLUMNS`
    (sorted by ``timestamp``). ``None`` when the partition holds no bar for the
    day.
    """
    import polars as pl

    base = (
        lake_root
        / "bars"
        / series.exchange
        / series.segment
        / series.symbol
        / REFERENCE_EXPORT_INTERVAL
    )
    frame = read_day_intraday_frame(base, export_date, symbol=series.symbol)
    if frame is None or frame.is_empty():
        return None
    day_frame = (
        frame.filter(pl.col("timestamp").dt.convert_time_zone(_IST).dt.date() == export_date)
        .select(list(REFERENCE_EXPORT_COLUMNS))
        .sort("timestamp")
    )
    if day_frame.is_empty():
        return None
    return day_frame


def export_reference_day(
    lake_root: Path,
    export_date: date,
    *,
    uploader: S3Uploader,
    bucket: str,
    prefix: str = REFERENCE_EXPORT_PREFIX,
    log: structlog.stdlib.BoundLogger | None = None,
) -> ReferenceExportReport:
    """Export *export_date*'s cleaned stored 1-minute bars to S3 — manifest LAST.

    Discovers the in-scope spot instruments, streams each one's day frame to a
    Parquet buffer (one instrument in memory at a time — O(instruments), no
    whole-day blowup), computes its row count + sha256, uploads it, and — only
    after EVERY instrument file has been written — uploads the ``manifest.json``
    LAST so a present manifest always describes a COMPLETE export.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    export_date
        The IST calendar day to export.
    uploader
        The bound-bucket :class:`S3Uploader` (WRITE-ONLY).
    bucket
        The destination bucket name (stamped in the manifest for the consumer).
    prefix
        Top-level key prefix; defaults to :data:`REFERENCE_EXPORT_PREFIX`.
    log
        Optional bound logger; defaults to the module logger.

    Returns
    -------
    ReferenceExportReport
        The set of written files + skipped (no-data) symbols.

    Raises
    ------
    Exception
        Any uploader / read error propagates BEFORE the manifest is written —
        so a failed export never leaves a manifest, and the seam
        (:func:`run_reference_export_seam`) fences it into a WARN + alert.
    """
    logger = log if log is not None else _LOG
    day_prefix = f"{prefix}/{export_date.isoformat()}"
    generated_at = datetime.now(UTC)

    series_list = discover_reference_instruments(lake_root)
    files: list[ReferenceFileEntry] = []
    skipped: list[str] = []
    for series in series_list:
        day_frame = _read_clean_day_frame(lake_root, series, export_date)
        if day_frame is None:
            skipped.append(series.symbol)
            continue
        buffer = BytesIO()
        day_frame.write_parquet(buffer)
        body = buffer.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        key = f"{day_prefix}/{series.symbol}.parquet"
        uploader.put_object(key=key, body=body, content_type=_PARQUET_CONTENT_TYPE)
        files.append(
            ReferenceFileEntry(
                symbol=series.symbol,
                exchange=series.exchange,
                segment=series.segment,
                key=key,
                rows=day_frame.height,
                sha256=digest,
            )
        )

    manifest_key = f"{day_prefix}/{MANIFEST_NAME}"
    manifest_body = _render_manifest(
        export_date=export_date,
        bucket=bucket,
        day_prefix=day_prefix,
        generated_at=generated_at,
        files=files,
        skipped=skipped,
    )
    # Manifest LAST — a single PutObject is atomic, so a consumer that sees the
    # manifest is guaranteed every listed file already landed (never a false-OK).
    uploader.put_object(key=manifest_key, body=manifest_body, content_type=_MANIFEST_CONTENT_TYPE)
    logger.info(
        "reference_export.day_written",
        export_date=export_date.isoformat(),
        bucket=bucket,
        instruments=len(files),
        skipped=len(skipped),
        manifest_key=manifest_key,
    )
    return ReferenceExportReport(
        export_date=export_date,
        bucket=bucket,
        day_prefix=day_prefix,
        manifest_key=manifest_key,
        generated_at_utc=generated_at,
        files=tuple(files),
        skipped_symbols=tuple(skipped),
    )


def _render_manifest(
    *,
    export_date: date,
    bucket: str,
    day_prefix: str,
    generated_at: datetime,
    files: Sequence[ReferenceFileEntry],
    skipped: Sequence[str],
) -> bytes:
    """Render the manifest JSON bytes (stable key order, UTF-8)."""
    payload = {
        "source": REFERENCE_EXPORT_SOURCE_TAG,
        "export_date": export_date.isoformat(),
        "interval": REFERENCE_EXPORT_INTERVAL,
        "generated_at_utc": generated_at.isoformat(),
        "bucket": bucket,
        "prefix": day_prefix,
        "instrument_count": len(files),
        "skipped_count": len(skipped),
        "skipped_symbols": list(skipped),
        "files": [
            {
                "key": entry.key,
                "symbol": entry.symbol,
                "exchange": entry.exchange,
                "segment": entry.segment,
                "rows": entry.rows,
                "sha256": entry.sha256,
            }
            for entry in files
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def run_reference_export_seam(
    lake_root: Path,
    export_date: date,
    *,
    uploader: S3Uploader,
    bucket: str,
    alert: bool = True,
    log: structlog.stdlib.BoundLogger | None = None,
) -> ReferenceExportReport | None:
    """Run the post-pull reference export FENCED — never fails the pull.

    The best-effort seam: on ANY error it emits exactly ONE loud WARN + a
    best-effort Telegram alert (when *alert*) and returns ``None`` — the export
    is a downstream OBSERVER of the lake and must NEVER fail an already-completed
    pull (the ``_run_cli_greeks_enrich`` fence convention). Because
    :func:`export_reference_day` writes the manifest LAST, a fenced failure
    leaves NO manifest, so the consumer detects the day as incomplete rather
    than false-OK.

    Parameters
    ----------
    lake_root
        Top-level lake directory.
    export_date
        The IST calendar day to export.
    uploader
        The bound-bucket :class:`S3Uploader` (production builds the real boto3
        client in the CLI shim; tests inject a fake).
    bucket
        The destination bucket name.
    alert
        When ``True`` (default), fire the best-effort Telegram failure alert.
    log
        Optional bound logger; defaults to the module logger.

    Returns
    -------
    ReferenceExportReport | None
        The report on success, or ``None`` when the fenced export failed.
    """
    logger = log if log is not None else _LOG
    try:
        report = export_reference_day(
            lake_root, export_date, uploader=uploader, bucket=bucket, log=logger
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught — fenced; never fails the pull
        logger.warning(
            "reference_export.failed",
            export_date=export_date.isoformat(),
            bucket=bucket,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        if alert:
            from brutex.notifications.sweep_alerts import emit_reference_export_failed_alert

            emit_reference_export_failed_alert(
                export_date=export_date, reason=f"{type(exc).__name__}: {exc}"
            )
        return None
    return report


def build_real_s3_uploader(*, bucket: str, region: str | None = None) -> S3Uploader:
    """Build the production :class:`S3Uploader` backed by a boto3 S3 client (lazy import).

    Mirrors :func:`~brutex.adapters.groww.aws_client.build_real_aws_client`: a
    missing ``boto3`` raises a clear :class:`ReferenceExportConfigError` naming
    the extra. Credentials come from the ambient AWS chain (instance role / SSM
    profile) — NEVER a hardcoded key (the ``config/credentials_aws.py``
    precedent). *region* defaults to :data:`DEFAULT_REFERENCE_EXPORT_REGION`.

    EVERY build failure is :class:`ReferenceExportConfigError` — the ONE fence
    type (round-4 refutation fix): ``boto3.client(...)`` itself raises
    non-boto3-typed errors on config gaps — botocore ``InvalidRegionError`` (a
    ``ValueError`` subclass) on a malformed region, ``ProfileNotFound`` on a bad
    ``AWS_PROFILE``, ``DataNotFoundError``/``UnknownServiceError`` on a partial
    install — none of which the CLI shims' ``except ReferenceExportConfigError``
    guards would catch, so they escaped ``cmd_fetch`` AFTER a fully-persisted
    pull (traceback + red exit on a green pull). Converting HERE fixes every
    call site at once and keeps the fence contract single-typed.
    """
    try:
        import boto3  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:
        raise ReferenceExportConfigError(
            "boto3 is required to export the reference lake to S3; install the extra: "
            'pip install -e ".[token-broker]"'
        ) from exc

    try:
        client = boto3.client("s3", region_name=region or DEFAULT_REFERENCE_EXPORT_REGION)
    except Exception as exc:  # pylint: disable=broad-exception-caught — converted, never swallowed
        # Deliberately broad: botocore's config-gap errors share no useful base
        # (InvalidRegionError IS a ValueError; ProfileNotFound is its own tree).
        # The error is CONVERTED to the one fence type + chained, never dropped.
        raise ReferenceExportConfigError(
            f"failed to build the S3 client (region={region or DEFAULT_REFERENCE_EXPORT_REGION!r}"
            f", bucket={bucket!r}): {type(exc).__name__}: {exc}"
        ) from exc
    return _Boto3S3Uploader(bucket=bucket, client=client)


class _Boto3S3Uploader:
    """Adapts the boto3 low-level S3 client to the :class:`S3Uploader` Protocol.

    WRITE-ONLY: it only ever calls ``put_object`` on the ONE bound bucket. The
    bounded-IAM policy (write-only to the ``groww-reference/`` prefix) lives on
    the bucket side; this client never lists/reads/deletes.
    """

    def __init__(self, *, bucket: str, client: object) -> None:
        self._bucket = bucket
        self._client = client

    def put_object(self, *, key: str, body: bytes, content_type: str) -> None:
        """Write *body* to ``s3://<bucket>/<key>`` via a single boto3 ``put_object``."""
        self._client.put_object(  # type: ignore[attr-defined]
            Bucket=self._bucket, Key=key, Body=body, ContentType=content_type
        )


__all__ = [
    "DEFAULT_REFERENCE_EXPORT_REGION",
    "MANIFEST_NAME",
    "REFERENCE_EXPORT_BUCKET_ENV",
    "REFERENCE_EXPORT_COLUMNS",
    "REFERENCE_EXPORT_ENABLED_ENV",
    "REFERENCE_EXPORT_INTERVAL",
    "REFERENCE_EXPORT_PREFIX",
    "REFERENCE_EXPORT_REGION_ENV",
    "REFERENCE_EXPORT_SEGMENTS",
    "REFERENCE_EXPORT_SOURCE_TAG",
    "ReferenceExportConfigError",
    "ReferenceExportError",
    "ReferenceExportReport",
    "ReferenceFileEntry",
    "S3Uploader",
    "build_real_s3_uploader",
    "discover_reference_instruments",
    "export_reference_day",
    "resolve_export_bucket",
    "resolve_export_region",
    "resolve_reference_export_enabled",
    "run_reference_export_seam",
]
