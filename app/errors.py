from __future__ import annotations


class PermanentProcessingError(RuntimeError):
    """An input/document validation error that will not improve on queue retry."""
