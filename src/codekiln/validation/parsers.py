"""Per-test result parsers. Both return {node_id: status}, status in passed|failed|skipped."""
import json
import xml.etree.ElementTree as ET  # ponytail: stdlib XML is not hardened; swap for defusedxml if reports come from untrusted code

TestResults = dict[str, str]

_CTRF = {"passed": "passed", "failed": "failed", "skipped": "skipped", "pending": "skipped"}


def parse_ctrf(text: str) -> TestResults:
    tests = json.loads(text)["results"]["tests"]
    out: TestResults = {}
    for t in tests:
        node = f"{t['filePath']}::{t['name']}" if t.get("filePath") else t["name"]
        out[node] = _CTRF.get(t["status"], "failed")  # "other"/unknown never counts as a pass
    return out


def parse_junit(text: str) -> TestResults:
    out: TestResults = {}
    for case in ET.fromstring(text).iter("testcase"):
        cls, name = case.get("classname"), case.get("name", "")
        node = f"{cls}::{name}" if cls else name
        if case.find("failure") is not None or case.find("error") is not None:
            out[node] = "failed"
        elif case.find("skipped") is not None:
            out[node] = "skipped"
        else:
            out[node] = "passed"
    return out
