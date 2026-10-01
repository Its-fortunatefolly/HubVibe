import pytest


@pytest.fixture(autouse=True)
def _isolated_mpp_hash_ledger(tmp_path, monkeypatch):
    """Every test gets its own spent-hash record, so a hash spent in one test
    (or one run) is never already spent in the next."""
    monkeypatch.setenv("MPP_HASH_LEDGER_PATH", str(tmp_path / "mpp-hashes.db"))


@pytest.fixture(autouse=True)
def _isolated_purchase_book(tmp_path, monkeypatch):
    """Every test writes its purchase-book rows to its own file (never a real
    /data), and no test posts a sale alert."""
    monkeypatch.setenv("PURCHASE_BOOK_PATH", str(tmp_path / "purchases.db"))
    monkeypatch.delenv("PURCHASE_ALERT_WEBHOOK", raising=False)
