import pytest

from gtm_pilot.core import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()
