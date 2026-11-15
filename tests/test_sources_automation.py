from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_core import config,store
from vibekeeper.classifier import GENRE_TAGS,matching_categories,group_tracks
from vibekeeper.models import Track
from vibekeeper.music_sources import source_link,parse_yandex,youtube_track
from vibekeeper.providers import ProviderError
from vibekeeper.imports import classify_import
from vibekeeper.automation import refresh_library
from vibekeeper.app import Application


def test_all_genres_have_a_working_canonical_tag():
    assert len(GENRE_TAGS)>=40
    for category,tag in GENRE_TAGS.items():
        assert category in matching_categories([tag]),(category,tag)
    assert 'dnb' in matching_categories(['Drum & Bass'])
    assert 'rnb' in matching_categories(['R&B'])
    assert 'kpop' in matching_categories(['K-pop'])


def test_yandex_and_youtube_urls_only_use_known_hosts():
    assert source_link('https://music.yandex.ru/users/Gleb.borisovone/playlists/3?utm_source=copy')==('yandex',('Gleb.borisovone','3'))
    assert source_link('https://music.youtube.com/playlist?list=PL1234567890')[0]=='youtube'
    for url in ('https://music.yandex.ru.evil/users/a/playlists/3','https://youtube.com@127.0.0.1/playlist?list=PL1234567890'):
        with pytest.raises(ProviderError):source_link(url)


def test_yandex_metadata_parsing_retains_artist_and_link():
    payload={'result':{'kind':3,'title':'My mix','trackCount':1,'tracks':[{'track':{'id':'123','title':'Song','artists':[{'name':'Artist'}]}}]}}
    result=parse_yandex(payload,'User','3')
    assert result['tracks'][0]['uri']=='yandex:track:123'
    assert result['text']=='Artist — Song'
    assert result['platform']=='yandex' and not result['partial']


def test_youtube_keeps_video_id_and_does_not_invent_artist():
    track=youtube_track({'snippet':{'title':'Artist - Track','resourceId':{'videoId':'abcdefghijk'}}})
    assert track.artists==['Artist'] and track.title=='Track'
    assert track.uri=='youtube:video:abcdefghijk'
    assert youtube_track({'snippet':{'title':'Deleted video','resourceId':{'videoId':'abcdefghijk'}}}) is None


@pytest.mark.asyncio
async def test_irrelevant_lastfm_tags_do_not_block_ai_fallback(store,monkeypatch):
    import vibekeeper.imports as module
    monkeypatch.setenv('PROVOD_API_KEY','test-key')
    # Use the canonical tag in a real model response.
    async def infer(session,tracks):
        tracks[0].tags=['drum and bass'];tracks[0].tag_source='ai'
    model=AsyncMock(side_effect=infer)
    monkeypatch.setattr(module,'infer_tags',model)
    track=Track('t','Song',['Artist'],'',['favorite','2025'])
    tagger=SimpleNamespace(enrich=AsyncMock(),session=None)
    _,body=await classify_import(1,[track],store,tagger)
    model.assert_awaited_once()
    assert 'dnb' in body['groups']


@pytest.mark.asyncio
async def test_refresh_updates_same_library_and_keeps_unseen_old_tracks(config,store,monkeypatch):
    app=Application(config,store,None,None)
    _,initial=await classify_import(1,[Track('yandex:track:1','Old',['Artist'],'')],store,
                                   SimpleNamespace(enrich=AsyncMock()),genre_hint='phonk')
    key=initial['library_id'];initial['source_url']='https://music.yandex.ru/users/test/playlists/3';initial['source_platform']='yandex'
    store.update_library(1,key,initial)
    fetched={'platform':'yandex','url':initial['source_url'],'tracks':[{'uri':'yandex:track:2','title':'New','artists':['Artist'],'url':''}]}
    monkeypatch.setattr('vibekeeper.automation.fetch_music_source',AsyncMock(return_value=fetched))
    result=await refresh_library(1,key,app,None)
    assert result['library_id']==key
    assert result['scanned']==2
    assert len(store.library(1))==1
    assert len(result['groups']['phonk'])==2
    store.subscription(1,key,True)
    assert store.subscription(1,key)['enabled']
    store.delete_library(1,key)
    assert store.subscription(1,key)['enabled'] is False


@pytest.mark.asyncio
async def test_personal_genre_corrections_are_reused_and_private(store,monkeypatch):
    monkeypatch.delenv('PROVOD_API_KEY',raising=False)
    monkeypatch.delenv('AI_API_KEY',raising=False)
    tagger=SimpleNamespace(enrich=AsyncMock())
    await classify_import(1,[Track('first','Song',['Artist'],'')],store,tagger,genre_hint='phonk')
    _,mine=await classify_import(1,[Track('new','Song',['Artist'],'',['rock'])],store,tagger)
    assert 'phonk' in mine['groups'] and 'rock' not in mine['groups']
    _,other=await classify_import(2,[Track('new','Song',['Artist'],'',['rock'])],store,tagger)
    assert 'rock' in other['groups'] and 'phonk' not in other['groups']
    store.disconnect(1)
    assert store.track_genre(1,Track('new','Song',['Artist'],'')) is None


@pytest.mark.asyncio
async def test_catalog_and_subscription_api(config,store):
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    from test_miniapp import signed
    from vibekeeper.miniapp import install_miniapp
    app=Application(config,store,None,None)
    server=web.Application();install_miniapp(server,app,None)
    key=store.save_library(1,{'source_url':'https://music.yandex.ru/users/test/playlists/3','groups':{},'scanned':0})
    headers={'X-Telegram-Init-Data':signed(config.bot_token)}
    async with TestClient(TestServer(server)) as client:
        catalog=await (await client.get('/api/catalog')).json()
        assert len(catalog['genres'])>=40
        response=await client.put('/api/library/'+key+'/sync',headers=headers,json={'enabled':True})
        assert (await response.json())['enabled'] is True
        details=await (await client.get('/api/library/'+key,headers=headers)).json()
        assert details['sync']['enabled'] is True
        assert (await client.put('/api/library/'+key+'/sync',json={'enabled':False})).status==401
