"""让 `python -m importer` 可以直接跑。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
