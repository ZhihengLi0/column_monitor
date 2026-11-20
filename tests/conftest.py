import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_state(tmp_path):
    """A StateStore backed by a temp file."""
    from bfm.core.state import StateStore
    return StateStore(tmp_path / "state.json").load()


@pytest.fixture
def live_state_copy(tmp_path):
    """A StateStore loaded from a COPY of the real monitor_state.json (if it
    exists) — used to prove migration works on production data."""
    import shutil
    from bfm.core.state import StateStore
    src = ROOT / "monitor_state.json"
    if not src.exists():
        pytest.skip("no live state file")
    dst = tmp_path / "state.json"
    shutil.copy(src, dst)
    return StateStore(dst).load()
