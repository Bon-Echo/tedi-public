"""Shared test setup.

Set the env vars required by `Settings` validation BEFORE any `app.*` import
so that pytest collection does not fail on a fresh checkout.
"""

import os
import sys

os.environ.setdefault(
    "ANTHROPIC_API_KEY", "test-key-pretending-to-be-long-enough-for-validator"
)
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://tedi:password@localhost:5432/tedi_public"
)
os.environ.setdefault("ADMIN_SESSION_SECRET", "test-secret-must-be-long-enough-1234567890")
os.environ.setdefault("ADMIN_ALLOWED_DOMAIN", "bonecho.ai")
os.environ.setdefault("ADMIN_UI_ORIGIN", "http://localhost:3001")

# Ensure the worktree root is on sys.path so `import app.*` works when pytest
# is invoked from a subdirectory.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
