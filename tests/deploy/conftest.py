"""Make the self-contained ``deploy/aws_token_minter`` handler importable in tests.

The Lambda handler lives OUTSIDE the ``brutex`` package (it is deployed as a
standalone bundle — DEC-TOKEN-MINTER-CENTRAL-001), so it is not on the default
import path under ``testpaths = ["tests"]``. This conftest prepends the repo
root to ``sys.path`` so ``import deploy.aws_token_minter.handler`` resolves,
without polluting the ``brutex`` package namespace.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
