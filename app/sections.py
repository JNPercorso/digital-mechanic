"""Карта разделов руководств: какие страницы PDF к какому разделу относятся и что это за раздел.

Источник - оглавления самих PDF (строки «Название ...... 265»). Печатный номер страницы сопоставляется
со страницей PDF по смещению, которое определяется по совпадению заголовков разделов с текстом страниц.
Для руководства без оглавления разделы берутся из заголовков на страницах.

Тип раздела нужен агенту, чтобы различать контекст норматива: эксплуатация и ТО (работающая машина),
неисправности, ремонт (разборка и сборка), обкатка и испытания после ремонта, перечни деталей.

Сборка: python -m app.sections build  (пишет index/sections.json)
"""
import json
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

from app import settings
from app.manuals import MANUALS

SECTIONS_FILE = Path(settings.INDEX_DIR) / "sections.json"

TOC_LINE = re.compile(r"^\s*(?P<title>[^\n]{3,140}?)\s*(?:\.{2,}|…+|\s{3,})[\s.…]*(?P<page>\d{1,3})\s*$")

# тип раздела по названию (первое совпадение)
KINDS = [
    ("обкатка", r"обкатк|испытани|приработк"),
    ("неисправности", r"неисправност|отказ|устранени|диагностик"),
    ("детали", r"перечень деталей|запасн|деталей разового|ремонтные размеры|каталог|комплект прокладок"),
    ("обслуживание", r"техническое обслуживание|\bто\b|обслуживани|смазк[аи] двигател|регулировк|проверк"),
    ("эксплуатация", r"эксплуатаци|\bпуск|\bостановк|меры безопасност|указани|хранени|транспортир|консервац"),
    ("характеристика", r"характеристик|техническ(ие|ая) данн|основные (данные|параметры)|комплектност|маркировк"),
    ("ремонт", r"ремонт|разборк|сборк|дефект|снятие|установк|восстановлени|замен|мойк|^\d{4}\.|карта"),
    ("устройство", r"устройство|описание|конструкци|система|механизм|двигател|работа"),
]


def kind_of(title: str) -> str:
    t = title.lower()
    for k, pat in KINDS:
        if re.search(pat, t):
            return k
    return "прочее"


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", s.lower().replace("ё", "е"))


TOC_ROW = re.compile(r"^(?P<title>.*?[^\d\s.|…])[\s.|…]{2,}(?P<page>\d{1,3})$")


def _level(title: str) -> int:
    """Уровень раздела: «6» и ЗАГЛАВНЫЕ - главный, «6.3» и обычные строчные - подраздел."""
    num = re.match(r"^(\d+(?:\.\d+)*)\.?\s", title)
    if num:
        return num.group(1).count(".") + 1
    letters = [c for c in title if c.isalpha()]
    return 1 if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7 else 2


def parse_toc(pages: list[str], toc_pages: list[int]) -> list[dict]:
    """Строки оглавления в любом из форматов: «Название . . . 12», «Название ..... 12», «Название | 12»,
    перенос названия на следующую строку и номер страницы отдельной строкой."""
    out = []
    for p in toc_pages:
        pending = ""
        for raw in pages[p - 1].split(chr(10)):
            line = raw.strip()
            if not line or re.match(r"^(содержание|стр\.?|\|?\s*стр\.?)$", line, re.I):
                continue
            if re.fullmatch(r"\d{1,3}", line):  # номер страницы отдельной строкой
                if pending:
                    out.append({"title": pending.strip(" .…|"), "printed": int(line), "level": _level(pending)})
                    pending = ""
                continue
            pairs = re.findall(r"([^|]*?[^\d\s|][^|]*?)\s*\|\s*(\d{1,3})(?=\s|$)", line) if "|" in line else []
            if len(pairs) > 1:  # табличное оглавление: несколько строк «Название | стр.» слиплись в одну
                for i, (t, pg) in enumerate(pairs):
                    t = ((pending + " ") if i == 0 and pending else "") + t
                    out.append({"title": t.strip(" .…|"), "printed": int(pg), "level": _level(t.strip())})
                pending = ""
                continue
            m = TOC_ROW.match(line)
            if m:
                title = (pending + " " + m.group("title")).strip(" .…|") if pending else m.group("title").strip(" .…|")
                out.append({"title": title, "printed": int(m.group("page")), "level": _level(title)})
                pending = ""
            else:
                pending = (pending + " " + line.strip(" .…|")).strip()[-160:]
    return out


def find_offset(pages: list[str], entries: list[dict]) -> int:
    """Смещение «страница PDF - печатная страница»: где заголовок раздела встречается в тексте страницы."""
    votes = Counter()
    for e in entries:
        key = _norm(re.sub(r"^\d+(\.\d+)*\.?\s*", "", e["title"]))[:30]
        if len(key) < 8:
            continue
        for d in range(-15, 30):
            i = e["printed"] + d
            if 1 <= i <= len(pages) and key in _norm(pages[i - 1])[:600]:
                votes[d] += 1
    return votes.most_common(1)[0][0] if votes else 0


def headings_from_pages(pages: list[str]) -> list[dict]:
    """Руководство без оглавления: заголовок раздела - первая содержательная строка страницы,
    если она похожа на заголовок (короткая, без точки в конце)."""
    out, last = [], None
    for i, t in enumerate(pages):
        first = next((ln.strip() for ln in t.split("\n") if len(ln.strip()) > 6 and not re.match(r"^\d+$", ln.strip())), "")
        if first and len(first) < 90 and not first.endswith(".") and first != last:
            out.append({"title": first, "pdf": i + 1, "level": 1})
            last = first
    return out


def build() -> None:
    from app.rag import _pages
    from app.rules import is_toc_page
    allp = _pages()
    result = {}
    for m in MANUALS:
        pages = allp[m.doc_id]
        toc = [i + 1 for i, t in enumerate(pages) if is_toc_page(t)]
        entries = parse_toc(pages, toc) if toc else []
        if len(entries) >= 10:
            off = find_offset(pages, entries)
            for e in entries:
                e["pdf"] = e["printed"] + off
            entries = [e for e in entries if 1 <= e["pdf"] <= len(pages)]
            entries.sort(key=lambda e: (e["pdf"], e["level"]))
            source = f"оглавление, смещение {off:+d}"
        else:
            entries = headings_from_pages(pages)
            source = "заголовки страниц"
        # страница -> путь раздела (верхний уровень и текущий подраздел)
        top, cur, by_page = None, None, {}
        ei = 0
        for p in range(1, len(pages) + 1):
            while ei < len(entries) and entries[ei]["pdf"] <= p:
                e = entries[ei]
                if e["level"] == 1 or top is None:
                    top = e
                cur = e
                ei += 1
            if top:
                title = top["title"] if cur is top else f"{top['title']} / {cur['title']}"
                # тип: главный раздел «обкатка», «неисправности», «детали» определяет тип всех своих страниц,
                # иначе - по подразделу, а если он нейтральный - по главному разделу
                tk, k = kind_of(top["title"]), kind_of(cur["title"])
                if tk in ("обкатка", "неисправности", "детали") or k in ("устройство", "прочее"):
                    k = tk
                if k in ("устройство", "прочее") and m.kind == "РК":
                    k = "ремонт"  # в руководствах по ремонту страницы деталей - карты дефектации и ремонта
                by_page[p] = {"section": title[:200], "kind": k}
        result[m.doc_id] = {"source": source, "pages": by_page}
        print(f"{m.doc_id}: {source}, разделов {len(entries)}, размечено страниц {len(by_page)} из {len(pages)}")
    SECTIONS_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(SECTIONS_FILE.read_text(encoding="utf-8")) if SECTIONS_FILE.exists() else {}


def section_of(doc_id: str, page: int) -> dict:
    """{"section": путь раздела, "kind": тип} для страницы PDF; пусто, если карты нет."""
    return _data().get(doc_id, {}).get("pages", {}).get(str(page), {})


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build()
