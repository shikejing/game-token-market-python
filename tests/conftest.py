import os
import tempfile

import pytest

from app.service import init_service


@pytest.fixture
def svc(tmp_path):
    """Fresh service backed by an isolated temp data directory."""
    service = init_service(str(tmp_path / "marketdata"))
    yield service
    service.stop()


@pytest.fixture
def engine(svc):
    return svc.engine
