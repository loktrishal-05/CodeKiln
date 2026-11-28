import pytest
from click import termui

def test_valid_within_bounds(monkeypatch):
    # Simulate user entering 5
    monkeypatch.setattr(termui, "prompt", lambda *args, **kwargs: 5)
    assert termui.prompt_int_range("Enter a number", min=1, max=10) == 5

def test_value_below_min_raises(monkeypatch):
    monkeypatch.setattr(termui, "prompt", lambda *args, **kwargs: 0)
    with pytest.raises(ValueError) as exc:
        termui.prompt_int_range("Enter", min=1, max=10)
    assert "less than minimum" in str(exc.value)

def test_inconsistent_bounds_raises(monkeypatch):
    # Ensure prompt is never called when bounds are inconsistent
    called = False
    def fake_prompt(*args, **kwargs):
        nonlocal called
        called = True
        return 5
    monkeypatch.setattr(termui, "prompt", fake_prompt)
    with pytest.raises(ValueError) as exc:
        termui.prompt_int_range("Enter", min=10, max=5)
    assert "min cannot be greater than max" in str(exc.value)
    assert not called
