# Integer range prompt

## Requirements
- REQ-1: `click.termui.prompt_int_range(text: str, min: int | None = None, max: int | None = None, default: int | None = None, hide_input: bool = False, err: bool = False) -> int` prompts the user for an integer and returns it.
- REQ-2: If `min` is greater than `max` a `ValueError` is raised before prompting.
- REQ-3: If the entered value is lower than `min` a `ValueError` is raised.

## Errors
- `ValueError` when bounds are inconsistent or the result is out of range.
