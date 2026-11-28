import os
import pytest
from click.shell_completion import completion_enabled

def test_not_set(monkeypatch):
    monkeypatch.delenv('CLICK_COMPLETION_DISABLED', raising=False)
    assert completion_enabled() is True

def test_false_values(monkeypatch):
    for val in ('0', 'false', 'FALSE'):
        monkeypatch.setenv('CLICK_COMPLETION_DISABLED', val)
        assert completion_enabled() is False

def test_true_values(monkeypatch):
    for val in ('1', 'true', 'TRUE'):
        monkeypatch.setenv('CLICK_COMPLETION_DISABLED', val)
        assert completion_enabled() is True

def test_invalid_value(monkeypatch):
    monkeypatch.setenv('CLICK_COMPLETION_DISABLED', 'maybe')
    with pytest.raises(ValueError) as exc:
        completion_enabled()
    assert 'Invalid value' in str(exc.value)
