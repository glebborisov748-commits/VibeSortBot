import re
from collections import defaultdict

from .models import Track

# Only explicit tags. A genre does not imply mood. Rules are intentionally editable.
RULES = {
    "phonk": ("Фонк", {"phonk", "drift phonk", "brazilian phonk", "фонк", "cosmic phonk", "cosmic funk", "lunar funk"}),
    "funk": ("Фанк", {"funk", "brazilian funk", "baile funk", "funk carioca", "funk brasileiro", "бразильский фанк", "фанк"}),
    "rock": ("Рок", {"rock", "alternative rock", "indie rock", "hard rock", "punk rock", "post rock"}),
    "pop": ("Поп", {"pop", "indie pop", "synthpop", "dream pop", "electropop"}),
    "hiphop": ("Хип-хоп", {"hip hop", "rap", "trap", "russian rap", "хип хоп", "рэп"}),
    "electronic": ("Электроника", {"electronic", "electronica", "house", "techno", "trance", "drum and bass", "dubstep"}),
    "metal": ("Метал", {"metal", "heavy metal", "death metal", "black metal", "metalcore"}),
    "jazz": ("Джаз", {"jazz", "smooth jazz", "jazz fusion"}),
    "classical": ("Классика", {"classical", "piano", "orchestral", "neoclassical"}),
    "chill": ("Спокойный вайб", {"chill", "chillout", "relax", "relaxing", "calm", "mellow", "ambient", "downtempo"}),
    "energy": ("Энергичный вайб", {"energetic", "upbeat", "workout", "high energy"}),
    "melancholy": ("Меланхолия", {"melancholic", "melancholy", "sad", "sadness"}),
    "dark": ("Мрачный вайб", {"dark", "dark ambient", "haunting", "gloomy"}),
    "romantic": ("Романтика", {"romantic", "romance", "love"}),
}
LABELS = {key: title for key, (title, _) in RULES.items()} | {"unknown": "Без категории"}


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", value.casefold())).strip()


def group_tracks(tracks: list[Track]) -> dict[str, list[Track]]:
    groups = defaultdict(list)
    seen = set()
    for track in tracks:
        if track.uri in seen:
            continue
        seen.add(track.uri)
        tags = {normalize(tag) for tag in track.tags}
        matches = [key for key, (_, aliases) in RULES.items() if tags & aliases]
        for key in matches or ["unknown"]:
            groups[key].append(track)
    return dict(groups)
