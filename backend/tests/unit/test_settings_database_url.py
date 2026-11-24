from app.core.config import Settings


def test_managed_postgres_urls_get_asyncpg_driver():
    assert Settings._async_database_driver("postgres://db.example/ac") == (
        "postgresql+asyncpg://db.example/ac"
    )
    assert Settings._async_database_driver("postgresql://db.example/ac") == (
        "postgresql+asyncpg://db.example/ac"
    )
    assert Settings._async_database_driver("postgresql+asyncpg://db.example/ac") == (
        "postgresql+asyncpg://db.example/ac"
    )
