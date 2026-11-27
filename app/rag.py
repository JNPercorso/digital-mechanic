"""База знаний по руководствам: разбор PDF, фрагменты с номерами страниц PDF и поиск.

Сборка индекса (один раз, после добавления PDF в manuals/):
    python -m app.rag build [--reuse-ocr]
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

from app import settings
from app.manuals import MANUALS, MANUALS_DIR, BY_ID, manuals_for_engine

INDEX_DIR = Path(settings.INDEX_DIR)
CHUNKS_FILE = INDEX_DIR / "chunks.json"
PAGES_FILE = INDEX_DIR / "pages.json"

CHUNK_CHARS = 900      # размер фрагмента
OVERLAP_CHARS = 200    # перекрытие соседних фрагментов, чтобы не резать таблицы и пункты пополам
MIN_TEXT = 50          # меньше символов на странице - считаем, что текстового слоя нет

_stem = snowballstemmer.stemmer("russian")
_WORD = re.compile(r"[a-zа-яё0-9]+(?:[.,][0-9]+)?", re.IGNORECASE)


# Символы шрифта Symbol в PDF приходят из области частного использования Unicode (U+F0xx):
# в окне страницы это квадратики, а модель не понимает, что «207000» - это «20…7000»
SYMBOL_FONT = {0xF02D: "-", 0xF02B: "+", 0xF0B0: "°", 0xF0B1: "±", 0xF0B7: "•", 0xF0C6: "∅", 0xF0D7: "·",
               0xF0B4: "×", 0xF0A2: "′", 0xF0B2: "″", 0xF0BC: "…", 0xF03D: "=", 0xF0A3: "≤", 0xF0B3: "≥"}


def fix_symbols(t: str) -> str:
    return t.translate(SYMBOL_FONT)


def clean_text(t: str) -> str:
    """Склеивает переносы «воз-\\nдуха», убирает лишние пробелы, сохраняет строки."""
    t = fix_symbols(t).replace("­", "")
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


SUB = re.compile(r"^([а-яa-z]\)|\d+(\.\s?\d+)*\.?\s)")  # начало подпункта: а) или 10.8


def _table_rows(pg, tb):
    """Строки таблицы; ячейка с подпунктами а), б)... без линий между ними делится по высоте строк."""
    import pymupdf
    words = pg.get_text("words")
    out = []
    for row in tb.rows:
        cells = list(row.cells)
        cols = []
        for c in cells:
            if c is None:
                cols.append([])
                continue
            r = pymupdf.Rect(c)
            lines = {}
            for w in words:
                if r.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)):
                    lines.setdefault((w[5], w[6]), []).append(w)
            ls = sorted(lines.values(), key=lambda ws: (round(ws[0][1]), ws[0][0]))
            cols.append([(ws[0][1], " ".join(x[4] for x in ws)) for ws in ls])
        starts = [y for y, t in (cols[0] if cols else []) if SUB.match(t)]
        if not cols[0] or len(starts) < 2:
            out.append([" ".join(t for _, t in col) for col in cols])
            continue
        bounds = sorted(set([cols[0][0][0]] + starts))
        segs = [[[] for _ in cols] for _ in bounds]
        for ci, col in enumerate(cols):
            for y, t in col:
                k = max([i for i, b in enumerate(bounds) if y >= b - 2] or [0])
                segs[k][ci].append(t)
        for s in segs:
            out.append([" ".join(x) for x in s])
    return [[clean_text(re.sub(r"(\w)- (\w)", r"\1\2", c)) for c in r] for r in out]


CHR10 = chr(10)
TABLE_SEP = " | "  # строка таблицы в тексте страницы: ячейки через разделитель


def page_text_pdf(pg) -> str:
    """Текст страницы с текстовым слоем; таблицы - построчно «причина | способ устранения».

    Обычное чтение PDF выдаёт сначала всю левую колонку таблицы, потом всю правую, и способ
    устранения оказывается не напротив своей причины. Таблицы разбираем по ячейкам, подпункты
    а), б)... внутри ячейки сопоставляем с правой колонкой по высоте строки.
    """
    import pymupdf
    blocks = pg.get_text("dict")["blocks"]
    words = pg.get_text("words")
    norm = lambda x: re.sub(r"[^0-9a-zа-яё]", "", x.lower().replace("ё", "е"))  # noqa: E731
    center_in = lambda r, bb: r.contains(pymupdf.Point((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2))  # noqa: E731
    tabs = []  # (таблица, прямоугольник, строки); таблица, при разборе которой потерялось слово, идёт обычным текстом
    for t in pg.find_tables().tables:
        if t.col_count < 2:
            continue
        r = pymupdf.Rect(t.bbox)
        rows = [TABLE_SEP.join(x.replace("|", "/") for x in row) for row in _table_rows(pg, t)]
        rows = [x for x in rows if x.replace("|", "").strip()]
        joined = norm(" ".join(rows))
        inside = {(b["number"], i) for b in blocks for i, ln in enumerate(b.get("lines", [])) if center_in(r, ln["bbox"])}
        lost = [w[4] for w in words if (w[5], w[6]) in inside and len(norm(w[4])) >= 1 and norm(w[4]) not in joined]
        if not lost:
            tabs.append((t, r, rows))
    rects = [r for _, r, _ in tabs]
    items = []  # (y, x, текст) блоков вне таблиц; строки внутри таблиц отбрасываем по одной
    for b in blocks:
        keep = []
        for ln in b.get("lines", []):
            if not any(center_in(r, ln["bbox"]) for r in rects):
                keep.append((ln["bbox"][1], ln["bbox"][0], "".join(sp["text"] for sp in ln["spans"])))
        if keep:
            items.append((keep[0][0], keep[0][1], CHR10.join(t for _, _, t in keep)))
    for _, r, rows in tabs:
        items.append((r.y0, r.x0, CHR10.join(rows)))
    if tabs:  # на страницах с таблицами - сверху вниз (руководства в одну колонку)
        items.sort(key=lambda it: (round(it[0]), it[1]))
    return clean_text(CHR10.join(t for _, _, t in items))


def build(reuse_ocr: bool = False) -> None:
    import pymupdf
    INDEX_DIR.mkdir(exist_ok=True)
    prev = json.loads(PAGES_FILE.read_text(encoding="utf-8")) if reuse_ocr and PAGES_FILE.exists() else {}
    chunks, pages = [], {}
    for m in MANUALS:
        doc = pymupdf.open(MANUALS_DIR / m.file)
        texts, ocr_count = [], 0
        old = prev.get(m.doc_id) or []
        for i, page in enumerate(doc):
            t = page_text_pdf(page) if len(clean_text(page.get_text())) >= MIN_TEXT else ""
            if len(t) < MIN_TEXT:
                # распознанный ранее текст страницы-картинки берём из прежнего индекса (OCR долгий)
                o = old[i] if reuse_ocr and i < len(old) else clean_text(_ocr_page(page))
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
    from app.rules import page_usable
    bm25, items = _bm25(doc_ids)
    scores = bm25.get_scores(tokenize(query))
    order = sorted(range(len(items)), key=lambda i: scores[i], reverse=True)
    out = []
    for i in order:  # оглавления и страницы про другие модели двигателя в общем руководстве - не источник
        if scores[i] <= 0 or len(out) >= k:
            break
        if page_usable(items[i]["doc_id"], items[i]["page"], engine_model):
            out.append({**items[i], "manual": BY_ID[items[i]["doc_id"]].title, "score": round(float(scores[i]), 2)})
    return out


PART_NO = re.compile(r"\b\d{3,6}[.-]\d{6,7}(?:[-.][\wА-ЯЁ]+)?")  # обозначение детали: 236-1007118-В
_NOT_PART = re.compile(r"двигател|руководств|ГОСТ|для снятия|схема|рис\.|ф\.\s*«|приспособлен", re.I)


def parts_for(engine_model: str, node_groups: list[list[str]], limit: int = 8) -> list[dict]:
    """Кандидаты в запчасти узла: строки руководств двигателя с обозначением детали, где упомянут узел.

    Обычный поиск по словам обозначения не находит, поэтому смотрим строки страниц напрямую.
    Короткие строки «Наименование обозначение» и строки таблиц - первыми.
    """
    out, seen = [], set()
    for m in manuals_for_engine(engine_model):
        for i, t in enumerate(_pages().get(m.doc_id, [])):
            lines = t.split(CHR10)
            for j, ln in enumerate(lines):
                for d in PART_NO.findall(ln):
                    ctx = ln if len(ln) > 25 else (f"{lines[j - 1]} {ln}" if j else ln)
                    if d in seen or _NOT_PART.search(ctx) or len(ctx) > 140:
                        continue
                    st = tokenize(ctx)
                    if any(all(any(x.startswith(k) for x in st) for k in g) for g in node_groups):
                        seen.add(d)
                        out.append({"designation": d, "text": ctx.strip(), "doc_id": m.doc_id, "page": i + 1})
    out.sort(key=lambda x: len(x["text"]))
    return out[:limit]


def page_text(doc_id: str, page: int) -> str | None:
    """Текст страницы PDF (нумерация с 1) для окна просмотра."""
    texts = _pages().get(doc_id)
    if texts and 1 <= page <= len(texts):
        return texts[page - 1]
    return None


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build(reuse_ocr="--reuse-ocr" in sys.argv)
    elif sys.argv[1:2] == ["fix-symbols"]:  # заменить символы шрифта Symbol в готовой базе без пересборки
        pages = json.loads(PAGES_FILE.read_text(encoding="utf-8"))
        pages = {k: [fix_symbols(t) for t in v] for k, v in pages.items()}
        chunks = [{**c, "text": fix_symbols(c["text"])} for c in json.loads(CHUNKS_FILE.read_text(encoding="utf-8"))]
        PAGES_FILE.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        CHUNKS_FILE.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        print("Символы заменены")
    else:
        print("Использование: python -m app.rag build [--reuse-ocr] | fix-symbols")
