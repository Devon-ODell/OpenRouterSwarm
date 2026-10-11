"""Suite-wide isolation: nothing a test does may land in the operator's lasting ledgers.

Agents built in tests (model "test:free") record a receipt per request, and the default
receipt file is ~/.albatross/routes.jsonl. By 2026-10-11 it held 804 fixture rows dated as real
requests, so it could not be used for accounting. Every test, and every flint subprocess a
test starts (they inherit the environment), writes receipts here instead.
"""
import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="flint-tests-"))
os.environ["FLINT_ROUTES_FILE"] = str(_ROOT / "albatross" / "routes.jsonl")
