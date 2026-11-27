import json
from collections.abc import Mapping

import pytest
from spec_examples import INSTRUCTION, make_spec

from codekiln.planner.models import Category, Plan
from codekiln.synthesis.llm_router import (
    LLMWriter,
    ModelConfig,
    RouterError,
    complete,
    parse_spec,
)
from codekiln.synthesis.models import SynthesisError

CONFIG = ModelConfig(base_url="https://llm.test/v1", api_key="sk-test", model="test-model")
PLAN = Plan(Category.API_CLI, frozenset({"src/click/utils.py"}), frozenset({"echo"}), "forward", 2)


def reply(content: object) -> Mapping[str, object]:
    text = content if isinstance(content, str) else json.dumps(content)
    return {"choices": [{"message": {"content": text}}]}


def test_writer_sends_openai_payload_and_returns_valid_spec() -> None:
    calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> Mapping[str, object]:
        calls.append((url, headers, payload))
        return reply(make_spec().to_dict())

    spec = LLMWriter(CONFIG, post=post).write(PLAN, repo="click", base_commit="abc")
    assert spec == make_spec()
    url, headers, payload = calls[0]
    assert url == "https://llm.test/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert payload["model"] == "test-model"
    messages = payload["messages"]
    assert isinstance(messages, list) and messages[0]["role"] == "system"
    assert "src/click/utils.py" in messages[1]["content"]


def test_writer_repairs_a_rejected_spec_once() -> None:
    calls: list[dict[str, object]] = []

    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> Mapping[str, object]:
        calls.append(payload)
        if len(calls) == 1:
            return reply(make_spec(instruction=INSTRUCTION.replace("- REQ-2:", "- second:")).to_dict())
        return reply(make_spec().to_dict())

    spec = LLMWriter(CONFIG, post=post, retries=2).write(PLAN, repo="click", base_commit="abc")
    assert spec == make_spec()
    assert len(calls) == 2
    messages = calls[1]["messages"]
    assert isinstance(messages, list)
    assert any("Rejected:" in m["content"] and "REQ-2" in m["content"] for m in messages[2:])


def test_writer_repairs_rtm_failures_too() -> None:
    calls: list[object] = []
    unnamed = INSTRUCTION.replace("`CliRunner`", "the runner")

    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> Mapping[str, object]:
        calls.append(payload)
        return reply(make_spec(instruction=unnamed if len(calls) == 1 else INSTRUCTION).to_dict())

    assert LLMWriter(CONFIG, post=post).write(PLAN, repo="click", base_commit="abc") == make_spec()
    assert len(calls) == 2


def test_writer_gives_up_after_bounded_retries() -> None:
    calls: list[object] = []

    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> Mapping[str, object]:
        calls.append(payload)
        return reply("not json at all")

    with pytest.raises(SynthesisError, match="2 attempts"):
        LLMWriter(CONFIG, post=post, retries=1).write(PLAN, repo="click", base_commit="abc")
    assert len(calls) == 2


def test_parse_accepts_fenced_json() -> None:
    fenced = "```json\n" + json.dumps(make_spec().to_dict()) + "\n```"
    assert parse_spec(fenced) == make_spec()


@pytest.mark.parametrize("text", ["", "no object here", "{oops", "[1, 2]"])
def test_parse_rejects_non_specs(text: str) -> None:
    with pytest.raises(SynthesisError):
        parse_spec(text)


def test_complete_wraps_provider_failures() -> None:
    with pytest.raises(RouterError, match="no choices"):
        complete(CONFIG, [], post=lambda *a: {"error": "nope"})
    with pytest.raises(RouterError, match="empty completion"):
        complete(CONFIG, [], post=lambda *a: reply("  "))


def test_model_config_refuses_missing_credentials() -> None:
    with pytest.raises(RouterError, match="API_KEY"):
        ModelConfig(base_url="https://x", api_key="", model="m")
    with pytest.raises(RouterError, match="MODEL"):
        ModelConfig(base_url="https://x", api_key="k", model="")


def test_http_post_wraps_non_json_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from codekiln.synthesis import llm_router

    monkeypatch.setattr(llm_router.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"<html>502</html>"))
    with pytest.raises(RouterError, match="non-JSON"):
        llm_router.http_post("https://llm.test/v1/chat/completions", {}, {})


def test_model_config_repr_hides_the_key() -> None:
    assert "sk-test" not in repr(CONFIG)


def test_http_post_sends_an_explicit_user_agent_and_keeps_the_callers_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from codekiln.synthesis import llm_router

    seen: dict[str, str] = {}

    def fake_urlopen(req, timeout):  # type: ignore[no-untyped-def]
        seen.update({k.lower(): v for k, v in req.header_items()})
        return io.BytesIO(b'{"choices": []}')

    monkeypatch.setattr(llm_router.urllib.request, "urlopen", fake_urlopen)
    llm_router.http_post("https://p.invalid/v1/chat/completions", {"Authorization": "Bearer x"}, {})
    assert seen["user-agent"] == llm_router.USER_AGENT and "python-urllib" not in seen["user-agent"].lower()
    assert seen["authorization"] == "Bearer x"  # the caller's headers are untouched
    seen.clear()
    llm_router.http_post("https://p.invalid/x", {"User-Agent": "mine/1"}, {})
    assert seen["user-agent"] == "mine/1"  # an explicit caller choice wins


def _http_error(code: int, message: str = "", retry_after: str | None = None):  # type: ignore[no-untyped-def]
    import io
    import urllib.error
    from email.message import Message

    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError("https://p.invalid/x", code, "err", headers, io.BytesIO(body))


def _script(monkeypatch: pytest.MonkeyPatch, outcomes: list[object]):  # type: ignore[no-untyped-def]
    """urlopen yields each outcome in turn (an exception is raised, bytes are a 200 body)."""
    import io

    from codekiln.synthesis import llm_router

    waits: list[float] = []
    queue = list(outcomes)

    def fake_urlopen(req, timeout):  # type: ignore[no-untyped-def]
        out = queue.pop(0)
        if isinstance(out, Exception):
            raise out
        return io.BytesIO(out)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_router.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_router.time, "sleep", waits.append)
    return llm_router, waits


OK = b'{"choices": [{"message": {"content": "hi"}}]}'


def test_a_rate_limit_waits_for_retry_after_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    lr, waits = _script(monkeypatch, [_http_error(429, "slow down", "3"), _http_error(429, "slow down", "3"), OK])
    assert lr.http_post("https://p.invalid/x", {}, {})["choices"]
    assert waits == [3.0, 3.0]


def test_the_wait_is_read_from_the_message_when_there_is_no_header(monkeypatch: pytest.MonkeyPatch) -> None:
    lr, waits = _script(monkeypatch, [
        _http_error(429, "Rate limit reached on tokens per minute. Please try again in 6.5s."),
        _http_error(429, "Please try again in 1m3.2s."), OK])
    lr.http_post("https://p.invalid/x", {}, {})
    assert waits == [7.0, 63.7]


def test_a_huge_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    lr, waits = _script(monkeypatch, [_http_error(429, "", "5000"), OK])
    lr.http_post("https://p.invalid/x", {}, {})
    assert waits == [lr.MAX_WAIT_S]


def test_it_gives_up_after_the_attempt_limit_and_reports_the_providers_own_message(monkeypatch: pytest.MonkeyPatch) -> None:
    lr, waits = _script(monkeypatch, [_http_error(429, "Limit 8000 TPM", "1") for _ in range(6)])
    with pytest.raises(RouterError, match="HTTP 429: Limit 8000 TPM"):
        lr.http_post("https://p.invalid/x", {}, {})
    assert len(waits) == lr.MAX_ATTEMPTS - 1


def test_auth_and_client_errors_are_not_retried_and_never_echo_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    lr, waits = _script(monkeypatch, [_http_error(401, "bad key gsk_abcdef123456 for sk-testkey99")])
    with pytest.raises(RouterError) as err:
        lr.http_post("https://p.invalid/x", {}, {})
    assert waits == [] and "HTTP 401" in str(err.value)
    assert "gsk_abcdef123456" not in str(err.value) and "sk-testkey99" not in str(err.value)
    assert "<redacted>" in str(err.value)


def test_repair_requests_do_not_grow_with_each_attempt() -> None:
    """Re-sending every failed attempt grew each request until a provider with a per-request token
    cap (Groq free tier: 8000) refused it with HTTP 413 on the third call of the first real run."""
    sizes: list[int] = []
    last_messages: list[list[dict[str, str]]] = []

    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> Mapping[str, object]:
        messages = payload["messages"]
        assert isinstance(messages, list)
        sizes.append(len(messages))
        last_messages.append(messages)
        return reply(f"garbage attempt {len(sizes)}")

    with pytest.raises(SynthesisError, match="4 attempts"):
        LLMWriter(CONFIG, post=post, retries=3).write(PLAN, repo="click", base_commit="abc")
    assert sizes == [2, 4, 4, 4]  # system + prompt, then always + the latest reply and its rejection
    final = [m["content"] for m in last_messages[-1]]
    assert sum(c.startswith("Rejected:") for c in final) == 1  # only the latest rejection reason
    assert "garbage attempt 3" in final and "garbage attempt 1" not in final  # earlier failures are dropped
