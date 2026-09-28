import pytest


@pytest.fixture(autouse=True)
def _isolated_mpp_hash_ledger(tmp_path, monkeypatch):
    """Every test gets its own spent-hash record, so a hash spent in one test
    (or one run) is never already spent in the next."""
    monkeypatch.setenv("MPP_HASH_LEDGER_PATH", str(tmp_path / "mpp-hashes.db"))
