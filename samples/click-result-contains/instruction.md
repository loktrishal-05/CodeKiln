# Result output containment helper

## Requirements
- REQ-1: `Result.output_contains(substring: str | t.Sequence[str], case_sensitive: bool = True) -> bool` returns ``True`` when ``substring`` (or any element of the sequence) is found in the captured output according to the ``case_sensitive`` flag, otherwise ``False``.
- REQ-2: ``Result.output_contains`` raises ``TypeError`` if ``substring`` is not a ``str`` nor a sequence of strings.
- REQ-3: ``Result.output_contains`` raises ``TypeError`` if ``case_sensitive`` is not a ``bool``.
- REQ-4: When ``substring`` is a sequence, the method returns ``True`` if **any** element matches the output criteria.

## Errors
- ``TypeError`` for invalid argument types.
