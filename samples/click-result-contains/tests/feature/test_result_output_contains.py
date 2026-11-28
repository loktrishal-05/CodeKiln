import pytest
from click.testing import Result

class DummyResult(Result):
    def __init__(self, output: str):
        # Bypass the normal constructor; set the internal attributes directly.
        self._output = output
        self._stdout = output
        self._stderr = ''
        self.exit_code = 0
        self.exception = None
    def output(self) -> str:
        return self._output

def test_contains_case_sensitive_true():
    r = DummyResult('Hello World')
    assert r.output_contains('World') is True
    assert r.output_contains('world') is False

def test_contains_case_insensitive():
    r = DummyResult('Hello World')
    assert r.output_contains('world', case_sensitive=False) is True
    assert r.output_contains('WORLD', case_sensitive=False) is True

def test_invalid_substring_type():
    r = DummyResult('test')
    with pytest.raises(TypeError):
        r.output_contains(123)

def test_invalid_case_sensitive_type():
    r = DummyResult('test')
    with pytest.raises(TypeError):
        r.output_contains('test', case_sensitive='yes')

def test_contains_iterable_true():
    r = DummyResult('alpha beta gamma')
    assert r.output_contains(['delta', 'beta', 'epsilon']) is True

def test_contains_iterable_false():
    r = DummyResult('alpha beta gamma')
    assert r.output_contains(['delta', 'epsilon']) is False

def test_iterable_with_non_string_item():
    r = DummyResult('something')
    with pytest.raises(TypeError):
        r.output_contains(['valid', 123])
