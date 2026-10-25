import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Each test gets its own data dir, set before the `impl` fixture builds the store."""
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path / "data"))
