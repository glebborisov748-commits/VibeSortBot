import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import quote, urlencode

import aiohttp

from .models import Track


class ProviderError(Exception):
    """Only safe, user-facing messages; never include request URLs or tokens."""


class Spotify:
    API = "https://api.spotify.com/v1"
    TOKEN = "https://accounts.spotify.com/api/token"

    def __init__(self, config, store, session):
        self.config, self.store, self.session = config, store, session

    def login_url(self, uid):
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = self.store.issue_state(uid, verifier)
        return "https://accounts.spotify.com/authorize?" + urlencode({
            "client_id": self.config.client_id, "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "scope": "user-library-read playlist-modify-private",
            "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
        })

    async def _token(self, data):
        credentials = base64.b64encode(f"{self.config.client_id}:{self.config.client_secret}".encode()).decode()
        try:
            async with self.session.post(self.TOKEN, data=data,
                    headers={"Authorization": "Basic " + credentials}) as response:
                if response.status != 200:
                    raise ProviderError("Spotify не подтвердил доступ. Подключи аккаунт заново: /connect")
                token = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ProviderError("Spotify недоступен. Попробуй позже.") from None
        token["expires_at"] = time.time() + token["expires_in"]
        return token

    async def exchange(self, code, verifier):
        return await self._token({"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": self.config.redirect_uri, "code_verifier": verifier})

    async def access_token(self, uid):
        token = self.store.account(uid)
        if not token:
            raise ProviderError("Сначала подключи Spotify: /connect")
        if token["expires_at"] < time.time() + 30:
            fresh = await self._token({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
            token.update(fresh)
            self.store.save_account(uid, token)
        return token["access_token"]

    async def request(self, uid, method, path, **kwargs):
        for attempt in range(3):
            token = await self.access_token(uid)
            try:
                async with self.session.request(method, self.API + path,
                        headers={"Authorization": "Bearer " + token}, **kwargs) as response:
                    if response.status == 401 and attempt == 0:
                        saved = self.store.account(uid)
                        saved["expires_at"] = 0
                        self.store.save_account(uid, saved)
                        continue
                    if response.status == 429:
                        try:
                            delay = max(1, float(response.headers.get("Retry-After", "2")))
                        except ValueError:
                            delay = 2
                        if delay > 15 or attempt == 2:
                            raise ProviderError("Spotify ограничил частоту запросов. Попробуй позже.")
                        await asyncio.sleep(delay)
                        continue
                    if response.status == 403:
                        raise ProviderError("Spotify отказал в доступе. Проверь Premium владельца приложения, список тестировщиков и разрешения /connect.")
                    if response.status >= 400:
                        raise ProviderError(f"Spotify вернул ошибку {response.status}. Попробуй позже или переподключи аккаунт.")
                    return await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # Never retry writes: the server may already have applied them.
                raise ProviderError("Связь со Spotify прервалась. Результат записи мог сохраниться; проверь Spotify.") from None
        raise ProviderError("Не удалось выполнить запрос Spotify.")

    async def liked_tracks(self, uid, limit):
        result, seen, offset, total = [], set(), 0, 0
        while offset < limit:
            page = await self.request(uid, "GET", "/me/tracks", params={"limit": min(50, limit-offset), "offset": offset})
            total = page.get("total", 0)
            items = page.get("items", [])
            for item in items:
                track = item.get("track")
                if not track or not track.get("id") or track.get("is_local") or track.get("is_playable") is False:
                    continue
                uri = track.get("uri", "")
                if not uri.startswith("spotify:track:") or uri in seen:
                    continue
                seen.add(uri)
                result.append(Track(uri, track["name"], [a["name"] for a in track.get("artists", [])],
                                    "https://open.spotify.com/track/" + track["id"]))
            offset += len(items)
            if not items or not page.get("next"):
                break
        return result, total

    async def create_playlist(self, uid, name):
        return await self.request(uid, "POST", "/me/playlists", json={
            "name": "VibeSort · " + name, "public": False,
            "description": "Из моих любимых треков. Подборка по тегам Last.fm; настроение приблизительное.",
        })

    async def add_tracks(self, uid, playlist_id, uris):
        for start in range(0, len(uris), 100):
            await self.request(uid, "POST", f"/playlists/{playlist_id}/items", json={"uris": uris[start:start+100]})


class LastFM:
    def __init__(self, key, session):
        self.key, self.session = key, session
        self.gate = asyncio.Lock()
        self.next_request = 0.0

    async def enrich(self, tracks):
        if not self.key:
            raise ProviderError("Разбор пока настраивается владельцем бота. Сейчас можно посмотреть пример: /demo.")
        # Per-scan cache only, no cross-user retention of a listening library.
        cache = {}
        for track in tracks:
            if not track.artists:
                continue
            lookup = (track.artists[0], track.title)
            if lookup not in cache:
                cache[lookup] = await self.tags(*lookup)
            track.tags, track.tag_url = cache[lookup]

    async def tags(self, artist, title):
        async with self.gate:
            await asyncio.sleep(max(0, self.next_request-time.monotonic()))
            self.next_request = time.monotonic() + 0.3
            try:
                async with self.session.get("https://ws.audioscrobbler.com/2.0/", params={
                    "method": "track.getTopTags", "artist": artist, "track": title,
                    "api_key": self.key, "format": "json", "autocorrect": "1",
                }) as response:
                    if response.status != 200:
                        raise ProviderError("Last.fm временно недоступен. Разбор не сохранён, попробуй позже.")
                    data = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise ProviderError("Нет связи с Last.fm. Попробуй позже.") from None
        if data.get("error") == 6:  # Not found is a valid unclassified track.
            return [], ""
        if data.get("error"):
            raise ProviderError("Last.fm отклонил запрос. Проверь API-ключ или попробуй позже.")
        tags = data.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):
            tags = [tags]
        # Ignore weak tags, cap at ten; no guessing from title or genre alone.
        names = [tag["name"] for tag in tags[:10] if int(tag.get("count", 0)) >= 20]
        url = "https://www.last.fm/music/" + quote(artist, safe="")
        return names, url
