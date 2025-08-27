from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    # Isola state/ per i test
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    yield


@pytest.fixture
def sample_csv(tmp_path):
    src = Path(__file__).parent / "data" / "sample_products.csv"
    dst = tmp_path / "in.csv"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst
