"""Public playlist readers. Never request passwords, cookies or private tokens."""
import asyncio
import html
import json
import os
import re
from dataclasses import asdict
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from .models import Track
from .playlist_links import fetch_playlist
from .providers import ProviderError


def source_link(url):
    if not isinstance(url, str) or len(url)>2048:
        raise ProviderError("Некорректная ссылка.")
    parsed=urlparse(url.strip())
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port:
        raise ProviderError("Нужна HTTPS-ссылка на публичный музыкальный плейлист.")
    if parsed.netloc=='open.spotify.com':
        from .playlist_links import playlist_id
        return 'spotify',playlist_id(url)
    if parsed.netloc in {'music.youtube.com','www.youtube.com','youtube.com'}:
        identity=parse_qs(parsed.query).get('list',[''])[0]
        if parsed.path not in {'/playlist','/watch'} or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',identity):
            raise ProviderError("Нужна ссылка на публичный плейлист YouTube / YouTube Music с параметром list. Личные лайки и история не поддерживаются.")
        return 'youtube',identity
    if parsed.netloc in {'music.yandex.ru','music.yandex.com','music.yandex.kz','music.yandex.by'}:
        match=re.fullmatch(r'/users/([A-Za-z0-9_.@-]{1,100})/playlists/(\d{1,12})/?',parsed.path)
        if not match:
            raise ProviderError("Для Яндекса пока нужна публичная ссылка вида music.yandex.ru/users/имя/playlists/номер. Ссылки нового UUID-формата ещё не поддерживаются.")
        return 'yandex',match.groups()
    raise ProviderError("Поддерживаются публичные ссылки Spotify, Яндекс Музыки и YouTube / YouTube Music.")


async def read_json(session,url,params=None,headers=None):
    async with session.get(url,params=params,headers=headers,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=15)) as response:
        if response.status!=200:
            raise ProviderError(f"Музыкальная платформа вернула HTTP {response.status}.")
        body=bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body)>4_000_000:
                raise ProviderError("Ответ платформы слишком большой. Используй файл или скриншоты.")
        text=body.decode('utf-8')
        if 'недоступна в вашем регионе' in text.casefold() or 'not available in your region' in text.casefold():
            raise ProviderError("Яндекс Музыка недоступна в регионе сервера. Публичность плейлиста здесь ни при чём. Пока можно отправить скриншоты или CSV.")
        try:return json.loads(text)
        except ValueError:raise ProviderError("Платформа не отдала список песен без входа. Используй скриншот или файл.") from None


def result_body(title,tracks,url,platform,partial=False):
    if not tracks:
        raise ProviderError("В публичном ответе нет доступных песен. Используй скриншоты или файл.")
    return {'title':str(title)[:200],'tracks':[asdict(t) for t in tracks], 'url':url,'platform':platform,'partial':partial,
            'text':'\n'.join(', '.join(t.artists)+' — '+t.title for t in tracks)}


def parse_yandex(payload,owner,kind):
    playlist=payload.get('result',payload)
    if 'playlist' in playlist: playlist=playlist['playlist']
    if str(playlist.get('kind',kind))!=kind or playlist.get('visibility')=='private':
        raise ProviderError("Яндекс не отдал публичный плейлист по этому адресу.")
    tracks,seen=[],set()
    for entry in playlist.get('tracks',[])[:300]:
        item=entry.get('track') or entry
        identity=str(item.get('id',''))
        artists=[a['name'] for a in item.get('artists',[]) if isinstance(a.get('name'),str)]
        if not identity.isdigit() or identity in seen or not artists or not isinstance(item.get('title'),str):continue
        seen.add(identity)
        title=item['title']+(' — '+item['version'] if item.get('version') else '')
        tracks.append(Track('yandex:track:'+identity,title[:200],artists,'https://music.yandex.ru/track/'+identity))
    return result_body(playlist.get('title','Яндекс Музыка'),tracks,
        'https://music.yandex.ru/users/'+quote(owner,safe='')+'/playlists/'+kind,'yandex',int(playlist.get('trackCount',len(tracks)))>len(tracks))


async def fetch_yandex(session,identity):
    owner,kind=identity
    failures=[]
    for url,params in [('https://api.music.yandex.net/users/'+quote(owner,safe='')+'/playlists/'+kind,None),
                       ('https://music.yandex.ru/handlers/playlist.jsx',{'owner':owner,'kinds':kind,'light':'true'})]:
        try:return parse_yandex(await read_json(session,url,params),owner,kind)
        except ProviderError as error:failures.append(str(error))
    if any('регион' in message or '451' in message for message in failures):
        raise ProviderError("Яндекс ограничил доступ с региона сервера. Плейлист может быть публичным. Отправь скриншот или CSV; импорт аккаунта сейчас не подключён.")
    raise ProviderError(failures[-1])


def youtube_track(item):
    snippet=item.get('snippet',{})
    identity=snippet.get('resourceId',{}).get('videoId','')
    title=html.unescape(snippet.get('title',''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',identity) or title.casefold() in {'private video','deleted video'}:return None
    # YouTube metadata does not guarantee an artist field. Prefer explicit
    # Artist - Song titles, otherwise keep the owner label with uncertainty.
    parts=re.split(r'\s+[—–-]\s+',title,maxsplit=1)
    if len(parts)==2 and all(parts):artist,title=parts
    else:artist=snippet.get('videoOwnerChannelTitle') or 'Исполнитель не указан'
    return Track('youtube:video:'+identity,title[:200],[artist[:150]],'https://www.youtube.com/watch?v='+identity)


async def fetch_youtube(session,identity):
    key=os.getenv('YOUTUBE_API_KEY','')
    if not key:
        raise ProviderError("Импорт YouTube готов к подключению, но владельцу нужно добавить YOUTUBE_API_KEY в Bothost и включить YouTube Data API v3. Это не ключ Provod.")
    headers={'X-Goog-Api-Key':key}
    meta=await read_json(session,'https://www.googleapis.com/youtube/v3/playlists',
                         {'part':'snippet,contentDetails','id':identity},headers)
    if not meta.get('items'):raise ProviderError("YouTube не отдал публичный плейлист. Проверь, что это обычный плейлист, а не персональный микс.")
    info=meta['items'][0]
    tracks,seen=[],set()
    token=''
    for _ in range(6):
        params={'part':'snippet','playlistId':identity,'maxResults':50}
        if token:params['pageToken']=token
        page=await read_json(session,'https://www.googleapis.com/youtube/v3/playlistItems',params,headers)
        for item in page.get('items',[]):
            track=youtube_track(item)
            if track and track.uri not in seen:seen.add(track.uri);tracks.append(track)
        token=page.get('nextPageToken','')
        if not token:break
    return result_body(info['snippet']['title'],tracks,'https://music.youtube.com/playlist?list='+identity,'youtube',
                       bool(token) or info.get('contentDetails',{}).get('itemCount',len(tracks))>len(tracks))


async def fetch_music_source(session,url,store=None):
    platform,identity=source_link(url)
    if platform=='spotify':return {**await fetch_playlist(session,url,store),'platform':'spotify'}
    try:
        return await (fetch_yandex(session,identity) if platform=='yandex' else fetch_youtube(session,identity))
    except ProviderError:raise
    except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,TypeError,KeyError,UnicodeDecodeError):
        raise ProviderError("Не удалось прочитать музыкальную платформу. Попробуй позже или отправь скриншоты.") from None
