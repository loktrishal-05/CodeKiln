# Completion enable flag

Add a public helper that determines whether shell completion is enabled based on the environment variable ``CLICK_COMPLETION_DISABLED``.

## Requirements
- REQ-1: ``click.shell_completion.completion_enabled()`` returns ``True`` when the environment variable is not set.
- REQ-2: ``completion_enabled()`` returns ``False`` when the variable is set to a falsey value (``"0"``, ``"false"``, case‑insensitive).
- REQ-3: ``completion_enabled()`` returns ``True`` when the variable is set to a truthy value (``"1"``, ``"true"``, case‑insensitive).
- REQ-4: ``completion_enabled()`` raises ``ValueError`` for any other value.

## Errors
- ``ValueError`` is raised when the environment variable contains an unrecognised value.
