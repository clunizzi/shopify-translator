import pytest


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    # Isola state/ per i test
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    yield
