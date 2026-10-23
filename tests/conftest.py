import pytest_asyncio

from miku.database import Database


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    await db.initialize()
    yield db
    await db.close()
