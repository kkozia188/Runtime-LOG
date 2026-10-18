from pathlib import Path

import pytest

from runtime_log.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


@pytest.fixture(scope="session")
def profile():
    return load_profile(ROOT / "profiles/rt_control.yaml")


@pytest.fixture
def fixture_text():
    def read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return read
