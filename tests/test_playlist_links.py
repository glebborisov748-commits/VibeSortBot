import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_core import store
from vibekeeper.playlist_links import parse_embed, playlist_id
from vibekeeper.imports import classify_import
from vibekeeper.models import Track
from vibekeeper.providers import ProviderError

PLAYLIST = '6vhNGp6iTwJqGE5Dcl9INl'


def test_public_link_accepts_share_parameters_but_rejects_other_hosts():
    assert playlist_id('https://open.spotify.com/playlist/'+PLAYLIST+'?si=abc&pi=def')==PLAYLIST
    for url in ('http://127.0.0.1/playlist/'+PLAYLIST, 'https://open.spotify.com.evil/playlist/'+PLAYLIST,
                'https://open.spotify.com@127.0.0.1/playlist/'+PLAYLIST, 'https://open.spotify.com/collection/tracks'):
        with pytest.raises(ProviderError): playlist_id(url)


def test_embed_requires_matching_playlist_and_extracts_tracks_only():
    data={'props':{'pageProps':{'state':{'data':{'entity':{'type':'playlist','id':PLAYLIST,'name':'My mix',
        'trackList':[{'uri':'spotify:track:'+'a'*22,'title':'Song','subtitle':'Artist, Other'},
                     {'uri':'spotify:episode:'+'b'*22,'title':'Podcast','subtitle':'Host'}]}}}}}}
    html='<script id="__NEXT_DATA__" type="application/json">'+json.dumps(data)+'</script>'
    result=parse_embed(html,PLAYLIST)
    assert len(result['tracks'])==1
    assert result['tracks'][0]['artists']==['Artist','Other']
    assert result['text']=='Artist, Other — Song'
    with pytest.raises(ProviderError): parse_embed(html,'x'*22)
    with pytest.raises(ProviderError): parse_embed('<html>Login required</html>',PLAYLIST)


@pytest.mark.asyncio
async def test_link_import_never_uses_ai_if_lastfm_unavailable(store,monkeypatch):
    import vibekeeper.imports as imports
    monkeypatch.setenv('PROVOD_API_KEY','test-only')
    model=AsyncMock()
    monkeypatch.setattr(imports,'infer_tags',model)
    tagger=SimpleNamespace(enrich=AsyncMock(side_effect=ProviderError('Last.fm: HTTP 503')))
    draft,body=await classify_import(1,[Track('spotify:track:'+'a'*22,'Song',['Artist'],'')],store,tagger,allow_ai=False)
    assert 'unknown' in body['groups']
    assert body['ai_estimated'] is False
    model.assert_not_awaited()
