"""`python -m privatemetrics ...` (same as the `privatemetrics` console script)."""
import sys

from .cli import main

sys.exit(main())
