import asyncio
import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request
from cryptography.fernet import Fernet

from vibekeeper.app import Application, secure_headers
from vibekeeper.classifier import group_tracks
from vibekeeper.config import Config
from vibekeeper.models import Track
from vibekeeper.providers import LastFM, ProviderError, Spotify
from vibekeeper.service import Curator
from vibekeeper.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path, Fernet.generate_key().decode())
    yield instance
    instance.close()


@pytest.fixture
def config(tmp_path):
    return Config("123456:fake-token", "client", "secret", "https://example.org", Fernet.generate_key().decode(),
                  frozenset({1}), "lastfm", tmp_path)


def track(uri="spotify:track:1", tags=None):
    return Track(uri, "Track", ["Artist"], "https://open.spotify.com/track/1", tags or [])


def test_mood_requires_explicit_tags_and_deduplicates():
    rock = track(tags=["Rock"])
    groups = group_tracks([rock, rock, track("spotify:track:2", ["hip-hop", "sad"]), track("spotify:track:3")])
    assert len(groups["rock"]) == 1
    assert "energy" not in groups
    assert groups["hiphop"] == groups["melancholy"]
    assert len(groups["unknown"]) == 1


def test_token_encryption_and_disconnect(store):
    store.save_account(1, {"access_token": "secret-value"})
    stored = store.db.execute("SELECT token FROM accounts").fetchone()[0]
    assert b"secret-value" not in stored
    assert store.account(1)["access_token"] == "secret-value"
    draft = store.save_draft(1, {"groups": {}})
    nonce = store.issue_state(1, "verifier")
    store.reserve_export(1, draft, "rock")
    store.disconnect(1)
    assert store.account(1) is None
    assert store.draft(1, draft) is None
    assert store.state_owner(nonce) is None
    assert store.export(1, draft, "rock") is None


def test_state_one_use_expiry_and_replacement(store):
    first = store.issue_state(1, "first")
    second = store.issue_state(1, "second")
    assert store.consume_state(first) is None
    assert store.consume_state(second)["verifier"] == "second"
    assert store.consume_state(second) is None
    third = store.issue_state(1, "third")
    with store.db:
        store.db.execute("UPDATE oauth SET expires=0")
    assert store.consume_state(third) is None


def test_draft_ownership_and_expiry(store):
    draft = store.save_draft(1, {"groups": {"rock": []}})
    assert store.draft(2, draft) is None
    with store.db:
        store.db.execute("UPDATE drafts SET expires=0")
    assert store.draft(1, draft) is None


@pytest.mark.asyncio
async def test_export_idempotent_and_owned(store):
    provider = SimpleNamespace(create_playlist=AsyncMock(return_value={"id": "new"}), add_tracks=AsyncMock())
    curator = Curator(store, provider, None, 300)
    body = {"demo": False, "groups": {"rock": [asdict(track()), asdict(track())]}}
    draft = store.save_draft(1, body)
    with pytest.raises(ProviderError):
        await curator.export(2, draft, "rock")
    assert await curator.export(1, draft, "rock") == "new"
    assert await curator.export(1, draft, "rock") == "new"
    provider.create_playlist.assert_awaited_once()
    provider.add_tracks.assert_awaited_once_with(1, "new", ["spotify:track:1"])


@pytest.mark.asyncio
async def test_partial_export_is_not_repeated_after_restart(store):
    provider = SimpleNamespace(create_playlist=AsyncMock(return_value={"id": "partial"}),
                               add_tracks=AsyncMock(side_effect=ProviderError("timeout")))
    draft = store.save_draft(1, {"groups": {"rock": [asdict(track())]}})
    curator = Curator(store, provider, None, 300)
    with pytest.raises(ProviderError):
        await curator.export(1, draft, "rock")
    assert store.export(1, draft, "rock")["status"] == "uncertain"
    restarted = Curator(store, provider, None, 300)
    with pytest.raises(ProviderError, match="partial"):
        await restarted.export(1, draft, "rock")
    provider.create_playlist.assert_awaited_once()


@pytest.mark.asyncio
async def test_demo_never_writes(store):
    provider = SimpleNamespace(create_playlist=AsyncMock())
    draft = store.save_draft(1, {"demo": True, "groups": {"rock": [asdict(track())]}})
    with pytest.raises(ProviderError, match="демонстрация"):
        await Curator(store, provider, None, 300).export(1, draft, "rock")
    provider.create_playlist.assert_not_awaited()


@pytest.mark.asyncio
async def test_spotify_batches_writes(config, store):
    spotify = Spotify(config, store, None)
    spotify.request = AsyncMock()
    await spotify.add_tracks(1, "playlist", [f"spotify:track:{i}" for i in range(205)])
    assert [len(call.kwargs["json"]["uris"]) for call in spotify.request.await_args_list] == [100, 100, 5]
    assert all(call.args[2] == "/playlists/playlist/items" for call in spotify.request.await_args_list)


@pytest.mark.asyncio
async def test_saved_tracks_pagination_skips_unavailable(config, store):
    spotify = Spotify(config, store, None)
    saved = {"track": {"id": "1", "name": "One", "uri": "spotify:track:1", "artists": [{"name": "Artist"}]}}
    spotify.request = AsyncMock(side_effect=[
        {"total": 3, "items": [{"track": None}, saved], "next": "next"},
        {"total": 3, "items": [saved], "next": None},
    ])
    tracks, total = await spotify.liked_tracks(1, 300)
    assert len(tracks) == 1 and total == 3
    assert spotify.request.await_args_list[1].kwargs["params"]["offset"] == 2


class Response:
    def __init__(self, status, data=None, headers=None):
        self.status, self.data, self.headers = status, data, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.data


@pytest.mark.asyncio
async def test_refresh_preserves_refresh_token(config, store):
    store.save_account(1, {"access_token": "old", "refresh_token": "refresh", "expires_at": 0})
    session = SimpleNamespace(post=lambda *a, **kw: Response(200, {"access_token": "new", "expires_in": 3600}))
    spotify = Spotify(config, store, session)
    assert await spotify.access_token(1) == "new"
    assert store.account(1)["refresh_token"] == "refresh"


@pytest.mark.asyncio
async def test_ambiguous_network_write_is_never_retried(config, store):
    store.save_account(1, {"access_token": "token", "expires_at": time.time()+3600})
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        raise asyncio.TimeoutError()

    spotify = Spotify(config, store, SimpleNamespace(request=request))
    with pytest.raises(ProviderError):
        await spotify.create_playlist(1, "Рок")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_oauth_consumes_state_and_rejects_replay(config, store):
    bot = SimpleNamespace(send_message=AsyncMock())
    app = Application(config, store, None, bot)
    app.spotify.exchange = AsyncMock(return_value={"access_token": "token"})
    state = store.issue_state(1, "verifier")
    request = make_mocked_request("GET", f"/oauth/spotify?state={state}&code=secret-code")
    response = await app.oauth(request)
    assert response.status == 200
    assert store.account(1)["access_token"] == "token"
    assert (await app.oauth(request)).status == 400
    app.spotify.exchange.assert_awaited_once_with("secret-code", "verifier")


@pytest.mark.asyncio
async def test_unauthorized_user_and_group_cannot_connect(config, store):
    app = Application(config, store, None, None)
    reply = AsyncMock()
    await app.message(SimpleNamespace(chat=SimpleNamespace(type="private"), from_user=SimpleNamespace(id=2),
                                      text="/connect", answer=reply))
    assert "тестировщикам" in reply.await_args.args[0]
    assert store.db.execute("SELECT count(*) FROM oauth").fetchone()[0] == 0
    reply.reset_mock()
    await app.message(SimpleNamespace(chat=SimpleNamespace(type="group"), from_user=SimpleNamespace(id=1),
                                      text="/connect", answer=reply))
    reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_demo_ui_and_callback_preview(config, store):
    app = Application(config, store, None, None)
    reply = AsyncMock()
    await app.demo(1, reply)
    markup = reply.await_args.kwargs["reply_markup"]
    data = markup.inline_keyboard[0][0].callback_data
    query = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(type="private"), answer=reply),
                            from_user=SimpleNamespace(id=1), data=data, answer=AsyncMock())
    await app.callback(query)
    assert "Демо" in reply.await_args.args[0]
    assert all(not b.callback_data.startswith("export:") for row in reply.await_args.kwargs["reply_markup"].inline_keyboard for b in row)


@pytest.mark.asyncio
async def test_oauth_security_headers():
    from aiohttp import web
    response = await secure_headers(make_mocked_request("GET", "/"), AsyncMock(return_value=web.Response(text="ok")))
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_lastfm_missing_track_is_not_service_failure():
    tagger = LastFM("key", SimpleNamespace(get=lambda *a, **kw: Response(200, {"error": 6})))
    assert await tagger.tags("Artist", "Unknown") == ([], "")
    tagger = LastFM("key", SimpleNamespace(get=lambda *a, **kw: Response(200, {"error": 10})))
    with pytest.raises(ProviderError):
        await tagger.tags("Artist", "Unknown")


@pytest.mark.asyncio
async def test_lastfm_filters_weak_tags_and_encodes_link():
    tagger = LastFM("key", SimpleNamespace(get=lambda *a, **kw: Response(200, {
        "toptags": {"tag": [{"name": "rock", "count": "100"}, {"name": "energetic", "count": "1"}]}
    })))
    tags, url = await tagger.tags("A/B & C", "Track")
    assert tags == ["rock"]
    assert "A%2FB%20%26%20C" in url


@pytest.mark.asyncio
async def test_end_to_end_scan_and_export_with_fake_apis(config, store):
    provider = SimpleNamespace(liked_tracks=AsyncMock(return_value=([track()], 1)),
                               create_playlist=AsyncMock(return_value={"id": "result"}), add_tracks=AsyncMock())
    session = SimpleNamespace(get=lambda *a, **kw: Response(200, {
        "toptags": {"tag": [{"name": "chillout", "count": 100}]}
    }))
    curator = Curator(store, provider, LastFM("key", session), 300)
    draft, body = await curator.scan(1)
    assert set(body["groups"]) == {"chill"}
    assert await curator.export(1, draft, "chill") == "result"


def test_purge_removes_expired_user_data(store):
    draft = store.save_draft(1, {"groups": {}})
    store.reserve_export(1, draft, "rock")
    with store.db:
        store.db.execute("UPDATE drafts SET expires=0")
    store.purge_expired()
    assert store.export(1, draft, "rock") is None
