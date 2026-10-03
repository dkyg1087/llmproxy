import os
import pytest
import asyncio

# Ensure all tests use an isolated test database
os.environ["DB_PATH"] = "test_pytest_db.db"
import src.config
src.config.DB_PATH = "test_pytest_db.db"
from src.db import init_db


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    asyncio.run(init_db())
    yield
    # Cleanup test db file after session finishes
    for f in ("test_pytest_db.db", "test_pytest_db.db-shm", "test_pytest_db.db-wal"):
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
