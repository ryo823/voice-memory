import pytest


@pytest.fixture
def db_path(tmp_path):
    from voice_memory import database as db
    p = tmp_path / "test.db"
    db.init_db(p)
    return p
