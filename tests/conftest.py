from datetime import date
from pathlib import Path

import pytest

from keika.db import connect
from keika.settings import Settings

SAMPLE = Path(__file__).parent.parent / "sample_data"


@pytest.fixture
def sample_settings(tmp_path) -> Settings:
    return Settings(drive_root=SAMPLE / "drive", db_path=tmp_path / "keika.db", as_of=date(2026, 11, 4))


@pytest.fixture
def conn(sample_settings):
    c = connect(sample_settings)
    yield c
    c.close()
