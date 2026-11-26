"""Озвучка ответов на своём сервере: Silero TTS (модель v4_ru, голос eugene, 48 кГц).

Без зарубежных сервисов и без отправки текста наружу. Модель грузится один раз при первом запросе.
Если torch или файл модели недоступны (например, на ноутбуке разработчика), synth() возвращает None,
и интерфейс озвучивает голосом браузера.

Лицензия моделей Silero: CC BY-NC (некоммерческое использование, для хакатона подходит);
для промышленной эксплуатации нужна коммерческая лицензия у авторов.
"""
import hashlib
import io
import os
import re
import threading
import wave
from pathlib import Path

from app import settings

SPEAKER = os.getenv("TTS_SPEAKER", "eugene")
SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "48000"))
MODEL_PATH = Path(os.getenv("TTS_MODEL_PATH", str(
    Path.home() / ".cache/torch/hub/snakers4_silero-models_master/src/silero/model/v4_ru.pt")))
CACHE_DIR = Path(settings.DB_PATH).parent / "tts"
MAX_CHARS = 900  # ограничение модели на один вызов

_lock = threading.Lock()
_model = None
_failed = False


def available() -> bool:
    return _load() is not None


def _load():
    global _model, _failed
    if _model is not None or _failed:
        return _model
    try:
        import torch
        torch.set_num_threads(max(1, (os.cpu_count() or 2)))
        imp = torch.package.PackageImporter(str(MODEL_PATH))
        _model = imp.load_pickle("tts_models", "model")
        _model.to(torch.device("cpu"))
    except Exception:  # нет torch или модели: озвучка браузером
        _failed = True
    return _model


# Подготовка текста: модель не читает цифры и сокращения, их пишем словами
_ONES = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
         "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
         "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def _below_1000(n: int, fem: bool = False) -> str:
    out = []
    if n >= 100:
        out.append(_HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10])
        n %= 10
    if n or not out:
        w = _ONES[n]
        if fem and n in (1, 2):
            w = "одна" if n == 1 else "две"
        out.append(w)
    return " ".join(out)


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    n = n % 100
    if 11 <= n <= 19:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def num_words(n: int) -> str:
    """Целое число словами."""
    if n == 0:
        return "ноль"
    parts = []
    for div, forms, fem in ((10 ** 6, ("миллион", "миллиона", "миллионов"), False),
                            (1000, ("тысяча", "тысячи", "тысяч"), True)):
        if n >= div:
            k = n // div
            parts.append(f"{num_words(k) if k >= 1000 else _below_1000(k, fem)} {_plural(k, forms)}")
            n %= div
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def _decimal(m: re.Match) -> str:
    whole, frac = m.group(1), m.group(2)
    tail = {1: ("десятая", "десятых", "десятых"), 2: ("сотая", "сотых", "сотых"),
            3: ("тысячная", "тысячных", "тысячных")}.get(len(frac))
    if not tail:
        return f"{num_words(int(whole))} и {num_words(int(frac))}"
    w, f = int(whole), int(frac)
    whole_w = f"{_below_1000(w, True) if w < 1000 else num_words(w)} {_plural(w, ('целая', 'целых', 'целых'))}"
    frac_w = _below_1000(f, True) if f < 1000 else num_words(f)
    return f"{whole_w} {frac_w} {_plural(f, tail)}"


_UNITS = [(r"Н\s*[·*.]?\s*м", "ньютон-метров"), (r"кгс\s*[·*.]?\s*м", "килограмм-силы на метр"),
          (r"кгс/см2|кгс/см²", "килограмм-силы на квадратный сантиметр"), (r"МПа", "мегапаскаля"),
          (r"кПа", "килопаскаля"), (r"об/мин", "оборотов в минуту"), (r"мм", "миллиметра"),
          (r"°\s*[CС]", "градусов"), (r"%", "процентов"), (r"км/ч", "километров в час")]
_ABBR = {"ТОиР": "тоир", "ТО": "тэ о", "ГРМ": "гэ эр эм", "ГБЦ": "гэ бэ цэ", "ОЖ": "охлаждающая жидкость",
         "ML": "эм эль", "ТНВД": "тэ эн вэ дэ", "ЯМЗ": "я эм зэ", "ЗМЗ": "зэ эм зэ", "ГУР": "гур",
         "EGR": "и джи ар", "КПП": "кэ пэ пэ", "РЭ": "руководство по эксплуатации", "РК": "руководство по ремонту"}
_DASHES = {chr(0x2014): ", ", chr(0x2013): "-", chr(0x2026): " до "}


def normalize(text: str) -> str:
    t = text.replace("ё", "е")
    for k, v in _DASHES.items():
        t = t.replace(k, v)
    t = re.sub(r"(\d)\s*-\s*(\d)", r"\1 до \2", t)  # диапазоны 0,30-0,40
    for pat, word in _UNITS:
        t = re.sub(r"(?<=\d)\s*(?:" + pat + ")", " " + word, t)
    for k, v in _ABBR.items():
        t = re.sub(rf"\b{k}\b", v, t)
    t = re.sub(r"(\d+)[.,](\d+)", _decimal, t)
    t = re.sub(r"\d+", lambda m: num_words(int(m.group(0))) if len(m.group(0)) <= 9 else " ".join(
        num_words(int(c)) for c in m.group(0)), t)
    t = re.sub(r"[^\w\s.,!?:;()«»\"'-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _wav(samples, rate: int) -> bytes:
    import numpy as np
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def synth(text: str) -> bytes | None:
    """WAV для фрагмента текста (до MAX_CHARS после подготовки); кеш на диске по тексту и голосу."""
    model = _load()
    if model is None:
        return None
    clean = normalize(text)[:MAX_CHARS]
    if not clean:
        return None
    key = hashlib.sha1(f"{SPEAKER}|{SAMPLE_RATE}|{clean}".encode()).hexdigest()
    path = CACHE_DIR / f"{key}.wav"
    if path.exists():
        return path.read_bytes()
    with _lock:  # одна озвучка за раз: процессор сервера общий
        audio = model.apply_tts(text=clean, speaker=SPEAKER, sample_rate=SAMPLE_RATE)
    data = _wav(audio.numpy(), SAMPLE_RATE)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data
