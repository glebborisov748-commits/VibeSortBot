import asyncio
import json
import re
from dataclasses import asdict
from urllib.parse import urlparse

import aiohttp

from .models import Track
from .providers import ProviderError


def playlist_id(url):
    parsed = urlparse(url.strip())
    match = re.fullmatch(r"/(?:intl-[a-z]{2}/)?playlist/([A-Za-z0-9]{22})/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "open.spotify.com" or not match:
        raise ProviderError("Нужна ссылка https://open.spotify.com/playlist/… на публичный плейлист. Личные «Любимые» могут быть недоступны.")
    return match.group(1)


def parse_embed(html, expected_id):
    match = re.search(r'<script\b[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise ProviderError("Spotify не отдал открытый список треков. Можно прислать скриншоты.")
    try:
        page = json.loads(match.group(1))["props"]["pageProps"]
        entity = page.get("state", {}).get("data", {}).get("entity", {})
        if page.get("status") == 404 or entity.get("id") != expected_id or entity.get("type") != "playlist":
            raise ProviderError("Spotify не отдал список с сервера VibeSort. Это не доказывает, что плейлист закрыт. Можно повторить загрузку или прислать скриншоты.")
        tracks, seen = [], set()
        for item in entity.get("trackList", [])[:300]:
            uri, title, artists = item.get("uri", ""), item.get("title"), item.get("subtitle")
            if not re.fullmatch(r"spotify:track:[A-Za-z0-9]{22}", uri) or uri in seen:
                continue
            if not isinstance(title, str) or not isinstance(artists, str) or not title.strip() or not artists.strip():
                continue
            seen.add(uri)
            tracks.append(Track(uri, " ".join(title.split())[:200], [x.strip() for x in artists.split(",") if x.strip()],
                                "https://open.spotify.com/track/" + uri.rsplit(":", 1)[1]))
        if not tracks:
            raise ProviderError("Spotify не показал песни в этом плейлисте. Пришли скриншоты или список текстом.")
        return {"title": str(entity.get("name") or "Плейлист Spotify")[:200], "tracks": [asdict(t) for t in tracks],
                "url": "https://open.spotify.com/playlist/" + expected_id,
                "text": "\n".join(", ".join(t.artists) + " — " + t.title for t in tracks)}
    except ProviderError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ProviderError("Формат страницы Spotify изменился. Пришли скриншот или список текстом.") from None


async def fetch_playlist(session, url, store=None):
    identity = playlist_id(url)
    last_error = None
    for attempt in range(2):
      try:
        # Fixed host/path only. Do not forward user query parameters or follow redirects.
        suffix = "?utm_source=generator" if attempt else ""
        async with session.get("https://open.spotify.com/embed/playlist/" + identity + suffix,
                               allow_redirects=False, timeout=aiohttp.ClientTimeout(total=12)) as response:
            if response.status != 200:
                raise ProviderError(f"Spotify не открыл публичный плейлист (HTTP {response.status}). Пришли скриншот или попробуй позже.")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise ProviderError("Ответ Spotify слишком большой. Используй список или скриншоты.")
        imported = parse_embed(body.decode("utf-8"), identity)
        if store:
            store.cache_playlist(identity, imported)
        return imported
      except (ProviderError, aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as error:
        last_error = error
        if attempt == 0:
            await asyncio.sleep(0.5)
    cached = store.cache_playlist(identity) if store else None
    if cached:
        return {**cached, "cached": True}
    if isinstance(last_error, ProviderError):
        raise last_error
    raise ProviderError("Не удалось получить страницу Spotify с сервера. Повтори позже или загрузи скриншоты.") from None
