"""База знаний по руководствам: разбор PDF, фрагменты с номерами страниц PDF и поиск.

Сборка индекса (один раз, после добавления PDF в manuals/):
    python -m app.rag build
Страницы без текстового слоя распознаются Tesseract (rus+eng), если он установлен.
Поиск - BM25 по основам слов (стеммер Snowball), только по документам двигателя машины.
"""
import json
import re
import shutil
import sys
from functools import lru_cache
from pathlib import Path

import snowballstemmer
from rank_bm25 import BM25Okapi

from app.manuals import MANUALS, MANUALS_DIR, BY_ID, manuals_for_engine

INDEX_DIR = Path(__file__).resolve().parent.parent / "index"
CHUNKS_FILE = INDEX_DIR / "chunks.json"
PAGES_FILE = INDEX_DIR / "pages.json"

CHUNK_CHARS = 900      # размер фрагмента
OVERLAP_CHARS = 200    # перекрытие соседних фрагментов, чтобы не резать таблицы и пункты пополам
MIN_TEXT = 50          # меньше символов на странице - считаем, что текстового слоя нет

_stem = snowballstemmer.stemmer("russian")
_WORD = re.compile(r"[a-zа-яё0-9]+(?:[.,][0-9]+)?", re.IGNORECASE)


def clean_text(t: str) -> str:
    """Склеивает переносы «воз-\\nдуха», убирает лишние пробелы, сохраняет строки."""
    t = t.replace("­", "")
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def tokenize(text: str) -> list[str]:
    """Слова в основы: «подшипников» и «подшипника» совпадут."""
    return [_stem.stemWord(w.lower().replace("ё", "е")) for w in _WORD.findall(text)]


def _ocr_page(page) -> str:
    """Распознаёт страницу-картинку; пустая строка, если Tesseract не установлен."""
    if not shutil.which("tesseract"):
        return ""
    import pytesseract
    from PIL import Image
    import io
    pix = page.get_pixmap(dpi=250)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(img, lang="rus+eng")


def _split(text: str) -> list[str]:
    """Режет текст страницы на фрагменты по границам строк."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    lines, chunks, cur = text.split("\n"), [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > CHUNK_CHARS:
            chunks.append(cur)
            cur = cur[-OVERLAP_CHARS:]
            cut = cur.find("\n")
            cur = cur[cut + 1:] if cut != -1 else cur
        cur = f"{cur}\n{line}" if cur else line
    if cur.strip():
        chunks.append(cur)
    return chunks


def build() -> None:
    import pymupdf
    INDEX_DIR.mkdir(exist_ok=True)
    chunks, pages = [], {}
    for m in MANUALS:
        doc = pymupdf.open(MANUALS_DIR / m.file)
        texts, ocr_count = [], 0
        for i, page in enumerate(doc):
            t = clean_text(page.get_text())
            if len(t) < MIN_TEXT:
                o = clean_text(_ocr_page(page))
                if len(o) > len(t):
                    t, ocr_count = o, ocr_count + 1
            texts.append(t)
            for part in _split(t):
                chunks.append({"doc_id": m.doc_id, "page": i + 1, "text": part})
        pages[m.doc_id] = texts
        print(f"{m.doc_id}: {len(doc)} стр., распознано OCR: {ocr_count}", flush=True)
    CHUNKS_FILE.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    PAGES_FILE.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    print(f"Фрагментов: {len(chunks)}")


@lru_cache(maxsize=1)
def _chunks() -> list[dict]:
    return json.loads(CHUNKS_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _pages() -> dict:
    return json.loads(PAGES_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=16)
def _bm25(doc_ids: tuple) -> tuple:
    items = [c for c in _chunks() if c["doc_id"] in doc_ids]
    return BM25Okapi([tokenize(c["text"]) for c in items]), items


def search(query: str, engine_model: str, k: int = 6) -> list[dict]:
    """Фрагменты руководств ТОЛЬКО этого двигателя, лучшие первыми.

    Возвращает [{doc_id, manual, page, text, score}]; пусто - данных в базе нет.
    """
    doc_ids = tuple(m.doc_id for m in manuals_for_engine(engine_model))
    if not doc_ids:
        return []
    bm25, items = _bm25(doc_ids)
    scores = bm25.get_scores(tokenize(query))
    best = sorted(range(len(items)), key=lambda i: scores[i], reverse=True)[:k]
    return [
        {**items[i], "manual": BY_ID[items[i]["doc_id"]].title, "score": round(float(scores[i]), 2)}
        for i in best if scores[i] > 0
    ]


def page_text(doc_id: str, page: int) -> str | None:
    """Текст страницы PDF (нумерация с 1) для окна просмотра."""
    texts = _pages().get(doc_id)
    if texts and 1 <= page <= len(texts):
        return texts[page - 1]
    return None


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build()
    else:
        print("Использование: python -m app.rag build")
