import pytest
from click.core import parse_json_param
from click.exceptions import BadParameter

def test_valid_json():
    assert parse_json_param('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}

def test_empty_string_returns_none():
    assert parse_json_param('') is None

def test_invalid_json_raises():
    with pytest.raises(BadParameter):
        parse_json_param('{"a": 1,}')
