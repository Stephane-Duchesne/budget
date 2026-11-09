from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PRIVATE = FIXTURES / "private"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def private_file(name: str) -> Path:
    """Relevé réel, jamais versionné : le test est sauté si le fichier est absent."""
    path = PRIVATE / name
    if not path.exists():
        pytest.skip(f"fichier privé absent : tests/fixtures/private/{name}")
    return path
