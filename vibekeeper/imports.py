import csv
import io
import re
from dataclasses import asdict

from .classifier import group_tracks
from .models import Track
from .providers import ProviderError

MAX_IMPORT_BYTES = 1_000_000
MAX_IMPORT_ROWS = 300


def parse_text_tracks(text: str) -> list[Track]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > MAX_IMPORT_ROWS:
        raise ProviderError("Пришли от 1 до 300 треков, каждый с новой строки: Исполнитель — Название.")
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
        raise ProviderError("Файл слишком большой. Максимум — 1 МБ и 300 треков.")
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


async def import_draft(uid, data, store, tagger):
    tracks = parse_tracks(data)
    return await classify_import(uid, tracks, store, tagger)


async def classify_import(uid, tracks, store, tagger):
    await tagger.enrich(tracks)
    groups = group_tracks(tracks)
    body = {"total": len(tracks), "scanned": len(tracks), "demo": False, "source": "import",
            "groups": {key: [asdict(track) for track in group] for key, group in groups.items()}}
    return store.save_draft(uid, body), body
