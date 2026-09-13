"""Re-export FakeGhClient from arsenal.gh_client (single source of truth)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "arsenal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from gh_client import FakeGhClient, GhClient, default_client  # noqa: E402

__all__ = ["FakeGhClient", "GhClient", "default_client"]
