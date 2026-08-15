"""Create progress receiver credentials without printing secret material."""

from __future__ import annotations

import os
from pathlib import Path
import secrets


def create_progress_token(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(secrets.token_hex(32) + "\n")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target
