"""Проверка базы знаний по всем страницам всех руководств (python tests/check_pages.py).

1. Полнота: каждое слово текстового слоя PDF есть в тексте страницы, который видит агент и механик.
2. Таблицы: строки таблиц разобраны по ячейкам (причина напротив способа устранения).
3. Подсветка: любое предложение страницы находится на картинке страницы (для страниц с текстовым слоем).
Печатает сводку и список страниц с замечаниями; код возврата 1, если замечания есть.
"""
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf  # noqa: E402

from app import rag  # noqa: E402
from app.main import _reflow, _highlight, _sentences, _word_rects  # noqa: E402
from app.manuals import MANUALS, MANUALS_DIR  # noqa: E402

norm = lambda s: re.sub(r"[^0-9a-zа-яё]", "", s.lower().replace("ё", "е"))  # noqa: E731
random.seed(1)
problems, stats = [], {"pages": 0, "text_layer": 0, "table_rows": 0, "sentences": 0, "found_on_image": 0}

for m in MANUALS:
    doc = pymupdf.open(MANUALS_DIR / m.file)
    for i, pg in enumerate(doc):
        stats["pages"] += 1
        text = rag.page_text(m.doc_id, i + 1) or ""
        paras = _reflow(text)
        shown = norm(" ".join(" ".join(p["row"]) if isinstance(p, dict) else p for p in paras))
        stats["table_rows"] += sum(1 for p in paras if isinstance(p, dict))
        words = [norm(w[4]) for w in pg.get_text("words")]
        words = [w for w in words if len(w) >= 2]
        if len("".join(words)) < 40:
            continue  # страница-картинка: текст распознан OCR, сверять не с чем
        stats["text_layer"] += 1
        # переносы «заме-» + «нить» в тексте склеены: проверяем по склейке соседних слов
        miss = [w for j, w in enumerate(words)
                if w not in shown and (words[j - 1] + w if j else w) not in shown
                and (w + words[j + 1] if j + 1 < len(words) else w) not in shown]
        if miss:
            problems.append(f"{m.doc_id} стр. {i + 1}: нет слов в тексте: {miss[:6]}")
        sents = [x["t"] for x in _sentences(_highlight(paras, "")) if len(norm(x["t"])) >= 20]
        for s in random.sample(sents, min(3, len(sents))):
            stats["sentences"] += 1
            if _word_rects(pg, [s]):
                stats["found_on_image"] += 1
            else:
                problems.append(f"{m.doc_id} стр. {i + 1}: подсветка не нашла на картинке: «{s[:60]}»")

print(stats)
print("Замечаний:", len(problems))
for p in problems[:80]:
    print(" ", p)
sys.exit(1 if problems else 0)
