"""Tests must not depend on a developer's private settings (keys, pricing, URLs in .env or the shell)."""
import os

import pytest

from codekiln.config import Settings


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)  # never read a local .env
    for name in [n for n in os.environ if n.startswith("CODEKILN_")]:
        monkeypatch.delenv(name)  # tests that need a setting set it themselves
