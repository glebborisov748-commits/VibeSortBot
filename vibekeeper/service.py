import re

from .classifier import LABELS
from .providers import ProviderError

SPOTIFY_TRACK = re.compile(r'spotify:track:[A-Za-z0-9]{22}')


class Curator:
    def __init__(self, store, provider, tagger, limit):
        self.store, self.provider, self.tagger, self.limit = store, provider, tagger, limit

    async def scan(self, uid, limit=None):
        from .imports import classify_import
        tracks, total = await self.provider.liked_tracks(uid, self.limit if limit is None else limit)
        draft, body = await classify_import(uid, tracks, self.store, self.tagger, allow_ai=False)
        body.update(total=total, source='spotify', source_platform='spotify', source_kind='spotify_likes', source_url='spotify:liked')
        self.store.update_library(uid, body['library_id'], body)
        # Replace the short-lived draft with the same body used by the library.
        return self.store.save_draft(uid, body), body

    async def export(self, uid, draft_id, category):
        body = self.store.draft(uid, draft_id)
        return await self._export_body(uid, draft_id, body, category)

    async def export_library(self, uid, library_id, category):
        body = self.store.library(uid, library_id)
        return await self._export_body(uid, 'library:' + library_id, body, category)

    async def _export_body(self, uid, operation_id, body, category):
        if not body or category not in body.get('groups', {}):
            raise ProviderError('Подборка не найдена. Открой библиотеку заново.')
        if body.get('demo'):
            raise ProviderError('Это демонстрация. Для экспорта нужна твоя подборка.')
        tracks = body['groups'][category]
        missing = sum(not SPOTIFY_TRACK.fullmatch(track.get('uri', '')) for track in tracks)
        if missing or not tracks:
            raise ProviderError(f'У {missing or len(tracks)} треков нет точного Spotify ID. Чтобы не добавить другую песню или версию, экспорт остановлен. Загрузи исходный плейлист Spotify или свои лайки.')
        previous = self.store.export(uid, operation_id, category)
        if previous:
            if previous['status'] == 'complete':
                return previous['playlist']
            link = 'https://open.spotify.com/playlist/' + previous['playlist'] if previous['playlist'] else ''
            raise ProviderError('Эта запись уже начиналась. Чтобы избежать дублей, сначала проверь Spotify. ' + link)
        # An access refusal must not reserve an operation or leave a stuck draft.
        validate = getattr(self.provider, 'validate_access', None)
        if validate:
            await validate(uid)
        if not self.store.reserve_export(uid, operation_id, category):
            raise ProviderError('Эта подборка уже записывается. Дождись результата.')
        try:
            playlist = await self.provider.create_playlist(uid, LABELS.get(category, category))
            playlist_id = playlist['id']
            self.store.export_status(uid, operation_id, category, 'filling', playlist_id)
            uris = list(dict.fromkeys(track['uri'] for track in tracks))
            await self.provider.add_tracks(uid, playlist_id, uris)
            self.store.export_status(uid, operation_id, category, 'complete')
            return playlist_id
        except Exception:
            self.store.export_status(uid, operation_id, category, 'uncertain')
            raise
