import csv
import io
import re
from dataclasses import asdict

from .classifier import GENRE_TAGS, LABELS, group_tracks, matching_categories
from .models import Track
from .providers import ProviderError
from .vision import ai_available, infer_tags

MAX_IMPORT_BYTES = 1_000_000
MAX_IMPORT_ROWS = 1000


def parse_text_tracks(text: str) -> list[Track]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > MAX_IMPORT_ROWS:
        raise ProviderError("Пришли от 1 до 1000 треков, каждый с новой строки: Исполнитель — Название.")
    tracks, seen = [], set()
    for number, line in enumerate(lines, 1):
        parts = re.split(r"\s+[—–-]\s+", line, maxsplit=1)
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise ProviderError(f"Не понял строку {number}. Напиши так: Radiohead — No Surprises. Каждый трек — с новой строки.")
        artist, title = (part.strip() for part in parts)
        if len(artist) > 150 or len(title) > 200:
            raise ProviderError(f"Слишком длинное имя или название в строке {number}.")
        identity = (artist.casefold(), title.casefold())
        if identity not in seen:
            seen.add(identity)
            tracks.append(Track(f"import:{len(tracks)}", title, [artist], ""))
    return tracks


def playlist_text(items: list[dict]) -> bytes:
    return ("\n".join(", ".join(item["artists"]) + " — " + item["title"] for item in items) + "\n").encode("utf-8")


def parse_tracks(data: bytes) -> list[Track]:
    if len(data) > MAX_IMPORT_BYTES:
        raise ProviderError("Файл слишком большой. Максимум — 1 МБ и 1000 треков.")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ProviderError("Сохрани CSV-файл в кодировке UTF-8.") from None
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise ProviderError("CSV-файл пустой. Нужны столбцы artist и title.")
    fields = {name.strip().casefold(): name for name in reader.fieldnames if name}
    artist_key = next((fields[x] for x in ("artist", "artist name(s)", "artists", "исполнитель") if x in fields), None)
    title_key = next((fields[x] for x in ("title", "track name", "track", "название") if x in fields), None)
    if not artist_key or not title_key:
        raise ProviderError("Нужны столбцы artist и title (или Artist Name(s) и Track Name).")
    tracks = []
    seen = set()
    for row in reader:
        if len(tracks) >= MAX_IMPORT_ROWS:
            break
        artist = (row.get(artist_key) or "").strip()[:150]
        title = (row.get(title_key) or "").strip()[:200]
        if not artist or not title:
            continue
        identity = (artist.casefold(), title.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        tracks.append(Track(f"import:{len(tracks)}", title, [artist], ""))
    if not tracks:
        raise ProviderError("В файле нет строк с исполнителем и названием трека.")
    return tracks


def playlist_csv(items: list[dict]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["artist", "title"])
    for item in items:
        values = [", ".join(item["artists"]), item["title"]]
        # Prevent spreadsheet software from interpreting untrusted names as formulas.
        writer.writerow(["'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value for value in values])
    return output.getvalue().encode("utf-8-sig")


async def import_draft(uid, data, store, tagger, allow_ai=True):
    tracks = parse_tracks(data)
    return await classify_import(uid, tracks, store, tagger, allow_ai=allow_ai)


async def classify_import(uid, tracks, store, tagger, allow_ai=True, genre_hint="", target_library=None):
    previous = store.library(uid, target_library) if target_library else None
    if target_library and not previous:
        raise ProviderError("Подборка не найдена.")
    warning = ""
    allowed_hints = set(GENRE_TAGS.values())
    manual_genre = genre_hint in allowed_hints
    for track in tracks:
        saved_genre=store.track_genre(uid,track)
        if saved_genre in allowed_hints and not manual_genre:
            track.tags=[saved_genre]
            track.tag_source='user'
            track.tag_url=''
    if not manual_genre:
        try:
            await tagger.enrich(tracks)
        except ProviderError as error:
            if not ai_available() and allow_ai:
                raise
            warning = str(error)
    # Tags such as '2024', 'favorite' and artist names are not genres. They must
    # not accidentally suppress the AI fallback for otherwise unclassified tracks.
    missing = [track for track in tracks if not matching_categories(track.tags)]
    if missing and ai_available() and allow_ai and not manual_genre:
        await infer_tags(tagger.session, missing)
    if manual_genre:
        for track in tracks:
            if genre_hint not in track.tags:
                track.tags.append(genre_hint)
            track.tag_source = "user" if not track.tag_source else track.tag_source
            store.track_genre(uid,track,genre_hint)
    groups = group_tracks(tracks)
    body = {"total": len(tracks), "scanned": len(tracks), "demo": False, "source": "import", "tag_warning": warning,
            "genre_hint": genre_hint,
            "ai_estimated": any(track.tag_source == "ai" for track in tracks),
            "groups": {key: [asdict(track) for track in group] for key, group in groups.items()}}
    titles = [LABELS[key] for key in groups if key != "unknown"]
    body["name"] = (" · ".join(titles[:2]) if titles else "Моя музыка") + f" · {len(tracks)} треков"
    if previous:
        body["name"] = previous.get("name", body["name"])
        for key in ("source_url", "source_platform", "source_kind"):
            if key in previous: body[key]=previous[key]
        if not store.update_library(uid, target_library, body):
            raise ProviderError("Подборка уже удалена. Начни новую загрузку.")
        body["library_id"] = target_library
    else:
        body["library_id"] = store.save_library(uid, body)
    return store.save_draft(uid, body), body
