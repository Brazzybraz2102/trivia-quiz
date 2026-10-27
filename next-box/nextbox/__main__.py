"""`python -m nextbox ...`: the same as the `nextbox` command."""
import sys

from .cli import main

sys.exit(main())
