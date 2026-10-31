import os

import pytest

from genai_traces.ingest import Ingestor
from genai_traces.otlp import flatten, parse_request
from genai_traces.pricing import PriceTable
from genai_traces.store import Store, make_engine, metadata


@pytest.fixture
def db_url() -> str:
    """SQLite in memory, or the database in TEST_DATABASE_URL (CI runs the suite on PostgreSQL)."""
    return os.environ.get("TEST_DATABASE_URL", "sqlite://")


@pytest.fixture
def store(db_url):
    engine = make_engine(db_url)
    metadata.drop_all(engine)
    yield Store(engine)
    metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def ingestor(store):
    return Ingestor(store, PriceTable.load())


def load(ingestor, recorder):
    spans, _ = flatten(parse_request(recorder.protobuf(), "application/x-protobuf"))
    return ingestor.ingest(spans)
