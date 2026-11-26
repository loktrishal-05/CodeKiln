import json

from codekiln.validation.parsers import parse_ctrf, parse_junit


def test_ctrf_statuses_and_unknown_is_failure():
    doc = {"results": {"tests": [
        {"name": "t_ok", "status": "passed", "filePath": "tests/a.py"},
        {"name": "t_bad", "status": "failed"},
        {"name": "t_skip", "status": "pending"},
        {"name": "t_odd", "status": "other"},
    ]}}
    assert parse_ctrf(json.dumps(doc)) == {
        "tests/a.py::t_ok": "passed", "t_bad": "failed", "t_skip": "skipped", "t_odd": "failed",
    }


def test_junit_statuses():
    xml = """<testsuites><testsuite>
      <testcase classname="tests.test_a" name="ok"/>
      <testcase classname="tests.test_a" name="bad"><failure message="x"/></testcase>
      <testcase classname="tests.test_a" name="crash"><error message="x"/></testcase>
      <testcase classname="tests.test_a" name="skip"><skipped/></testcase>
    </testsuite></testsuites>"""
    assert parse_junit(xml) == {
        "tests.test_a::ok": "passed", "tests.test_a::bad": "failed",
        "tests.test_a::crash": "failed", "tests.test_a::skip": "skipped",
    }
