from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_core import config, store
from vibekeeper.providers import ProviderError
from vibekeeper.service import Curator
from vibekeeper.models import Track

URI='spotify:track:'+'a'*22


def provider():
    return SimpleNamespace(validate_access=AsyncMock(),create_playlist=AsyncMock(return_value={'id':'playlist1'}),add_tracks=AsyncMock())


@pytest.mark.asyncio
async def test_imported_exact_ids_can_be_exported_from_library_without_duplicates(store):
    body={'source':'import','groups':{'chill':[{'uri':URI,'title':'Song','artists':['Artist'],'url':''}]}}
    key=store.save_library(1,body)
    api=provider();curator=Curator(store,api,None,1000)
    assert await curator.export_library(1,key,'chill')=='playlist1'
    store.purge_expired()
    assert await curator.export_library(1,key,'chill')=='playlist1'
    api.create_playlist.assert_awaited_once()
    api.add_tracks.assert_awaited_once_with(1,'playlist1',[URI])
    with pytest.raises(ProviderError):await curator.export_library(2,key,'chill')


@pytest.mark.asyncio
async def test_access_refusal_does_not_reserve_or_create_a_playlist(store):
    key=store.save_library(1,{'groups':{'rock':[{'uri':URI}]}})
    api=provider();api.validate_access.side_effect=ProviderError('403')
    curator=Curator(store,api,None,1000)
    with pytest.raises(ProviderError):await curator.export_library(1,key,'rock')
    assert store.export(1,'library:'+key,'rock') is None
    api.create_playlist.assert_not_awaited()
    api.validate_access.side_effect=None
    assert await curator.export_library(1,key,'rock')=='playlist1'


@pytest.mark.asyncio
async def test_no_guessing_or_partial_export_for_tracks_without_spotify_ids(store):
    key=store.save_library(1,{'groups':{'rock':[{'uri':URI},{'uri':'import:0'}]}})
    api=provider()
    with pytest.raises(ProviderError,match='нет точного Spotify ID'):
        await Curator(store,api,None,1000).export_library(1,key,'rock')
    api.create_playlist.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_312_favorites_survive_full_import(store):
    api=provider()
    api.liked_tracks=AsyncMock(return_value=([Track('spotify:track:'+str(i).zfill(22),'Song '+str(i),['Artist'],'') for i in range(312)],312))
    tagger=SimpleNamespace(enrich=AsyncMock())
    _,body=await Curator(store,api,tagger,300).scan(1,limit=10000)
    assert body['scanned']==312 and body['total']==312
    assert store.library(1,body['library_id'])['source_kind']=='spotify_likes'
    api.liked_tracks.assert_awaited_once_with(1,10000)


@pytest.mark.asyncio
async def test_spotify_export_endpoint_returns_real_playlist_link(config,store):
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    from test_miniapp import signed
    from vibekeeper.app import Application
    from vibekeeper.miniapp import install_miniapp
    app=Application(config,store,None,None)
    app.curator.provider=provider()
    store.save_account(1,{'access_token':'test-only'})
    key=store.save_library(1,{'groups':{'chill':[{'uri':URI}]}})
    server=web.Application();install_miniapp(server,app,None)
    headers={'X-Telegram-Init-Data':signed(config.bot_token)}
    async with TestClient(TestServer(server)) as client:
        response=await client.post('/api/library/'+key+'/spotify',headers=headers,json={'category':'chill'})
        job=(await response.json())['job']
        result=await (await client.get('/api/jobs/'+job,headers=headers)).json()
        assert result['status']=='ready'
        assert result['playlist_url']=='https://open.spotify.com/playlist/playlist1'
        app.curator.provider.add_tracks.assert_awaited_once()
