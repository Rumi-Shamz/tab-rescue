"""Safe, read-only access to browser files: always work on a temp copy."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def temp_copy(*sources: Path | str) -> Iterator[list[Path | None]]:
    """Copy each existing source file into a fresh temp dir and yield the copies.

    Missing sources yield ``None``. The browser's own files are only ever
    opened for reading by ``shutil.copyfile``.
    """
    with tempfile.TemporaryDirectory(prefix="tab-rescue-") as tmp:
        out: list[Path | None] = []
        for src in sources:
            src = Path(src)
            if src.is_file():
                dst = Path(tmp) / src.name
                shutil.copyfile(src, dst)
                out.append(dst)
            else:
                out.append(None)
        yield out
