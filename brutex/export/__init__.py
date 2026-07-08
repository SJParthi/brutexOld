"""Reference-data export surfaces (DEC-REFERENCE-EXPORT-S3-001).

A NEW top-level subpackage that publishes BruteX's CLEANED, STORED lake bars to
an external reference store so a SECOND consumer (TickVault's ~770-instrument
cross-verification) can read them as an independent reference source. Nothing in
here is on the brute-force ENGINE path — it is a post-pull, best-effort,
read-then-upload observer of the lake (Golden Rule #3 scope-lock: the ENGINE
stays NIFTY/BANKNIFTY/SENSEX; the F&O lake stays REFERENCE_ONLY).
"""

from __future__ import annotations

from brutex.export.reference_export import (
    REFERENCE_EXPORT_BUCKET_ENV,
    REFERENCE_EXPORT_ENABLED_ENV,
    REFERENCE_EXPORT_PREFIX,
    ReferenceExportConfigError,
    ReferenceExportError,
    ReferenceExportReport,
    ReferenceFileEntry,
    S3Uploader,
    export_reference_day,
    resolve_export_bucket,
    resolve_reference_export_enabled,
    run_reference_export_seam,
)

__all__ = [
    "REFERENCE_EXPORT_BUCKET_ENV",
    "REFERENCE_EXPORT_ENABLED_ENV",
    "REFERENCE_EXPORT_PREFIX",
    "ReferenceExportConfigError",
    "ReferenceExportError",
    "ReferenceExportReport",
    "ReferenceFileEntry",
    "S3Uploader",
    "export_reference_day",
    "resolve_export_bucket",
    "resolve_reference_export_enabled",
    "run_reference_export_seam",
]
