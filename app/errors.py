from __future__ import annotations


class PermanentProcessingError(RuntimeError):
    """An input/document validation error that will not improve on queue retry."""


class GraphAccessDeniedError(RuntimeError):
    """Microsoft Graph denied an operation with the current app permissions."""
