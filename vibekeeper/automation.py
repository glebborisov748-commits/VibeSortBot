import asyncio

from .classifier import matching_categories
from .imports import classify_import
from .models import Track
from .music_sources import fetch_music_source
from .providers import ProviderError


def library_tracks(body):
    unique={}
    for items in body.get('groups',{}).values():
        for item in items:unique[item['uri']]=Track(**item)
    return list(unique.values())


async def refresh_library(uid,key,application,session):
    body=application.store.library(uid,key)
    if not body or not body.get('source_url'):
        raise ProviderError('У этой подборки нет ссылки-источника. Загрузить её можно заново через фото или список.')
    imported=await fetch_music_source(session,body['source_url'],application.store)
    tracks=[Track(**item) for item in imported['tracks']]
    old={track.uri:track for track in library_tracks(body)}
    for track in tracks:
        previous=old.get(track.uri)
        if previous:
            track.tags,track.tag_source,track.tag_url=previous.tags,previous.tag_source,previous.tag_url
    # Public widgets can truncate their response. Automatic updates add new
    # entries but must not delete songs merely absent from a partial response.
    fetched_ids={track.uri for track in tracks}
    tracks.extend(track for identity,track in old.items() if identity not in fetched_ids)
    # Grouped library order may differ from source order; compare semantic sets.
    identical={(t.uri,t.title,tuple(t.artists)) for t in tracks}=={(t.uri,t.title,tuple(t.artists)) for t in old.values()}
    if identical:
        return {**body,'library_id':key}
    _,result=await classify_import(uid,tracks,application.store,application.curator.tagger,
        allow_ai=imported['platform']!='spotify' and application.store.settings(uid)['ai_fallback'],
        genre_hint=body.get('genre_hint',''),target_library=key)
    result['source_url'],result['source_platform']=imported['url'],imported['platform']
    application.store.update_library(uid,key,result)
    return result


async def reanalyze_library(uid,key,application):
    body=application.store.library(uid,key)
    if not body:raise ProviderError('Подборка не найдена.')
    tracks=library_tracks(body)
    for track in tracks:
        if not matching_categories(track.tags):track.tags=[]
    _,result=await classify_import(uid,tracks,application.store,application.curator.tagger,
        allow_ai=body.get('source_platform')!='spotify' and not (body.get('source_url','').startswith('https://open.spotify.com/')) and application.store.settings(uid)['ai_fallback'],
        genre_hint=body.get('genre_hint',''),target_library=key)
    return result


async def sync_loop(application,session):
    # Bounded owner-hosted scheduler: at most one refresh per minute, six hours
    # per subscription. Restarting retains next_run in the database.
    while True:
        await asyncio.sleep(60)
        for sub in application.store.due_subscriptions():
            uid,key=sub['uid'],sub['id']
            if application.locks[uid].locked():
                application.store.subscription_result(uid,key,delay=60)
                continue
            async with application.locks[uid]:
                try:
                    await refresh_library(uid,key,application,session)
                    application.store.subscription_result(uid,key)
                except ProviderError as error:
                    application.store.subscription_result(uid,key,str(error))
                except Exception:
                    application.store.subscription_result(uid,key,'Не удалось обновить источник. Повтори вручную позже.')
