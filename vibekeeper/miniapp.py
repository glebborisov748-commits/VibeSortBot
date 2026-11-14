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
    jobs = {}
    application.mini_jobs = jobs
    tasks = set()
    last_ocr = {}

    def identity(request):
        try:
            uid = telegram_user(request.headers.get("X-Telegram-Init-Data", ""), application.config.bot_token)
            if application.config.allowed_ids and uid not in application.config.allowed_ids:
                raise ValueError("forbidden")
            return uid
        except Exception:
            raise web.HTTPUnauthorized(text="Открой приложение кнопкой внутри Telegram-бота.") from None

    async def index(request):
        return web.FileResponse(Path(__file__).parent / "web" / "index.html")

    async def ocr(request):
        uid = identity(request)
        if time.monotonic() - last_ocr.get(uid, 0) < 20:
            return web.json_response({"error": "Подожди 20 секунд перед следующим фото."}, status=429)
        if application.locks[uid].locked():
            return web.json_response({"error": "Дождись предыдущей операции."}, status=409)
        last_ocr[uid] = time.monotonic()
        async with application.locks[uid]:
            try:
                text = await recognize_tracks(session, await request.read())
                return web.json_response({"text": text})
            except ProviderError as error:
                return web.json_response({"error": str(error)}, status=400)

    async def analyze(request):
        uid = identity(request)
        if len(tasks) >= 8:
            return web.json_response({"error": "Сейчас много разборов. Попробуй чуть позже."}, status=429)
        if application.locks[uid].locked():
            return web.json_response({"error": "Дождись предыдущей операции."}, status=409)
        try:
            data = await request.json()
            text = data.get("text", "")
            if not isinstance(text, str) or len(text) > 100_000:
                raise ProviderError("Список слишком большой.")
            tracks = parse_text_tracks(text)
            source_ref = data.get("source_ref")
            public_source = application.store.draft(uid, source_ref) if isinstance(source_ref, str) and source_ref else None
            if source_ref and (not public_source or public_source.get("source") != "playlist_pending"):
                raise ProviderError("Ссылка устарела. Загрузи плейлист ещё раз.")
            if public_source:
                originals = {(", ".join(item["artists"]), item["title"]): item for item in public_source["tracks"]}
                for track in tracks:
                    original = originals.get((", ".join(track.artists), track.title))
                    if original:
                        track.uri, track.url = original["uri"], original["url"]
        except (ValueError, AttributeError, ProviderError) as error:
            return web.json_response({"error": str(error)}, status=400)
        if not application.config.lastfm_key and not ai_available():
            return web.json_response({"error": "Сервис жанров ещё не подключён владельцем бота."}, status=503)
        # Acquire before scheduling: concurrent requests cannot create duplicate jobs.
        await application.locks[uid].acquire()
        job_id = secrets.token_hex(16)
        jobs[job_id] = {"uid": uid, "created": time.time(), "status": "running"}
        async def work():
            try:
                draft, body = await classify_import(uid, tracks, application.store, application.curator.tagger, allow_ai=not bool(public_source))
                jobs[job_id].update(status="ready", draft=draft, body=body)
            except ProviderError as error:
                jobs[job_id].update(status="error", error=str(error))
            except Exception:
                jobs[job_id].update(status="error", error="Не удалось закончить разбор. Попробуй позже.")
            finally:
                application.locks[uid].release()
        task = asyncio.create_task(work())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return web.json_response({"job": job_id})

    async def status(request):
        uid = identity(request)
        job = jobs.get(request.match_info["job"])
        if not job or job["uid"] != uid:
            raise web.HTTPNotFound()
        return web.json_response({key: value for key, value in job.items() if key not in ("uid", "created")})

    async def playlist(request):
        uid = identity(request)
        if application.locks[uid].locked():
            return web.json_response({"error": "Дождись предыдущей операции."}, status=409)
        async with application.locks[uid]:
            try:
                data = await request.json()
                url = data.get("url", "")
                if not isinstance(url, str) or len(url) > 2048:
                    raise ProviderError("Некорректная ссылка.")
                imported = await fetch_playlist(session, url)
                ref = application.store.save_draft(uid, {"source": "playlist_pending", **imported})
                return web.json_response({"source_ref": ref, "text": imported["text"], "title": imported["title"], "count": len(imported["tracks"])})
            except (ProviderError, ValueError, AttributeError) as error:
                return web.json_response({"error": str(error)}, status=400)

    async def cleanup_loop():
        while True:
            await asyncio.sleep(600)
            now = time.time()
            for key in list(jobs):
                if jobs[key]["created"] < now - 86400 and jobs[key]["status"] != "running":
                    del jobs[key]
            for uid in list(last_ocr):
                if time.monotonic() - last_ocr[uid] > 3600:
                    del last_ocr[uid]

    async def lifecycle(app):
        cleaner = asyncio.create_task(cleanup_loop())
        yield
        cleaner.cancel()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(cleaner, *tasks, return_exceptions=True)

    server.cleanup_ctx.append(lifecycle)
    server.router.add_get("/", index)
    server.router.add_get("/app", index)
    server.router.add_post("/api/ocr", ocr)
    server.router.add_post("/api/playlist", playlist)
    server.router.add_post("/api/analyze", analyze)
    server.router.add_get("/api/jobs/{job}", status)
