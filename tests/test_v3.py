import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from test_core import config, store
from test_miniapp import signed
from vibekeeper.app import Application
from vibekeeper.miniapp import install_miniapp
from vibekeeper.store import Store
from vibekeeper.classifier import group_tracks
from vibekeeper.models import Track
from vibekeeper.providers import LastFM


def test_library_survives_restart_and_is_private(tmp_path):
    key=Fernet.generate_key().decode()
    db=Store(tmp_path,key)
    item=db.save_library(1,{'name':'Фонк','scanned':2,'groups':{'phonk':[]}})
    db.settings(1,{'ai_fallback':False})
    db.close()
    db=Store(tmp_path,key)
    assert db.library(1,item)['name']=='Фонк'
    assert db.library(2,item) is None
    assert db.update_library(2,item,{'name':'forged'}) is False
    assert db.settings(1)['ai_fallback'] is False
    db.disconnect(1)
    assert db.library(1)==[]
    assert db.settings(1)['ai_fallback'] is True
    db.close()


def test_phonk_funk_are_distinct_categories():
    groups=group_tracks([Track('a','One',['Artist'],'',['drift phonk']),Track('b','Two',['Artist'],'',['baile funk'])])
    assert len(groups['phonk'])==1 and len(groups['funk'])==1
    assert 'unknown' not in groups


@pytest.mark.asyncio
async def test_ocr_returns_job_before_provider_finishes(config,store,monkeypatch):
    gate=asyncio.Event()
    async def recognize(*args):
        await gate.wait()
        return 'Artist — Track'
    monkeypatch.setattr('vibekeeper.miniapp.recognize_tracks',recognize)
    app=Application(config,store,None,None)
    server=web.Application()
    install_miniapp(server,app,None)
    headers={'X-Telegram-Init-Data':signed(config.bot_token)}
    async with TestClient(TestServer(server)) as client:
        response=await client.post('/api/ocr',headers=headers,data=b'fake-photo')
        job=(await response.json())['job']
        assert (await (await client.get('/api/jobs/'+job,headers=headers)).json())['status']=='running'
        gate.set()
        for _ in range(10):
            result=await (await client.get('/api/jobs/'+job,headers=headers)).json()
            if result['status']=='ready':break
        assert result['text']=='Artist — Track'


@pytest.mark.asyncio
async def test_library_genre_settings_and_user_isolation(config,store):
    app=Application(config,store,None,None)
    server=web.Application()
    install_miniapp(server,app,None)
    item=store.save_library(1,{'scanned':1,'groups':{'unknown':[{'uri':'import:0','title':'Test','artists':['Artist'],'url':'','tags':[],'tag_url':'','tag_source':''}]}})
    headers={'X-Telegram-Init-Data':signed(config.bot_token)}
    async with TestClient(TestServer(server)) as client:
        response=await client.get('/api/library',headers=headers)
        assert (await response.json())['items'][0]['id']==item
        response=await client.post('/api/library/'+item+'/genre',headers=headers,json={'genre_hint':'phonk'})
        assert 'phonk' in (await response.json())['body']['groups']
        assert store.library(1,item)['groups']['phonk'][0]['tag_source']=='user'
        response=await client.put('/api/settings',headers=headers,json={'ai_fallback':False})
        assert (await response.json())['ai_fallback'] is False
        app.config=SimpleNamespace(bot_token=config.bot_token,allowed_ids=frozenset())
        other={'X-Telegram-Init-Data':signed(config.bot_token,2)}
        assert (await client.delete('/api/library/'+item,headers=other)).status==404
        assert store.library(1,item) is not None
        await client.delete('/api/data',headers=headers)
        assert store.library(1)==[]


@pytest.mark.asyncio
async def test_artist_fallback_only_imports_genre_not_mood():
    tagger=LastFM('key',None)
    tagger.tags=AsyncMock(side_effect=[([],''),(['phonk','sad'],'https://www.last.fm/music/Artist')])
    tracks=[Track('a','New edit',['Artist'],'')]
    await tagger.enrich(tracks)
    assert tracks[0].tags==['phonk']
    assert tracks[0].tag_source=='lastfm'


@pytest.mark.asyncio
async def test_explicit_genre_works_without_remote_services(store):
    from vibekeeper.imports import classify_import
    tagger=SimpleNamespace(enrich=AsyncMock(side_effect=AssertionError('No remote service needed')))
    draft,body=await classify_import(1,[Track('test','Song',['Artist'],'')],store,tagger,genre_hint='phonk')
    assert 'phonk' in body['groups']
    tagger.enrich.assert_not_awaited()
