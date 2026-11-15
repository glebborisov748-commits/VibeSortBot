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

EXTRA_GENRES = {
    "house": ("Хаус", {"house", "deep house", "tech house", "progressive house", "slap house", "tropical house", "хаус"}),
    "techno": ("Техно", {"techno", "minimal techno", "hard techno", "melodic techno", "техно"}),
    "trance": ("Транс", {"trance", "psytrance", "psy trance", "progressive trance", "uplifting trance", "транс"}),
    "dnb": ("Драм-н-бейс", {"drum and bass", "drum & bass", "dnb", "drum'n'bass", "liquid funk", "liquid dnb", "jungle", "драм н бейс"}),
    "dubstep": ("Дабстеп", {"dubstep", "brostep", "riddim", "melodic dubstep", "дабстеп"}),
    "hardstyle": ("Хардстайл", {"hardstyle", "hardcore techno", "gabber", "rawstyle", "хардстайл"}),
    "garage": ("UK Garage / Bass", {"uk garage", "garage", "2 step", "2step", "bassline", "uk bass", "future garage"}),
    "edm": ("EDM / Dance", {"edm", "dance", "dance music", "electro house", "big room", "future bass", "eurodance", "dance pop"}),
    "synthwave": ("Синтвейв", {"synthwave", "retrowave", "outrun", "darksynth", "vaporwave", "синтвейв"}),
    "ambient": ("Эмбиент", {"ambient", "dark ambient", "drone", "space ambient", "эмбиент"}),
    "lofi": ("Лоу-фай", {"lo fi", "lofi", "lo fi hip hop", "lofi hip hop", "chillhop", "лоу фай"}),
    "rnb": ("R&B", {"r&b", "rnb", "rhythm and blues", "contemporary r&b", "alternative r&b"}),
    "soul": ("Соул", {"soul", "neo soul", "motown", "соул"}),
    "blues": ("Блюз", {"blues", "blues rock", "delta blues", "electric blues", "блюз"}),
    "reggae": ("Регги", {"reggae", "dub", "roots reggae", "dancehall", "ska", "регги"}),
    "reggaeton": ("Реггетон", {"reggaeton", "reggaetón", "latin urban", "latin trap", "реггетон"}),
    "latin": ("Латино", {"latin", "latin pop", "salsa", "bachata", "bossa nova", "samba", "merengue", "латино"}),
    "kpop": ("K-pop", {"k pop", "kpop", "korean pop", "к поп"}),
    "jpop": ("J-pop / J-rock", {"j pop", "jpop", "j rock", "jrock", "japanese pop", "anisong"}),
    "hyperpop": ("Гиперпоп", {"hyperpop", "digicore", "glitchcore", "гиперпоп"}),
    "indie": ("Инди", {"indie", "indie rock", "indie pop", "indie folk", "bedroom pop", "инди"}),
    "punk": ("Панк", {"punk", "punk rock", "pop punk", "post punk", "hardcore punk", "панк"}),
    "grunge": ("Гранж / Альтернатива", {"grunge", "post grunge", "alternative", "alternative rock", "shoegaze", "гранж"}),
    "folk": ("Фолк / Акустика", {"folk", "indie folk", "folk rock", "acoustic", "singer songwriter", "фолк", "акустика"}),
    "country": ("Кантри", {"country", "country pop", "americana", "bluegrass", "кантри"}),
    "disco": ("Диско", {"disco", "nu disco", "italo disco", "диско"}),
    "afrobeats": ("Афробит", {"afrobeats", "afrobeat", "afro pop", "amapiano", "afro house"}),
    "soundtrack": ("Саундтреки", {"soundtrack", "ost", "film score", "cinematic", "video game music", "саундтрек"}),
    "world": ("Этника / World", {"world", "world music", "ethnic", "celtic", "traditional", "этника"}),
    "chanson": ("Шансон", {"chanson", "russian chanson", "шансон", "русский шансон"}),
    "drill": ("Дрилл", {"drill", "uk drill", "brooklyn drill", "new york drill", "дрилл"}),
    "trap": ("Трэп", {"trap", "cloud rap", "emo rap", "rage rap", "трэп", "клауд рэп"}),
}
RULES.update(EXTRA_GENRES)
MOOD_KEYS = {"chill", "energy", "melancholy", "dark", "romantic"}
GENRE_TAGS = {key: key for key in RULES if key not in MOOD_KEYS}
# Stable, unambiguous canonical tags for AI and explicit genre selection.
GENRE_TAGS.update({"phonk":"phonk","funk":"funk","rock":"rock","pop":"pop","hiphop":"hip hop","electronic":"electronic",
                  "metal":"metal","jazz":"jazz","classical":"classical","dnb":"drum and bass",
                  "edm":"edm","lofi":"lofi","rnb":"r&b","indie":"indie","folk":"folk",
                  "world":"world music","kpop":"k pop","jpop":"j pop","grunge":"grunge",
                  "soundtrack":"soundtrack","reggae":"reggae","garage":"uk garage"})
LABELS = {key:title for key,(title,_) in RULES.items()} | {"unknown":"Без категории"}


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", value.casefold())).strip()


def group_tracks(tracks: list[Track]) -> dict[str, list[Track]]:
    groups = defaultdict(list)
    seen = set()
    for track in tracks:
        if track.uri in seen:
            continue
        seen.add(track.uri)
        matches = matching_categories(track.tags)
        for key in matches or ["unknown"]:
            groups[key].append(track)
    return dict(groups)


def matching_categories(tags):
    values = {normalize(tag) for tag in tags}
    return [key for key, (_, aliases) in RULES.items() if values & {normalize(alias) for alias in aliases}]
