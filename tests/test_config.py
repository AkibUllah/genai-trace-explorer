from genai_traces.config import Settings


def test_hosting_provider_database_urls_get_a_driver():
    pg = Settings(_env_file=None, database_url="postgres://u:p@db:5432/traces")
    assert pg.database_url == "postgresql+psycopg://u:p@db:5432/traces"
    explicit = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@db/traces")
    assert explicit.database_url == "postgresql+psycopg://u:p@db/traces"
    assert Settings(_env_file=None).database_url == "sqlite:///./traces.db"


def test_blank_tokens_mean_no_auth(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "  ")
    monkeypatch.setenv("READ_TOKEN", "")
    monkeypatch.setenv("PRICING_FILE", "")
    settings = Settings(_env_file=None)
    assert settings.ingest_token is None and settings.read_token is None
    assert settings.pricing_file is None
