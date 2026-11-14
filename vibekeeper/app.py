import asyncio
import logging
from contextlib import suppress
from collections import defaultdict
from dataclasses import asdict
from html import escape

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .classifier import LABELS, group_tracks
from .config import Config
from .models import Track
from .providers import LastFM, ProviderError, Spotify
from .service import Curator
from .store import Store

HELP = (
    "🎧 <b>VibeSort</b> собирает плейлисты из твоих любимых треков Spotify.\n\n"
    "1. Подключи Spotify.\n"
    "2. Нажми «Разобрать любимые».\n"
    "3. Посмотри подборки и создай понравившиеся.\n\n"
    "Для знакомства есть пример без подключения. Бот пока в тестовом режиме; "
    "жанры и настроение определяются по тегам Last.fm.\n\n"
    "Данные и отключение аккаунта: /privacy · /disconnect"
)
PRIVACY = (
    "Это тестовая версия.\n\n"
    "При подключении сохраняю зашифрованные токены Spotify, чтобы читать лайки "
    "и по твоей кнопке создавать приватные плейлисты. Пароль Spotify мне не нужен.\n\n"
    "При /scan передаю Last.fm названия треков и исполнителей для поиска тегов. "
    "Настроение определяется приблизительно по этим тегам, без анализа аудио и нейросети. "
    "Нажимая /scan, ты соглашаешься с такой передачей.\n\n"
    "Черновики с треками зашифрованы и доступны 24 часа. Данные хранятся на хостинге владельца бота. "
    "Рекламы, платежей и передачи библиотеки рекламодателям в этой версии нет.\n\n"
    "/disconnect удаляет токены, черновики и историю операций из рабочей базы бота; "
    "сообщения Telegram и созданные плейлисты остаются. Доступ также можно отозвать "
    "в Spotify → Аккаунт → Приложения."
)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


class Application:
    def __init__(self, config, store, session, bot):
        self.config, self.store, self.bot = config, store, bot
        self.spotify = Spotify(config, store, session)
        self.curator = Curator(store, self.spotify, LastFM(config.lastfm_key, session), config.scan_limit)
        self.locks = defaultdict(asyncio.Lock)
        self.router = Router()
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback)

    async def guarded(self, uid, reply, operation):
        lock = self.locks[uid]
        if lock.locked():
            await reply("Предыдущая операция ещё идёт. Дождись результата.")
            return
        async with lock:
            try:
                await operation()
            except ProviderError as error:
                await reply(escape(str(error)))
            except Exception as error:
                # Logging raw HTTP exceptions can expose OAuth codes/API keys.
                logging.getLogger(__name__).error("Operation failed: %s", type(error).__name__)
                await reply("Операция не завершена. Попробуй позже. Если создавался плейлист, сначала проверь Spotify.")

    async def message(self, message: Message):
        if message.chat.type != "private" or not message.from_user:
            return
        uid = message.from_user.id
        command = (message.text or "").split(" ", 1)[0].split("@", 1)[0]
        if command == "/id":
            await message.answer(f"Твой Telegram ID: <code>{uid}</code>")
            return
        if self.config.allowed_ids and uid not in self.config.allowed_ids:
            await message.answer(f"Сейчас бот доступен тестировщикам. Твой Telegram ID: <code>{uid}</code>")
            return

        async def operation():
            if command in ("/start", "/help"):
                rows = []
                if not self.store.account(uid):
                    rows.append([InlineKeyboardButton(text="1. Подключить Spotify", url=self.spotify.login_url(uid))])
                rows.append([button("2. Разобрать любимые", "scan")])
                rows.append([button("Посмотреть пример", "demo")])
                await message.answer(HELP, reply_markup=keyboard(rows))
            elif command == "/connect":
                url = self.spotify.login_url(uid)
                await message.answer(
                    "Подключи Spotify через официальную страницу входа. Ссылка действует 10 минут. "
                    "Не пересылай её другим людям. О данных: /privacy",
                    reply_markup=keyboard([[InlineKeyboardButton(text="Подключить Spotify", url=url)]]))
            elif command == "/scan":
                await self.scan(uid, message.answer)
            elif command == "/demo":
                await self.demo(uid, message.answer)
            elif command == "/privacy":
                await message.answer(PRIVACY)
            elif command == "/disconnect":
                self.store.disconnect(uid)
                await message.answer("Аккаунт отключён, данные удалены из базы бота. Плейлисты в Spotify остались.")
            else:
                await message.answer("Список команд: /help")

        await self.guarded(uid, message.answer, operation)

    async def scan(self, uid, reply):
        if not self.store.account(uid):
            raise ProviderError("Сначала подключи Spotify: /connect")
        if not self.config.lastfm_key:
            raise ProviderError("Разбор пока настраивается владельцем бота. Сейчас можно посмотреть пример: /demo.")
        await reply(f"Читаю до {self.config.scan_limit} последних лайков и ищу теги Last.fm. "
                    "Это может занять несколько минут. О передаче названий треков: /privacy")
        draft, body = await self.curator.scan(uid)
        await self.show_groups(draft, body, reply)

    async def demo(self, uid, reply):
        samples = [
            Track("demo:1", "Пример спокойного трека", ["Демо-исполнитель"], "", ["chillout", "electronic"]),
            Track("demo:2", "Пример энергичного трека", ["Демо-исполнитель"], "", ["rock", "energetic"]),
            Track("demo:3", "Пример грустного трека", ["Демо-исполнитель"], "", ["indie pop", "melancholic"]),
            Track("demo:4", "Пример без тегов", ["Демо-исполнитель"], ""),
        ]
        body = {"demo": True, "total": 4, "scanned": 4,
                "groups": {key: [asdict(t) for t in items] for key, items in group_tracks(samples).items()}}
        draft = self.store.save_draft(uid, body)
        await self.show_groups(draft, body, reply)

    async def show_groups(self, draft, body, reply):
        if not body["groups"]:
            await reply("В библиотеке не нашлось доступных треков.")
            return
        prefix = "Демонстрация на вымышленных треках.\n\n" if body["demo"] else ""
        text = prefix + f"Доступных треков обработано: {body['scanned']}. Всего лайков: {body['total']}.\n\n"
        text += "Выбери подборку: покажу примеры и предложу создать её в Spotify.\n"
        text += "Жанр и настроение могут пересекаться. Без подходящих тегов — «Без категории». Черновик доступен 24 часа."
        rows = [[button(f"{LABELS[key]} · {len(items)}", f"preview:{draft}:{key}")]
                for key, items in body["groups"].items()]
        await reply(text, reply_markup=keyboard(rows))

    async def callback(self, query: CallbackQuery):
        if not query.message or query.message.chat.type != "private" or (self.config.allowed_ids and query.from_user.id not in self.config.allowed_ids):
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        uid = query.from_user.id
        reply = query.message.answer
        parts = (query.data or "").split(":")

        async def operation():
            if parts == ["scan"]:
                await self.scan(uid, reply)
                return
            if parts == ["demo"]:
                await self.demo(uid, reply)
                return
            if len(parts) != 3 or parts[0] not in ("preview", "export", "groups"):
                return
            action, draft, category = parts
            body = self.store.draft(uid, draft)
            if not body:
                raise ProviderError("Черновик устарел. Запусти /scan.")
            if action == "groups":
                await self.show_groups(draft, body, reply)
                return
            tracks = body["groups"].get(category)
            if not tracks or category not in LABELS:
                raise ProviderError("Подборка не найдена. Запусти /scan.")
            if action == "preview":
                lines = [f"<b>{LABELS[category]}</b> · {len(tracks)} треков\n"]
                for track in tracks[:5]:
                    title = escape((", ".join(track["artists"]) + " — " + track["title"])[:180])
                    if not body["demo"]:
                        title = f'<a href="{escape(track["url"], quote=True)}">{title}</a> (Spotify)'
                    lines.append(title)
                    if track["tags"]:
                        lines.append("Теги: " + escape(", ".join(track["tags"][:5])[:180]))
                        if track.get("tag_url"):
                            lines.append(f'<a href="{escape(track["tag_url"], quote=True)}">Источник: Last.fm</a>')
                rows = []
                if not body["demo"]:
                    lines.append("\nСоздать новый приватный плейлист со всеми треками этой подборки?")
                    rows.append([button("Создать в Spotify", f"export:{draft}:{category}")])
                else:
                    lines.append("\nДемо: запись в Spotify отключена.")
                rows.append([button("← К подборкам", f"groups:{draft}:all")])
                await reply("\n".join(lines), reply_markup=keyboard(rows), disable_web_page_preview=True)
            else:
                await reply("Создаю плейлист в Spotify…")
                playlist_id = await self.curator.export(uid, draft, category)
                await reply("Плейлист готов в Spotify:", reply_markup=keyboard([[
                    InlineKeyboardButton(text="Открыть в Spotify", url="https://open.spotify.com/playlist/" + playlist_id)
                ]]))

        await self.guarded(uid, reply, operation)

    async def oauth(self, request):
        state = request.query.get("state", "")
        uid = self.store.state_owner(state)
        if uid is None or (self.config.allowed_ids and uid not in self.config.allowed_ids):
            return web.Response(text="Ссылка устарела. Вернись в Telegram и введи /connect.", status=400)
        async with self.locks[uid]:
            saved = self.store.consume_state(state)
            if not saved:
                return web.Response(text="Ссылка уже использована. Введи /connect в боте.", status=400)
            code = request.query.get("code")
            if request.query.get("error") or not code:
                return web.Response(text="Подключение отменено. Можно вернуться в Telegram.", status=400)
            try:
                token = await self.spotify.exchange(code, saved["verifier"])
                self.store.save_account(uid, token)
            except Exception as error:
                logging.getLogger(__name__).warning("OAuth failed: %s", type(error).__name__)
                return web.Response(text="Не удалось подключиться. Введи /connect в Telegram заново.", status=502)
        try:
            await self.bot.send_message(uid, "Spotify подключён! Введи /scan, чтобы разобрать любимые. О данных: /privacy")
        except Exception:
            pass  # Credentials are saved even if Telegram notification is temporarily unavailable.
        return web.Response(text="Spotify подключён. Вернись в Telegram и введи /scan.")


@web.middleware
async def secure_headers(request, handler):
    response = await handler(request)
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                             "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
    return response


async def run():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    store = Store(config.data_dir, config.encryption_key)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with Bot(config.bot_token, default=DefaultBotProperties(parse_mode="HTML")) as bot:
                application = Application(config, store, session, bot)
                dispatcher = Dispatcher()
                dispatcher.include_router(application.router)
                server = web.Application(middlewares=[secure_headers])
                server.router.add_get("/oauth/spotify", application.oauth)

                async def health(request):
                    return web.json_response({"status": "ok"})

                async def privacy(request):
                    return web.Response(text=PRIVACY)

                server.router.add_get("/health", health)
                server.router.add_get("/privacy", privacy)
                runner = web.AppRunner(server, access_log=None)  # OAuth codes must never enter access logs.
                await runner.setup()
                async def maintenance():
                    while True:
                        await asyncio.sleep(3600)
                        store.purge_expired()

                maintenance_task = asyncio.create_task(maintenance())
                try:
                    await web.TCPSite(runner, "0.0.0.0", config.port).start()
                    await bot.set_my_commands([BotCommand(command=command, description=description) for command, description in [
                        ("start", "Начать"), ("connect", "Подключить Spotify"), ("scan", "Разобрать любимые"),
                        ("demo", "Демонстрация"), ("privacy", "О данных"), ("disconnect", "Удалить данные"), ("id", "Мой Telegram ID"),
                    ]])
                    await bot.delete_webhook(drop_pending_updates=False)
                    await dispatcher.start_polling(bot, close_bot_session=False)
                finally:
                    maintenance_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await maintenance_task
                    await runner.cleanup()
    finally:
        store.close()
