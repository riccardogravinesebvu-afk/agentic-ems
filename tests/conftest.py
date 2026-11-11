from datetime import date

import pytest

from agentic_ems.generator import generate
from agentic_ems.semantic import open_db


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "plant.duckdb"
    generate(path, start=date(2026, 10, 19), end=date(2026, 11, 1))
    return path


@pytest.fixture(scope="session")
def con(db_path):
    return open_db(db_path)


@pytest.fixture(scope="session")
def full_db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("full") / "plant.duckdb"
    generate(path)
    return path
