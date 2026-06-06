"""Pre-mock oprim so aegis-agent unit tests run without git+ssh VCS install."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# oprim is a VCS dep; inject a MagicMock before any aegis_agent module is imported.
# This runs at conftest load time — before pytest collects test modules — so the
# module-level `from oprim import ...` in _collector.py binds the mock attributes.
if "oprim" not in sys.modules:
    sys.modules["oprim"] = MagicMock()
