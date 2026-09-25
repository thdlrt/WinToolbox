"""Never register a test data store as the real machine's memory client."""
import pytest


@pytest.fixture(autouse=True)
def isolate_project_memory_machine_state(monkeypatch, tmp_path):
    from toolbox.project_memory import store
    monkeypatch.setattr(store, 'state_directory', lambda: tmp_path / 'machine-state')
