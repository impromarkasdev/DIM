from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable


def zip_files(files: Iterable[tuple[str, bytes]]) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for filename, content in files:
            output.writestr(filename, content)
    return archive.getvalue()
