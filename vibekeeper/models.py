from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Track:
    uri: str
    title: str
    artists: list[str]
    url: str
    tags: list[str] = field(default_factory=list)
    tag_url: str = ""
    tag_source: str = ""


class MusicProvider(Protocol):
    """Boundary for future Yandex/Apple adapters; only Spotify is implemented."""

    async def liked_tracks(self, user_id: int, limit: int) -> tuple[list[Track], int]: ...
    async def create_playlist(self, user_id: int, name: str) -> dict: ...
    async def add_tracks(self, user_id: int, playlist_id: str, uris: list[str]) -> None: ...
