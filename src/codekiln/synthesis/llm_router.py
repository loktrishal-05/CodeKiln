"""OpenAI-compatible chat router: prompt -> FeatureSpec, with bounded repair and no self-grading.

Host-only by design: the API key is read from settings and never passed to a container. The
router only proposes a spec; `validate`, the RTM audit and the execution gate decide whether it
is admissible, so a confident-sounding model cannot mark its own work correct.
"""
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from codekiln.planner.models import Plan
from codekiln.states import MAX_REPAIR_RETRIES
from codekiln.synthesis.edits import build_diff
from codekiln.synthesis.models import FeatureSpec, SynthesisError, from_dict, validate
from codekiln.synthesis.rtm import require_ok
from codekiln.synthesis.spec_generator import FORMAT_RULES, render_prompt

Post = Callable[[str, dict[str, str], dict[str, object]], Mapping[str, object]]

SYSTEM = """\
You are CodeKiln's task synthesizer. You design ONE new feature and return JSON only, with no
markdown fences and no commentary. Never invent test results. The feature must not exist in the
base commit, and no upstream behaviour may be removed to make room for it.
"""


USER_AGENT = "codekiln/0.1 (+https://github.com/loktrishal-05/CodeKiln)"


class RouterError(RuntimeError):
    """Transport or provider failure (missing key, HTTP error, malformed response)."""


class Writer(Protocol):
    """Anything the pipeline can call to turn one plan slot into one spec."""

    def write(
        self, plan: Plan, *, repo: str, base_commit: str,
        previous: FeatureSpec | None = None, feedback: str = "",
    ) -> FeatureSpec: ...


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    api_key: str = field(repr=False)  # never shows up in a traceback or log line
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


def parse_spec(text: str, workspace: Path | None = None) -> FeatureSpec:
    """Accept JSON with or without a markdown fence; anything else is a synthesis error.

    A reply may carry `edits` (where new code goes) instead of a `reference_diff`: with a WORKSPACE
    the diff is computed from the real files, so it applies by construction."""
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
    if "edits" in raw and "reference_diff" not in raw:
        if workspace is None:
            raise SynthesisError("edits need the repo checkout; none is available, send reference_diff")
        raw["reference_diff"] = build_diff(workspace, raw.pop("edits"))
    return from_dict(raw)


class LLMWriter:
    """Implements Writer over a chat model. Two repairs max, then the candidate is rejected."""

    def __init__(self, config: ModelConfig, post: Post | None = None,
                 retries: int = MAX_REPAIR_RETRIES) -> None:
        self._config = config
        self._post = post or http_post
        self._retries = retries

    def write(
        self, plan: Plan, *, repo: str, base_commit: str,
        previous: FeatureSpec | None = None, feedback: str = "",
    ) -> FeatureSpec:
        """A new spec for PLAN; with PREVIOUS and FEEDBACK, a corrected one: the previous attempt plus
        what really happened when it was executed (real pytest output, surviving mutants)."""
        base = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": render_prompt(plan, repo=repo, base_commit=base_commit)},
        ]
        if previous is not None:
            base += [
                {"role": "assistant", "content": json.dumps(previous.to_dict())},
                {"role": "user", "content": (
                    "That attempt was EXECUTED and failed. What actually happened:\n" + feedback
                    + "\n\nFix the cause and return the complete corrected spec as JSON, using `edits` as "
                    "before (not reference_diff). Change only what the failure requires.\n" + FORMAT_RULES
                )},
            ]
        messages = base
        last = "no attempt made"
        for _ in range(self._retries + 1):
            reply = complete(self._config, messages, self._post)
            try:
                spec = parse_spec(reply, Path(plan.workspace) if plan.workspace else None)
                validate(spec)
                require_ok(spec)
                return spec
            except SynthesisError as e:
                last = str(e)
                # Each repair carries the original prompt plus only the LATEST failed reply and why
                # it failed. Appending every attempt grew the request each time until a provider with
                # a per-request token cap (Groq free tier: 8000) refused it with HTTP 413.
                messages = [
                    *base,
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": f"Rejected: {last}\n{FORMAT_RULES}\nReturn corrected JSON only."},
                ]
        raise SynthesisError(f"no valid spec after {self._retries + 1} attempts: {last}")


def http_post(url: str, headers: dict[str, str], payload: dict[str, object]) -> Mapping[str, object]:
    """Stdlib POST so the factory gains no SDK dependency; 120 s covers slow completions.

    Sends an explicit User-Agent: urllib's default (`Python-urllib/x.y`) is rejected with a 403
    (Cloudflare error 1010) by providers such as Groq, found on the first real run.
    """
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            url, data=data, headers={"User-Agent": USER_AGENT, **headers}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body: object = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            message = _provider_message(e)
            if e.code in _RETRYABLE and attempt < MAX_ATTEMPTS:
                time.sleep(min(_retry_after(e, message), MAX_WAIT_S))  # wait out the rate-limit window
                continue
            raise RouterError(f"provider returned HTTP {e.code}: {message}" if message
                              else f"provider returned HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise RouterError(f"provider request failed: {e}") from e
        except ValueError as e:  # 200 with a non-JSON or non-UTF-8 body (proxy, HTML error page)
            raise RouterError("provider returned a non-JSON body") from e
        if not isinstance(body, dict):
            raise RouterError("provider returned a non-object body")
        return body
    raise RouterError("provider kept rate-limiting the request")  # unreachable: the loop returns or raises


_RETRYABLE = {429, 502, 503, 504}  # rate limit / transient: the same request may succeed shortly
MAX_ATTEMPTS = 4
MAX_WAIT_S = 70.0  # one window of a per-minute limit; never sleep longer than this per attempt
_WAIT = re.compile(r"try again in (?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?")
_SECRET = re.compile(r"\b(?:gsk|sk)[-_][A-Za-z0-9_-]{6,}")


def _provider_message(e: urllib.error.HTTPError) -> str:
    """The provider's own error text (it says which limit was hit), with anything key-like removed."""
    try:
        raw = e.read().decode("utf-8", "replace")
    except OSError:
        return ""
    try:
        parsed = json.loads(raw)
        text = str(parsed.get("error", {}).get("message", "")) if isinstance(parsed, dict) else raw
    except ValueError:
        text = raw
    return _SECRET.sub("<redacted>", " ".join(text.split()))[:240]


def _retry_after(e: urllib.error.HTTPError, message: str) -> float:
    """Seconds to wait: the Retry-After header, else the message's 'try again in 1m3.5s', else 20."""
    try:
        return float(e.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        pass
    m = _WAIT.search(message)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + float(m.group(2) or 0) + 0.5
    return 20.0
