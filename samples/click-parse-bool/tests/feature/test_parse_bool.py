import pytest
from click.utils import parse_bool

def test_none_returns_default():
    assert parse_bool(None, default=True) is True
    assert parse_bool(None, default=False) is False

def test_none_uses_default_when_omitted():
    # default is False when not provided
    assert parse_bool(None) is False

def test_bool_passthrough():
    assert parse_bool(True) is True
    assert parse_bool(False) is False

def test_string_truthy_and_falsy():
    truthy = ["1", "TRUE", "Yes", " on "]
    falsy = ["0", "false", "No", "OFF"]
    for val in truthy:
        assert parse_bool(val) is True
    for val in falsy:
        assert parse_bool(val) is False

def test_invalid_string_raises():
    for bad in ["maybe", "123", "", "   "]:
        with pytest.raises(ValueError):
            parse_bool(bad)

def test_invalid_type_raises():
    for bad in [42, ["true"], b"true"]:
        with pytest.raises(ValueError):
            parse_bool(bad)
