from __future__ import annotations

from pathlib import Path

import pytest

from oraja_training.serve import create_progress_token


def test_progress_token_is_random_and_never_overwritten(tmp_path: Path) -> None:
    target = create_progress_token(tmp_path / "private" / "token.txt")
    value = target.read_text(encoding="utf-8").strip()
    assert len(value) == 64
    assert all(character in "0123456789abcdef" for character in value)
    with pytest.raises(FileExistsError):
        create_progress_token(target)
    assert target.read_text(encoding="utf-8").strip() == value
