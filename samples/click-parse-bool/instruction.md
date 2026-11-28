# Boolean parsing helper

## Requirements
- REQ-1: `click.utils.parse_bool(value: str | bool | None, *, default: bool = False) -> bool` returns a boolean for common truthy/falsy strings.
- REQ-2: When `value` is ``None`` the function returns the provided ``default``.
- REQ-3: When `value` is already a ``bool`` it is returned unchanged.
- REQ-4: When `value` is a string that does not match any known truthy or falsy representation a ``ValueError`` is raised.
- REQ-5: When `value` is of any other type a ``ValueError`` is raised.

## Errors
- ``ValueError`` if the string cannot be interpreted as a boolean or if the type is unsupported.
