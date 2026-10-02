"""Shared pytest fixtures."""
import pytest

from market.engine import MarketEngine
from market.models import Side


@pytest.fixture()
def engine(tmp_path):
    """A fresh market engine backed by a temp data dir."""
    eng = MarketEngine(str(tmp_path / "data"))
    eng.load()
    return eng


@pytest.fixture()
def funded_engine(engine):
    """An engine with two funded accounts: alice and bob."""
    engine.open_account("alice", 1_000_000)
    engine.open_account("bob", 1_000_000)
    return engine
