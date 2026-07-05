"""Make `lib` importable and isolate storage writes into a temp dir."""
import sys
from pathlib import Path

import pytest

JB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JB_ROOT))

import lib.bootstrap as bt  # noqa: E402  (needs path above)


@pytest.fixture()
def tmp_storage(tmp_path, monkeypatch):
    """Redirect every storage dir + catalog to a throwaway tree so tests
    never touch the real research data lake."""
    import lib.storage as st
    for name in ("RAW_DIR", "BRONZE_DIR", "SILVER_DIR", "GOLD_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(bt, name, d)
    monkeypatch.setattr(st, "CATALOG", tmp_path / "catalog.parquet")
    monkeypatch.setattr(st, "_LAYER_DIRS", {
        "bronze": tmp_path / "bronze_dir", "silver": tmp_path / "silver_dir",
        "gold": tmp_path / "gold_dir"})
    return tmp_path
