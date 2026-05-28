"""Enable ``python -m brutex`` as an alias for the ``brutex`` console script.

Mirrors the ``[project.scripts]`` ``brutex = brutex.cli:main`` entry so a
fresh clone can launch the dashboard even when the console script is not on
PATH::

    python -m brutex dashboard      # or: python -m brutex run

See :mod:`brutex.cli` for the full command reference.
"""

from __future__ import annotations

import sys

from brutex.cli import main

if __name__ == "__main__":
    sys.exit(main())
