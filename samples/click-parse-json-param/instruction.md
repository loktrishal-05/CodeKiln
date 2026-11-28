# JSON parameter parser

## Requirements
- REQ-1: `click.core.parse_json_param(value: str) -> Any` returns the parsed Python object for a valid JSON string.
- REQ-2: `parse_json_param` returns ``None`` when the input string is empty.
- REQ-3: `parse_json_param` raises :class:`click.exceptions.BadParameter` for invalid JSON.

## Errors
- Raises ``BadParameter`` with a clear message when JSON decoding fails.
