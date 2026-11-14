import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from test_core import config, store
from vibekeeper.app import Application
from vibekeeper.miniapp import install_miniapp, telegram_user
from vibekeeper.vision import recognize_tracks
from vibekeeper.providers import ProviderError


def signed(token, uid=1, date=None):
    values = {'auth_date':str(int(time.time()) if date is None else date), 'user':json.dumps({'id':uid}), 'query_id':'test'}
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    check='\n'.join(f'{k}={values[k]}' for k in sorted(values))
    values['hash']=hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_telegram_signature_rejects_tampering_and_expired_session():
    token='test-token'
    assert telegram_user(signed(token), token)==1
    with pytest.raises(ValueError):
        telegram_user(signed(token)+'&auth_date=0', token)
    with pytest.raises(ValueError):
        telegram_user(signed(token, date=1), token)
    with pytest.raises(ValueError):
        telegram_user(signed(token), 'different-token')


@pytest.mark.asyncio
async def test_api_job_is_private_and_runs_without_spotify(config, store):
    application=Application(config, store, None, None)
    application.curator.tagger.enrich=AsyncMock(side_effect=lambda tracks: tracks[0].tags.append('rock'))
    server=web.Application()
    install_miniapp(server, application, None)
    async with TestClient(TestServer(server)) as client:
        r=await client.post('/api/analyze', json={'text':'Radiohead — No Surprises'})
        assert r.status==401
        headers={'X-Telegram-Init-Data':signed(config.bot_token)}
        r=await client.post('/api/analyze', headers=headers, json={'text':'Radiohead — No Surprises'})
        assert r.status==200
        job=(await r.json())['job']
        r=await client.get('/api/jobs/'+job, headers=headers)
        data=await r.json()
        assert data['status']=='ready'
        assert 'rock' in data['body']['groups']
        assert 'uid' not in data
        application.config=SimpleNamespace(bot_token=config.bot_token, allowed_ids=frozenset())
        r=await client.get('/api/jobs/'+job, headers={'X-Telegram-Init-Data':signed(config.bot_token, 2)})
        assert r.status==404


@pytest.mark.asyncio
async def test_invalid_image_never_calls_ai():
    with pytest.raises(ProviderError, match='PNG'):
        await recognize_tracks(None, b'not an image')


@pytest.mark.asyncio
async def test_image_recognition_returns_editable_text(monkeypatch):
    monkeypatch.setenv('PROVOD_API_KEY', 'test-only-key')
    class Result:
        status=200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self):
            return {'choices':[{'message':{'content':'```json\n{"tracks":[{"artist":"Radiohead","title":"No Surprises"}]}\n```'}}]}
    def post(url, **kwargs):
        assert url=='https://api.provod.ai/v1/chat/completions'
        assert kwargs['headers']['Authorization']=='Bearer test-only-key'
        assert kwargs['json']['messages'][1]['content'][0]['image_url']['url'].startswith('data:image/png;base64,')
        return Result()
    text=await recognize_tracks(SimpleNamespace(post=post), b'\x89PNG\r\n\x1a\ntest')
    assert text=='Radiohead — No Surprises'


@pytest.mark.asyncio
async def test_lastfm_failure_falls_back_to_ai_and_labels_estimates(store, monkeypatch):
    from vibekeeper.imports import classify_import, parse_text_tracks
    monkeypatch.setenv('PROVOD_API_KEY', 'test-only-key')
    class Result:
        status=200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self):
            return {'choices':[{'message':{'content':'{"tracks":[{"id":0,"tags":["electronic","dark"]},{"id":1,"tags":[]}]}'}}]}
    tagger=SimpleNamespace(enrich=AsyncMock(side_effect=ProviderError('Last.fm: HTTP 403.')),
                           session=SimpleNamespace(post=lambda *a, **k:Result()))
    tracks=parse_text_tracks('PHNKR — PANDORA\nUnknown — Not Known')
    draft, body=await classify_import(1, tracks, store, tagger)
    assert body['ai_estimated'] is True
    assert body['tag_warning']=='Last.fm: HTTP 403.'
    assert set(body['groups'])=={'electronic','dark','unknown'}
    assert body['groups']['dark'][0]['tag_source']=='ai'
    assert store.draft(1,draft)['groups']==body['groups']


@pytest.mark.asyncio
async def test_ai_fallback_cannot_replace_titles(monkeypatch):
    from vibekeeper.vision import infer_tags
    from vibekeeper.models import Track
    monkeypatch.setenv('PROVOD_API_KEY','test-key')
    class Result:
        status=200
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
        async def json(self):
            return {'choices':[{'message':{'content':'{"tracks":[{"id":0,"title":"wrong","tags":["made up","rock"]}]}'}}]}
    track=Track('import:0','Original',['Artist'],'')
    await infer_tags(SimpleNamespace(post=lambda *a,**k:Result()),[track])
    assert track.title=='Original' and track.tags==['rock']
