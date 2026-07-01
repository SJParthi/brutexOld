"""Centralized Groww daily-token minter — AWS Lambda deploy package.

DEC-TOKEN-MINTER-CENTRAL-001 (docs/adr/0014-centralized-token-minter.md). This
package is deployed as a self-contained AWS Lambda; it does NOT import the
``brutex`` package. See :mod:`deploy.aws_token_minter.handler` for the handler.
"""

from __future__ import annotations
