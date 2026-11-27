"""Jinja2 template loader for Dockerfiles and Harbor task manifests."""
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATE_DIR = Path(__file__).parent
_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=False,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_template(name: str, **context: Any) -> str:
    """Render a template relative to src/codekiln/templates/ with strict undefined checks."""
    return _ENV.get_template(name).render(**context)
