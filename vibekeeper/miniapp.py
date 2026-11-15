import asyncio
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from .imports import classify_import, parse_text_tracks
from .providers import ProviderError
from .vision import MAX_IMAGE_BYTES, ai_available, recognize_tracks
from .playlist_links import fetch_playlist
from .classifier import group_tracks
from .models import Track
from dataclasses import asdict


def telegram_user(init_data, token):
    pairs = parse_qsl(init_data, keep_blank_values=True)
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("duplicate fields")
    signature = values.pop("hash", "")
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid signature")
    age = time.time() - int(values["auth_date"])
    if not -30 <= age <= 86400:
        raise ValueError("expired")
    uid = json.loads(values["user"])["id"]
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise ValueError("invalid user")
    return uid


def install_miniapp(server, application, session):
    jobs, tasks = {}, set()
    application.mini_jobs = jobs
    last_ocr = {}

    def identity(request):
        try:
            uid = telegram_user(request.headers.get('X-Telegram-Init-Data', ''), application.config.bot_token)
            if application.config.allowed_ids and uid not in application.config.allowed_ids:
                raise ValueError('forbidden')
            return uid
        except Exception:
            raise web.HTTPUnauthorized(text='Открой приложение кнопкой внутри Telegram-бота.') from None

    async def index(request):
        return web.FileResponse(Path(__file__).parent / 'web' / 'index.html')

    async def start_job(uid, kind, operation):
        if len(tasks) >= 8:
            return web.json_response({'error':'Сейчас много загрузок. Попробуй чуть позже.'}, status=429)
        lock = application.locks[uid]
        if lock.locked():
            return web.json_response({'error':'Предыдущая загрузка ещё идёт. Дождись результата.'}, status=409)
        await lock.acquire()
        job_id = secrets.token_hex(16)
        jobs[job_id] = {'uid':uid, 'kind':kind, 'created':time.time(), 'status':'running'}
        async def work():
            try:
                result = await operation()
                jobs[job_id].update(status='ready', **result)
            except ProviderError as error:
                jobs[job_id].update(status='error', error=str(error))
            except Exception:
                jobs[job_id].update(status='error', error='Не удалось завершить загрузку. Исходный список остался в поле ввода.')
            finally:
                lock.release()
        task = asyncio.create_task(work())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return web.json_response({'job':job_id}, status=200)

    async def ocr(request):
        uid = identity(request)
        if time.monotonic()-last_ocr.get(uid, 0) < 20:
            return web.json_response({'error':'Подожди 20 секунд перед следующим фото.'}, status=429)
        data = await request.read()
        if len(data)>MAX_IMAGE_BYTES:
            return web.json_response({'error':'Фото слишком большое. Максимум — 6 МБ.'}, status=413)
        async def operation():
            return {'text':await recognize_tracks(session, data)}
        response = await start_job(uid, 'ocr', operation)
        if response.status == 200:
            last_ocr[uid] = time.monotonic()
        return response

    async def analyze(request):
        uid = identity(request)
        try:
            data = await request.json()
            text = data.get('text', '')
            if not isinstance(text, str) or len(text)>100_000:
                raise ProviderError('Список слишком большой.')
            tracks = parse_text_tracks(text)
            ref = data.get('source_ref')
            public_source = application.store.draft(uid, ref) if isinstance(ref,str) and ref else None
            if ref and (not public_source or public_source.get('source')!='playlist_pending'):
                raise ProviderError('Ссылка устарела. Загрузи плейлист ещё раз.')
            if public_source:
                originals = {(', '.join(t['artists']),t['title']):t for t in public_source['tracks']}
                for track in tracks:
                    original = originals.get((', '.join(track.artists),track.title))
                    if original:
                        track.uri,track.url=original['uri'],original['url']
            hint=data.get('genre_hint','')
            allowed={'','phonk','funk','rock','pop','hip hop','electronic','metal','jazz','classical'}
            if not isinstance(hint,str) or hint not in allowed:
                raise ProviderError('Выбери жанр из списка.')
            options=application.store.settings(uid)
        except (ValueError, AttributeError, ProviderError) as error:
            return web.json_response({'error':str(error)},status=400)
        async def operation():
            draft,body=await classify_import(uid,tracks,application.store,application.curator.tagger,
                allow_ai=not bool(public_source) and options['ai_fallback'],genre_hint=hint)
            if public_source:
                body['name']=public_source['title']
                body['source_url']=public_source['url']
                application.store.update_library(uid,body['library_id'],body)
            return {'draft':draft,'body':body}
        return await start_job(uid,'analyze',operation)

    async def status(request):
        uid=identity(request)
        job=jobs.get(request.match_info['job'])
        if not job or job['uid']!=uid:
            return web.json_response({'error':'Загрузка не найдена. Возможно, сервер перезапустился. Начни ещё раз.'},status=404)
        return web.json_response({k:v for k,v in job.items() if k not in ('uid','created')})

    async def playlist(request):
        uid=identity(request)
        try:
            data=await request.json()
            url=data.get('url','')
            if not isinstance(url,str) or len(url)>2048:
                raise ProviderError('Некорректная ссылка.')
            from .playlist_links import playlist_id
            playlist_id(url)
        except (ValueError, AttributeError, ProviderError) as error:
            return web.json_response({'error':str(error)},status=400)
        async def operation():
            imported=await fetch_playlist(session,url,application.store)
            ref=application.store.save_draft(uid,{'source':'playlist_pending',**imported})
            return {'source_ref':ref,'text':imported['text'],'title':imported['title'],
                    'count':len(imported['tracks']),'cached':imported.get('cached',False)}
        return await start_job(uid,'playlist',operation)

    async def library(request):
        uid=identity(request)
        key=request.match_info.get('item')
        if not key:
            return web.json_response({'items':application.store.library(uid)})
        body=application.store.library(uid,key)
        if not body:
            return web.json_response({'error':'Подборка не найдена.'},status=404)
        if request.method=='DELETE':
            application.store.delete_library(uid,key)
            for job_id in list(jobs):
                if jobs[job_id]['uid']==uid and jobs[job_id].get('body',{}).get('library_id')==key:
                    del jobs[job_id]
            return web.json_response({'ok':True})
        if request.method=='PATCH':
            data=await request.json()
            name=data.get('name')
            if not isinstance(name,str) or not 1<=len(name.strip())<=100:
                return web.json_response({'error':'Название должно содержать от 1 до 100 символов.'},status=400)
            body['name']=name.strip()
            application.store.update_library(uid,key,body)
        return web.json_response({'id':key,'body':body})

    async def settings(request):
        uid=identity(request)
        if request.method=='PUT':
            data=await request.json()
            if type(data.get('ai_fallback')) is not bool:
                return web.json_response({'error':'Неверные настройки.'},status=400)
            application.store.settings(uid,{'ai_fallback':data['ai_fallback']})
        return web.json_response(application.store.settings(uid))

    async def set_genre(request):
        uid=identity(request)
        key=request.match_info['item']
        body=application.store.library(uid,key)
        if not body:
            return web.json_response({'error':'Подборка не найдена.'},status=404)
        data=await request.json()
        genre=data.get('genre_hint')
        allowed={'phonk','funk','rock','pop','hip hop','electronic','metal','jazz','classical'}
        if not isinstance(genre,str) or genre not in allowed:
            return web.json_response({'error':'Выбери жанр.'},status=400)
        tracks={}
        unknown={t['uri'] for t in body['groups'].get('unknown',[])}
        for items in body['groups'].values():
            for item in items:
                track=Track(**item)
                if track.uri in unknown:
                    track.tags=[genre]
                    track.tag_source='user'
                tracks[track.uri]=track
        body['groups']={group:[asdict(t) for t in items] for group,items in group_tracks(list(tracks.values())).items()}
        application.store.update_library(uid,key,body)
        return web.json_response({'id':key,'body':body})

    async def delete_data(request):
        uid=identity(request)
        if application.locks[uid].locked():
            return web.json_response({'error':'Дождись завершения текущей загрузки.'},status=409)
        application.store.disconnect(uid)
        for key in list(jobs):
            if jobs[key]['uid']==uid: del jobs[key]
        return web.json_response({'ok':True})

    async def lifecycle(app):
        async def clean():
            while True:
                await asyncio.sleep(600)
                for key in list(jobs):
                    if jobs[key]['created']<time.time()-86400 and jobs[key]['status']!='running': del jobs[key]
                for uid in list(last_ocr):
                    if time.monotonic()-last_ocr[uid]>3600: del last_ocr[uid]
        cleaner=asyncio.create_task(clean())
        yield
        cleaner.cancel()
        for task in list(tasks): task.cancel()
        await asyncio.gather(cleaner,*tasks,return_exceptions=True)

    server.cleanup_ctx.append(lifecycle)
    server.router.add_get('/',index)
    server.router.add_get('/app',index)
    server.router.add_post('/api/ocr',ocr)
    server.router.add_post('/api/playlist',playlist)
    server.router.add_post('/api/analyze',analyze)
    server.router.add_get('/api/jobs/{job}',status)
    server.router.add_get('/api/library',library)
    server.router.add_route('GET','/api/library/{item}',library)
    server.router.add_route('DELETE','/api/library/{item}',library)
    server.router.add_route('PATCH','/api/library/{item}',library)
    server.router.add_get('/api/settings',settings)
    server.router.add_put('/api/settings',settings)
    server.router.add_post('/api/library/{item}/genre',set_genre)
    server.router.add_delete('/api/data',delete_data)
