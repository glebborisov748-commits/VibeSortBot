from dataclasses import asdict

from .classifier import LABELS, group_tracks
from .providers import ProviderError


class Curator:
    def __init__(self, store, provider, tagger, limit):
        self.store, self.provider, self.tagger, self.limit = store, provider, tagger, limit

    async def scan(self, uid):
        tracks, total = await self.provider.liked_tracks(uid, self.limit)
        await self.tagger.enrich(tracks)
        groups = group_tracks(tracks)
        body = {"total": total, "scanned": len(tracks), "demo": False,
                "groups": {key: [asdict(t) for t in items] for key, items in groups.items()}}
        return self.store.save_draft(uid, body), body

    async def export(self, uid, draft_id, category):
        body = self.store.draft(uid, draft_id)
        if not body or category not in body["groups"]:
            raise ProviderError("Эта подборка устарела. Запусти /scan заново.")
        if body.get("demo"):
            raise ProviderError("Это демонстрация. Для настоящих треков используй /connect и /scan.")
        if not self.store.reserve_export(uid, draft_id, category):
            previous = self.store.export(uid, draft_id, category)
            if previous["status"] == "complete":
                return previous["playlist"]
            link = "https://open.spotify.com/playlist/" + previous["playlist"] if previous["playlist"] else ""
            raise ProviderError("Эта запись уже начиналась. Чтобы избежать дублей, проверь Spotify вручную. " + link)
        try:
            playlist = await self.provider.create_playlist(uid, LABELS[category])
            playlist_id = playlist["id"]
            self.store.export_status(uid, draft_id, category, "filling", playlist_id)
            uris = list(dict.fromkeys(t["uri"] for t in body["groups"][category]))
            await self.provider.add_tracks(uid, playlist_id, uris)
            self.store.export_status(uid, draft_id, category, "complete")
            return playlist_id
        except Exception:
            self.store.export_status(uid, draft_id, category, "uncertain")
            raise
