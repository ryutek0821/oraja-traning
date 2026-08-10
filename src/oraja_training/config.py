"""Application configuration shared by the command-line entry points."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime paths and collector settings.

    ``beatoraja_dir`` is the directory containing ``player/<name>``.  A caller
    that already knows the player directory can pass it directly to
    :attr:`player_dir` consumers instead.
    """

    beatoraja_dir: Path
    player_name: str
    assistant_db: Path = Path("assistant.db")
    port: int = 8765
    poll_interval: float = 5.0

    @property
    def player_dir(self) -> Path:
        return self.beatoraja_dir / "player" / self.player_name

