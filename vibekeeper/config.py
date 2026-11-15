import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Config:
    bot_token: str
    client_id: str
    client_secret: str
    base_url: str
    encryption_key: str
    allowed_ids: frozenset[int]
    lastfm_key: str
    data_dir: Path
    port: int = 3000
    scan_limit: int = 1000

    @classmethod
    def from_env(cls):
        required = ("BOT_TOKEN",
                    "PUBLIC_BASE_URL", "TOKEN_ENCRYPTION_KEY")
        missing = [key for key in required if not os.getenv(key, "").strip()]
        if missing:
            raise ValueError("Заполните переменные: " + ", ".join(missing))
        base = os.environ["PUBLIC_BASE_URL"].strip().rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("PUBLIC_BASE_URL должен быть HTTPS-адресом без пути")
        ids = frozenset(int(x.strip()) for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",") if x.strip())
        limit = int(os.getenv("SCAN_LIMIT", "1000"))
        if not 1 <= limit <= 10000:
            raise ValueError("SCAN_LIMIT должен быть от 1 до 10000")
        return cls(os.environ["BOT_TOKEN"], os.getenv("SPOTIFY_CLIENT_ID", ""),
                   os.getenv("SPOTIFY_CLIENT_SECRET", ""), base,
                   os.environ["TOKEN_ENCRYPTION_KEY"], ids, os.getenv("LASTFM_API_KEY", ""),
                   Path(os.getenv("DATA_DIR", "./data")), int(os.getenv("PORT", "3000")), limit)

    @property
    def redirect_uri(self):
        return self.base_url + "/oauth/spotify"
