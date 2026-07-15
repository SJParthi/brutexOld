"""Daily TickVault cross-verification producer (DEC-CROSSVERIFY-PRODUCER-001).

Package marker only — every module is imported explicitly (the discovery gate
excludes ``__init__.py``). The package exports one day's stored 1-minute bars
per symbol to the pinned TickVault CSV contract and publishes the files to S3
(``s3://tv-prod-cold/crossverify/groww/<day>/<segment>/<symbol>.csv``), gated
on the today-completeness classifier — a missing bar is a withheld file, never
an invented row (Golden Rule #1).
"""
