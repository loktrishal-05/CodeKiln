"""OpenAI-compatible chat router: prompt -> FeatureSpec, with bounded repair and no self-grading.

Host-only by design: the API key is read from settings and never passed to a container. The
router only proposes a spec; `validate`, the RTM audit and the execution gate decide whether it
is admissible, so a confident-sounding model cannot mark its own work correct.
"""
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from codekiln.planner.models import Plan
from codekiln.states import MAX_REPAIR_RETRIES
from codekiln.synthesis.models import FeatureSpec, SynthesisError, from_dict, validate
from codekiln.synthesis.rtm import require_ok
from codekiln.synthesis.spec_generator import render_prompt

Post = Callable[[str, dict[str, str], dict[str, object]], Mapping[str, object]]

SYSTEM = """\
You are CodeKiln's task synthesizer. You design ONE new feature and return JSON only, with no
markdown fences and no commentary. Never invent test results. The feature must not exist in the
base commit, and no upstream behaviour may be removed to make room for it.
"""


class RouterError(RuntimeError):
    """Transport or provider failure (missing key, HTTP error, malformed response)."""


class Writer(Protocol):
    """Anything the pipeline can call to turn one plan slot into one spec."""

    def write(self, plan: Plan, *, repo: str, base_commit: str) -> FeatureSpec: ...


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 8000

    def __post_init__(self) -> None:
        if not self.api_key:
            raise RouterError("CODEKILN_LLM_API_KEY is not set")
        if not self.model:
            raise RouterError("CODEKILN_LLM_MODEL is not set")


def complete(config: ModelConfig, messages: list[dict[str, str]], post: Post) -> str:
    """One chat completion; returns the assistant text or raises RouterError."""
    url = config.base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, object] = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    try:
        data = post(url, headers, payload)
    except RouterError:
        raise
    except OSError as e:  # connection refused, DNS, TLS, timeout
        raise RouterError(f"provider unreachable: {e}") from e
    try:
        content = data["choices"][0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as e:
        raise RouterError("provider response has no choices[0].message.content") from e
    if not isinstance(content, str) or not content.strip():
        raise RouterError("provider returned an empty completion")
    return content


def parse_spec(text: str) -> FeatureSpec:
    """Accept JSON with or without a markdown fence; anything else is a synthesis error."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise SynthesisError("completion contains no JSON object")
    try:
        raw: object = json.loads(body[start : end + 1])
    except json.JSONDecodeError as e:
        raise SynthesisError(f"completion is not valid JSON: {e.msg}") from e
    if not isinstance(raw, dict):
        raise SynthesisError("completion must be a JSON object")
    return from_dict(raw)


class LLMWriter:
    """Implements Writer over a chat model. Two repairs max, then the candidate is rejected."""

    def __init__(self, config: ModelConfig, post: Post | None = None,
                 retries: int = MAX_REPAIR_RETRIES) -> None:
        self._config = config
        self._post = post or http_post
        self._retries = retries

    def write(self, plan: Plan, *, repo: str, base_commit: str) -> FeatureSpec:
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": render_prompt(plan, repo=repo, base_commit=base_commit)},
        ]
        last = "no attempt made"
        for _ in range(self._retries + 1):
            reply = complete(self._config, messages, self._post)
            try:
                spec = parse_spec(reply)
                validate(spec)
                require_ok(spec)
                return spec
            except SynthesisError as e:
                last = str(e)
                messages += [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": f"Rejected: {last}\nReturn corrected JSON only."},
                ]
        raise SynthesisError(f"no valid spec after {self._retries + 1} attempts: {last}")


def http_post(url: str, headers: dict[str, str], payload: dict[str, object]) -> Mapping[str, object]:
    """Stdlib POST so the factory gains no SDK dependency; 120 s covers slow completions."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body: object = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RouterError(f"provider returned HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RouterError(f"provider request failed: {e}") from e
    if not isinstance(body, dict):
        raise RouterError("provider returned a non-object body")
    return body
