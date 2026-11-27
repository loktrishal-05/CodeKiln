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
