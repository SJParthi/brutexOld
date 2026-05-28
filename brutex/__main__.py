"""Enable ``python -m brutex`` as an alias for the ``brutex`` console script.

Delegates to :func:`brutex.cli.main` so ``python -m brutex <command>``
behaves identically to the installed ``brutex <command>`` entry point
(DEC-CLONE-RUN-001). This keeps the clone-and-run path working even
before the console script is on ``PATH``.
"""

from __future__ import annotations

import sys

from brutex.cli import main

if __name__ == "__main__":
    sys.exit(main())
