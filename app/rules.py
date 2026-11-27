"""Жёсткие правила заказчика, которые проверяет код, а не только промпт.

- Сбой проверки: диагноз не ставится, модель не вызывается, заявки нет (F3, С3).
- Ссылки: только документы двигателя этой машины и только страницы, найденные поиском
  в этом же запросе (F4: норматив от другого двигателя - ошибка, выдуманных страниц нет).
- Точность ML ниже 0,50: заявки нет; ниже 0,60 или запись с помехами: вывод предварительный,
  заявка только после подтверждения механика.
- Приоритет: базовая критичность типа неисправности и поправка по оценке отклонения.
- Заявка: только страницы по узлу неисправности, с дословной цитатой; числа - только со ссылкой.
"""
import re

from app.manuals import BY_ID, manuals_for_engine

LOW_CONFIDENCE = 0.6  # ниже - вывод предварительный, нужна повторная проверка

CITE = re.compile(r"\[\[([a-z0-9_]+):(\d+)\]\]")


def is_check_failed(result: dict) -> bool:
    return result.get("status") == "check_failed" or result.get("signal_quality") == "bad"


def is_low_confidence(result: dict) -> bool:
    c = result.get("confidence")
    return result.get("status") == "anomaly" and (
        (c is not None and c < LOW_CONFIDENCE) or result.get("signal_quality") == "warning")


def check_failed_text(result: dict) -> str:
    return ("**Сбой проверки: запись непригодна для анализа.**\n\n"
            "Вывод о состоянии двигателя не делаю: по плохой записи диагноз поставить нельзя.\n\n"
            "Что сделать:\n"
            "1. Проверьте, что двигатель прогрет и работает на холостом ходу.\n"
            "2. Убедитесь, что микрофонный пост стоит под двигателем и рядом нет посторонних шумов.\n"
            "3. Повторите запись и запросите результат снова.")


def allowed_doc_ids(engine_model: str) -> set[str]:
    return {m.doc_id for m in manuals_for_engine(engine_model)}


_ANY_CITE = re.compile(r"\[\[([^\]]+)\]\]")


_PART = re.compile(r"(?:([a-z][a-z0-9_]*)\s*[:,]?\s*)?(?:стр\.?|с\.|p\.?)?\s*(\d+)(?:\s*[-–]\s*(\d+))?", re.I)


def split_citations(text: str) -> tuple[str, list[str]]:
    """Приводит ссылки модели к виду [[doc_id:стр]].

    Модель иногда пишет несколько страниц или диапазон в одной ссылке: [[yamz536_re:173, 215]],
    [[yamz536_re:173; yamz536_rk:20]], [[yamz536_re:173-175]], [[yamz536_re стр. 173]].
    Нераспознанные ссылки не теряются молча: возвращаются списком для журнала и техпанели.
    """
    unparsed = []

    def repl(m: re.Match) -> str:
        out, doc = [], None
        inner = re.sub(r"doc_id\s*[:=]\s*", "", m.group(1), flags=re.I)  # [[doc_id:zmz409_re:128]]
        for part in re.split(r"[;,](?=\s*(?:[a-z0-9_]+\s*:|\d))", inner):
            mm = _PART.fullmatch(part.strip())
            if not mm or not (mm.group(1) or doc):
                unparsed.append(m.group(0))
                return ""
            doc = (mm.group(1) or doc).lower()
            a = int(mm.group(2))
            b = int(mm.group(3) or a)
            out += [f"[[{doc}:{p}]]" for p in range(a, min(b, a + 4) + 1)]
        return " ".join(out)
    return _ANY_CITE.sub(repl, text), unparsed


def filter_citations(text: str, engine_model: str, seen_pages: set[tuple]) -> tuple[str, list[dict], list[str]]:
    """Оставляет в тексте только допустимые ссылки [[doc_id:page]].

    Возвращает (текст, список ссылок, список отклонённых ссылок для журнала).
    """
    text, unparsed = split_citations(text)
    allowed = allowed_doc_ids(engine_model)
    refs, rejected = [], [f"нераспознано {u}" for u in unparsed]

    def repl(m: re.Match) -> str:
        doc_id, page = m.group(1), int(m.group(2))
        if doc_id in allowed and (doc_id, page) in seen_pages and page_usable(doc_id, page, engine_model):
            if (doc_id, page) not in [(r["doc_id"], r["page"]) for r in refs]:
                refs.append({"doc_id": doc_id, "manual": BY_ID[doc_id].title, "page": page})
            return m.group(0)
        rejected.append(f"{doc_id}:{page}")
        return ""

    return CITE.sub(repl, text), refs, rejected


def check_references(refs: list[dict], engine_model: str, seen_pages: set[tuple]) -> tuple[list[dict], list]:
    """Ссылки для заявки: та же проверка, что и для текста ответа."""
    allowed = allowed_doc_ids(engine_model)
    ok, bad = [], []
    for r in refs:
        key = (r.get("doc_id"), int(r.get("page") or 0))
        if key[0] in allowed and key in seen_pages:
            if key not in [(x["doc_id"], x["page"]) for x in ok]:
                ok.append({"doc_id": key[0], "manual": BY_ID[key[0]].title, "page": key[1]})
        else:
            bad.append(r)
    return ok, bad


def recheck_operation(result: dict) -> str:
    """Обязательная первая работа при низкой точности ML (механик её не убирает)."""
    return "Повторная запись звука на посту и повторная проверка: вывод ML предварительный (низкая точность распознавания)"


# ---------- Пороги точности распознавания (порог задаём сами и показываем механику) ----------
NO_TICKET_CONFIDENCE = 0.50  # ниже - заявка не формируется, только повторная запись


def n2(x) -> str:
    return f"{x:.2f}".replace(".", ",")


def is_no_ticket(result: dict) -> bool:
    c = result.get("confidence")
    return result.get("status") == "anomaly" and c is not None and c < NO_TICKET_CONFIDENCE


def warning_text(result: dict) -> str | None:
    """Предупреждение о качестве вывода: показывается всегда, как бы ни был получен результат."""
    if is_check_failed(result):
        return None
    c, parts = result.get("confidence"), []
    if result.get("signal_quality") == "warning":
        parts.append("запись с помехами")
    if c is not None and c < LOW_CONFIDENCE:
        parts.append(f"точность распознавания {n2(c)} ниже порога {n2(LOW_CONFIDENCE)}")
    if not parts:
        return None
    head = "; ".join(parts)
    head = head[0].upper() + head[1:]
    if is_no_ticket(result):
        return (f"{head}. Точность ниже {n2(NO_TICKET_CONFIDENCE)}: вывод ненадёжен, "
                "заявка не формируется. Повторите запись на посту.")
    if result.get("status") != "anomaly":
        return f"{head}. Вывод предварительный."
    return (f"{head}. Вывод предварительный: заявку можно отправить только после "
            "вашего подтверждения, первой работой в ней будет повторная проверка.")


def no_ticket_text(result: dict) -> str:
    """Ответ при точности ниже порога: модель не вызывается, заявки нет."""
    return (f"**Вывод ненадёжен: точность распознавания {n2(result.get('confidence') or 0)} "
            f"ниже порога {n2(NO_TICKET_CONFIDENCE)}.**\n\n"
            f"ML предполагает: {result.get('anomaly_title') or result.get('anomaly_type')} "
            f"(оценка отклонения {n2(result.get('anomaly_score') or 0)}), но по такой записи диагноз не ставлю "
            "и заявку не формирую.\n\n"
            "Что сделать:\n"
            "1. Прогрейте двигатель, оставьте холостой ход.\n"
            "2. Проверьте, что рядом с постом нет посторонних шумов.\n"
            "3. Повторите запись и запросите результат снова.")


# ---------- Тип неисправности: базовая критичность и узел (для приоритета и применимости страницы) ----------
# Критичность - инженерная оценка последствий при эксплуатации (обоснование в README):
# high - угроза разрушения двигателя или безопасности (смазка, перегрев, цепь ГРМ, тормоза и рулевое);
# medium - прогрессирующий износ или отказ узла без немедленной угрозы; low - вспомогательные системы.
# Узел - группы основ слов: страница относится к узлу, если в ней есть все основы хотя бы одной группы.
FAULTS = {
    "F01": ("medium", [["клапан"], ["коромысл"], ["толкател"], ["газораспредел"]]),
    "F02": ("high", [["давлен", "масл"], ["масл", "насос"], ["манометр"]]),
    "F03": ("high", [["перегрев"], ["охлажд"], ["термостат"], ["радиатор"], ["прокладк", "головк"]]),
    "F04": ("medium", [["ремн"], ["ремен"], ["натяжн"], ["шкив"]]),
    "F05": ("low", [["генератор"]]),
    "F06": ("medium", [["сцеплен"]]),
    "F07": ("medium", [["коробк", "передач"], ["кпп"]]),
    "F08": ("medium", [["топлив"], ["форсунк"], ["тнвд"]]),
    "F09": ("medium", [["турбокомпрессор"], ["наддув"], ["турбин"]]),
    "F10": ("high", [["компрессор"], ["гур"], ["усилител", "рул"]]),
    "F11": ("low", [["рециркуляц"], ["egr"]]),
    "F12": ("low", [["зажиган"], ["свеч"], ["катушк"]]),
    "F13": ("medium", [["бензин"], ["топлив"], ["форсунк"], ["дроссел"]]),
    "F14": ("high", [["цеп"], ["натяжител"], ["грм"]]),
}
LEVELS = ["low", "medium", "high"]
PRIORITY_RU = {"low": "низкий", "medium": "средний", "high": "высокий"}
RU_LEVEL = {"low": "низкая", "medium": "средняя", "high": "высокая"}
URGENCY_MEANING = {
    "high": "дефектовка до выпуска в рейс",
    "medium": "дефектовка на ближайшем ТО, машина работает под наблюдением",
    "low": "машина работает, узел проверяется на следующей проверке",
}
SCORE_UP, SCORE_DOWN = 0.85, 0.60  # поправка по оценке отклонения - не больше одного уровня


def urgency_reason(result: dict) -> tuple[str, str]:
    """Приоритет считает код: базовая критичность типа неисправности плюс-минус один уровень по оценке отклонения."""
    code = result.get("anomaly_type")
    base = FAULTS.get(code, ("medium", []))[0]
    score, conf = result.get("anomaly_score") or 0, result.get("confidence") or 0
    i = LEVELS.index(base)
    if score >= SCORE_UP:
        i, adj = min(i + 1, 2), f"сильное отклонение (оценка {n2(score)}, порог {n2(SCORE_UP)}): на уровень выше"
    elif score < SCORE_DOWN:
        i, adj = max(i - 1, 0), f"слабое отклонение (оценка {n2(score)}, ниже {n2(SCORE_DOWN)}): на уровень ниже"
    else:
        adj = f"оценка отклонения {n2(score)}: без поправки"
    urgency = LEVELS[i]
    why = (f"Базовая критичность {code} «{result.get('anomaly_title') or ''}»: {RU_LEVEL[base]}; {adj}; "
           f"точность распознавания {n2(conf)}. Это значит: {URGENCY_MEANING[urgency]}")
    if is_low_confidence(result):
        why += (". Вывод предварительный"
                f"{' (запись с помехами)' if result.get('signal_quality') == 'warning' else ''}"
                ": первой работой повторная проверка")
    return urgency, why


def _stems(text: str) -> set[str]:
    from app.rag import tokenize
    return set(tokenize(text))


def node_query(result: dict) -> str:
    """Запрос для первого поиска по неисправности: название и слова узла."""
    words = " ".join(" ".join(g) for g in FAULTS.get(result.get("anomaly_type"), (None, []))[1])
    proc = PROCEDURE_HINT.get(result.get("anomaly_type"), "")
    return f"{result.get('anomaly_title') or ''} {proc} {words}".strip()


def page_matches_node(page_text: str, code: str) -> bool:
    """Страница относится к узлу из типа неисправности (иначе это не инструкция по этой неисправности)."""
    groups = FAULTS.get(code, (None, []))[1]
    if not groups:
        return True
    st = _stems(page_text)
    return any(all(any(t.startswith(k) for t in st) for k in g) for g in groups)


def is_toc_page(page_text: str) -> bool:
    """Оглавление: много строк, оканчивающихся номером страницы (часто через точки)."""
    lines = [ln.strip() for ln in page_text.split(chr(10)) if ln.strip()]
    if not lines:
        return False
    tail = sum(1 for ln in lines if re.search(r"(\.{3,}|\s)\d{1,3}$", ln) and len(ln) > 8)
    head = " ".join(lines[:3]).lower()
    return (tail >= 8 and tail / len(lines) > 0.35) or (("содержание" in head or "оглавление" in head) and tail >= 4)


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", s.lower().replace("ё", "е"))


def quote_on_page(quote: str, page_text: str) -> bool:
    """Дословная цитата есть на странице (без учёта регистра, пробелов, знаков и переносов)."""
    q, p = _norm(quote), _norm(page_text)
    if len(q) < 15:
        return False
    if q in p:
        return True
    words = [_norm(w) for w in quote.split() if _norm(w)]  # допуск на ошибки распознавания: 80% троек слов
    shingles = ["".join(words[i:i + 3]) for i in range(max(1, len(words) - 2))]
    return sum(1 for s in shingles if s in p) / len(shingles) >= 0.8


_MODEL = re.compile(r"(ЯМЗ|ЗМЗ|МАЗ|КАМАЗ|ПАЗ|УАЗ|ГАЗ|Урал|ЗИЛ)\s*[-–]?\s*\d[\w.]*", re.I)
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_BAN = re.compile(r"(эксплуатац|эксплуатир|выпуск|выезд|ездить|на лини|в рейс)[^.]{0,40}(нельзя|не допуска|запрещ|не следует)|"
                  r"(нельзя|запрещ\w*|не допуска\w*|не следует)[^.]{0,30}(эксплуатац|эксплуатир|выпуска|выезжа|ездить|на лини|в рейс)|"
                  r"прекрат\w* эксплуатац|не выпуска\w*", re.I)
_NOT_NORM = re.compile(r"(стр\.?|страниц\w*|рисун\w*|рис\.|таблиц\w*|F\d\d|S-\d+|№\s?\d+)\s*\d*", re.I)


def _block_cites(lines: list[str]) -> list[list]:
    """Для каждой строки - ссылки её блока (абзац или список до пустой строки или заголовка).
    Модель часто ставит одну ссылку на весь список нормативов, а не в каждую строку."""
    out, cur, idx = [], [], []
    def flush():
        cites = [c for i in idx for c in CITE.findall(lines[i])]
        for _ in idx:
            out.append(cites)
        idx.clear()
    for i, line in enumerate(lines):
        st = line.strip()
        if not st or re.match(r"^\*\*(.+?)\*\*\s*$", st) or re.match(r"^#+\s", st):
            flush()
            out.append([])
            continue
        idx.append(i)
    flush()
    return out


_ABSENT = re.compile(r"не\s+(приведен|указан|найден|описан|содерж)|нет\s+(данных|сведений|значени|норматив)|отсутству|"
                     r"в\s+руководств\w*[^.]{0,40}нет", re.I)


def unsupported_lines(text: str, allowed_numbers: set[str] = frozenset()) -> tuple[str, list[str]]:
    """Числа из руководства только со ссылкой: строка с числом без [[...]] убирается.

    Не проверяются: раздел наблюдений (там числа из результата проверки ML), заголовки,
    нумерация пунктов, номера страниц, рисунков, таблиц, кодов неисправностей и проверок.
    """
    out, removed, section = [], [], ""
    lines = text.split(chr(10))
    block = _block_cites(lines)
    for li, line in enumerate(lines):
        s = line.strip()
        h = re.match(r"^\*\*(.+?)\*\*\s*$", s) or re.match(r"^#+\s*(.+)$", s)
        if h:
            section = h.group(1).lower()
            out.append(line)
            continue
        body = re.sub(r"\[\[[^\]]*\]\]", "", s)
        body = re.sub(r"^(\d+[.)]|[-•*])\s+", "", body)
        body = _NOT_NORM.sub("", _MODEL.sub("", body))
        for x in sorted(allowed_numbers, key=len, reverse=True):  # данные машины (оценки, даты) - не нормативы
            body = body.replace(x, "")
        ban = (_BAN.search(s) or (_MANUAL_CLAIM.search(s) and not _NO_MANUAL.search(s))) and "[[" not in s
        # запрет эксплуатации и ссылка на руководство на словах - только со ссылкой на страницу
        cited = "[[" in s or bool(block[li])  # ссылка в строке или в её блоке (одна ссылка на весь список)
        if not ban and ("противореч" in section or cited or not _NUM.search(body)):
            out.append(line)
        else:
            removed.append(s)
    return chr(10).join(out), removed


# Общие слова действий и связки: есть в любой работе, ничего не доказывают
_GENERIC = ("провер", "замен", "регулир", "отрегул", "необходим", "осмотр", "выполн", "состоян", "налич",
            "устран", "дефектовк", "ремонт", "работ", "при", "после", "перед", "также", "затем", "случа")
_SECTION_NO = re.compile(r"\b(пп?\.|п\.п\.|раздел\w*|глав\w*)\s*\d+(\.\d+)*", re.I)


def op_supported(op: str, pages_text: str) -> bool:
    """Работа или утверждение опирается на страницы-источники.

    Все числа (нормативы, номера деталей) должны быть на этих страницах; номера пунктов и разделов не в счёт.
    Если числа есть и все нашлись - опора подтверждена. Иначе хотя бы половина значимых слов
    (без общих слов действий) должна встречаться на страницах.
    """
    pt = _norm(pages_text)
    nums = _NUM.findall(_SECTION_NO.sub("", _MODEL.sub("", op)))
    for num in nums:
        if _norm(num) not in pt:
            return False
    words = {w for w in _stems(op) if len(w) >= 4 and not w[:1].isdigit() and not w.startswith(_GENERIC)}
    if not words:
        return True
    share = len(words & _stems(pages_text)) / len(words)
    return share >= (0.3 if nums else 0.5)


def no_instruction_text(result: dict, engine: str, problem: str = "") -> str:
    """Заявка не сформирована: применимой инструкции нет. Механик получает рекомендацию на дефектовку
    с результатом проверки и оформляет её вне продукта по порядку автопарка."""
    title = result.get("anomaly_title") or result.get("anomaly_type")
    return (f"Заявка не сформирована: в руководстве на двигатель {engine} не найдена инструкция по неисправности "
            f"«{title}»{f' ({problem})' if problem else ''}.\n\n"
            f"Рекомендация на дефектовку: проверка {result.get('session_id')}, {result.get('anomaly_type')} «{title}», "
            f"оценка отклонения {n2(result.get('anomaly_score') or 0)}, точность распознавания "
            f"{n2(result.get('confidence') or 0)}. Оформите дефектовку по порядку автопарка.")


def unverified_cited_lines(text: str, page_text, node: str = "",
                           allowed_numbers: set[str] = frozenset()) -> tuple[str, list[str]]:
    """Строка со ссылкой должна опираться на эту страницу: её числа и большая часть слов есть на ней.

    Иначе это гипотеза, оформленная как цитата руководства (ошибка): строку убираем.
    page_text(doc_id, page) -> текст страницы. Разделы наблюдений, гипотез, источников и ограничений
    не проверяются (в гипотезах ссылки просто снимаются).
    """
    out, removed, section = [], [], ""
    lines = text.split(chr(10))
    block = _block_cites(lines)
    for li, line in enumerate(lines):
        s = line.strip()
        h = re.match(r"^\*\*(.+?)\*\*\s*$", s) or re.match(r"^#+\s*(.+)$", s)
        if h:
            section = h.group(1).lower()
            out.append(line)
            continue
        cites = CITE.findall(s)
        if any(k in section for k in ("наблюд", "слышно", "источник", "огранич", "противореч")) or _ABSENT.search(s):
            out.append(line)  # наблюдения, служебные разделы и «в руководстве этого нет» со страницей не сверяются
            continue
        if not cites:
            # строка списка без своей ссылки: её нормативы сверяем со страницами ссылки блока
            body = _NOT_NORM.sub("", _MODEL.sub("", re.sub(r"^(\d+[.)]|[-•*])\s+", "", s)))
            for x in sorted(allowed_numbers, key=len, reverse=True):
                body = body.replace(x, "")
            nums = _NUM.findall(body)
            if block[li] and nums and "гипотез" not in section:
                pt = _norm(chr(10).join(page_text(d, int(p)) or "" for d, p in block[li]))
                if not all(_norm(n) in pt for n in nums):
                    removed.append(s)
                    continue
            out.append(line)
            continue
        if "гипотез" in section:
            out.append(CITE.sub("", line).rstrip())
            continue
        body = re.sub(r"^(\d+[.)]|[-•*])\s+", "", CITE.sub("", s)).strip()
        texts = [page_text(d, int(p)) or "" for d, p in cites]
        if node:  # ответ по неисправности: проверки и ремонт - только со страниц про этот узел
            texts = [t for t in texts if page_matches_node(t, node)]
        if texts and op_supported(body, chr(10).join(texts)):
            out.append(line)
        else:
            removed.append(s)
    return chr(10).join(out), removed


def recurrence_note(sessions: list[dict], result: dict, tickets: list[dict]) -> str:
    """Тот же тип неисправности уже был у этой машины: факт для наблюдений ответа (считает код, не модель)."""
    code = result.get("anomaly_type")
    if not code or result.get("status") != "anomaly":
        return ""
    prev = [s for s in sessions if s.get("session_id") != result.get("session_id")
            and s.get("status") == "anomaly" and s.get("anomaly_type") == code]
    if not prev:
        return ""
    def day(x):
        d = str(x.get("checked_at", ""))[:10].split("-")
        return f"{d[2]}.{d[1]}" if len(d) == 3 else ""
    when = ", ".join(f"{x['session_id']} от {day(x)}" for x in prev[-3:])
    tk = [t for t in tickets if t.get("payload", {}).get("anomaly_type") == code and t.get("toir_ticket_id")]
    tail = f", заявки по нему: {', '.join(t['toir_ticket_id'] for t in tk[:3])}" if tk else ""
    n = len(prev)
    times = "раз" if n % 10 not in (2, 3, 4) or n % 100 in (12, 13, 14) else "раза"
    return (f"Повторяющийся дефект: {code} у этой машины уже находили {n} {times} "
            f"({'проверка' if n == 1 else 'проверки'} {when}){tail}.")


# Известные противоречия в руководствах (подтверждены заказчиком). Агент не выбирает одну формулировку
# как бесспорную: показывает обе с документами и страницами и рекомендует уточнить у специалиста.
CONFLICTS = [
    {
        "id": "yamz236_valve_clearance",
        "topic": "Условия регулировки тепловых зазоров клапанов",
        "sources": [
            ("yamz236_re", 31, "Тепловые зазоры регулировать на холодном двигателе или менее чем через 15 минут после его остановки."),
            ("yamz236_rk", 269, "Проверить и подрегулировать тепловые зазоры клапанного механизма перед началом и по окончании обкатки, но не ранее, чем через 15 мин после остановки двигателя."),
            ("yamz236_rk", 276, "Проверку регулировки зазоров в клапанном механизме производить на холодном двигателе или не ранее, чем через 15 минут после его остановки."),
        ],
        "decision": "Принято консервативное правило: регулировку тепловых зазоров выполнять на холодном двигателе. "
                    "Формулировка «не ранее чем через 15 минут» сама по себе не подтверждает, что двигатель остыл.",
        # формулировка спорного условия вне блока о противоречии: агент выдал бы её как бесспорную
        "avoid": r"15\s*мин",
        # в работах заявки спорная часть условия убирается, остаётся консервативный вариант
        "strip": r"\s*\(?\s*(,\s*)?(или|либо)?\s*(не ранее|не менее|менее|не позднее)[^.;()]*?15\s*мин\w*[^.;()]*\)?",
        "keep": "на холодном двигателе",
    },
]


def conflicts_for_pages(pages: set[tuple]) -> list[dict]:
    """Противоречия, к которым относится хотя бы одна из найденных страниц."""
    return [c for c in CONFLICTS if any((d, p) in pages for d, p, _ in c["sources"])]


def conflict_block(c: dict) -> str:
    """Блок ответа: обе формулировки со ссылками, отметка о противоречии, что делать."""
    lines = [f"**Противоречие в документации: {c['topic'].lower()}**"]
    for d, p, q in c["sources"]:
        lines.append(f"- {BY_ID[d].title}, стр. PDF {p}: «{q}» [[{d}:{p}]]")
    lines.append(c["decision"])
    lines.append("Какая формулировка верна для вашей модификации двигателя, уточните у специалиста "
                 "или производителя.")
    return chr(10).join(lines)


def disputed_lines(text: str, conflicts: list[dict]) -> tuple[str, list[str]]:
    """Спорное условие вне блока о противоречии и ограничений: оговорка вырезается, остаётся консервативный
    вариант (строка с нормативом не теряется)."""
    pats = [(re.compile(c["avoid"], re.I), c) for c in conflicts if c.get("avoid")]
    if not pats:
        return text, []
    out, changed, section = [], [], ""
    for line in text.split(chr(10)):
        s = line.strip()
        h = re.match(r"^\*\*(.+?)\*\*\s*$", s)
        if h:
            section = h.group(1).lower()
        hit = next((c for p, c in pats if p.search(s)), None)
        if h or not hit or any(k in section for k in ("противореч", "огранич")):
            out.append(line)
            continue
        new = re.sub(hit["strip"], "", line, flags=re.I)
        if re.search(hit["avoid"], new, re.I):  # оговорка в другой форме - строку убираем
            changed.append(s)
            continue
        if hit["keep"] not in new.lower():
            new = new.rstrip().rstrip(".") + f" ({hit['keep']})."
        out.append(new)
        changed.append(s)
    return chr(10).join(out), changed


def resolve_disputed_ops(ops: list[str], conflicts: list[dict]) -> tuple[list[str], list[str]]:
    """Работы заявки однозначны: спорную часть условия заменяем консервативным вариантом."""
    out, changed = [], []
    for o in ops:
        new = o
        for c in conflicts:
            if c.get("avoid") and re.search(c["avoid"], new, re.I):
                new = re.sub(c["strip"], "", new, flags=re.I).strip()
                if c["keep"] not in new.lower():
                    new = new.rstrip(".") + f" ({c['keep']})"
        out.append(new)
        if new != o:
            changed.append(f"{o} -> {new}")
    return out, changed


def drop_empty_sections(text: str) -> str:
    """Убирает заголовки разделов, под которыми после проверок ничего не осталось."""
    lines = text.split(chr(10))
    head = lambda x: bool(re.match(r"^\s*(\*\*[^*]+\*\*|#+\s.+)\s*$", x))  # noqa: E731
    out = []
    for i, line in enumerate(lines):
        if head(line):
            rest = lines[i + 1:]
            nxt = next((x for x in rest if x.strip()), None)
            if nxt is None or head(nxt):
                continue
        out.append(line)
    return re.sub(r"\n{3,}", chr(10) * 2, chr(10).join(out)).strip()


def is_ban(line: str) -> bool:
    return bool(_BAN.search(line))


def release_recommendation(result: dict) -> str:
    """Рекомендация о выпуске на линию по приоритету (считает код): это не запрет, решение за механиком."""
    if result.get("status") != "anomaly" or is_no_ticket(result):
        return ""
    urg, _ = urgency_reason(result)
    return (f"Рекомендация: {URGENCY_MEANING[urg]} (приоритет {PRIORITY_RU[urg]}). Прямого запрета эксплуатации "
            "в руководстве на этот двигатель не найдено, решение о выпуске принимает механик.")


_BETTER = re.compile(r"(стало|состояние)\s+лучше|улучш", re.I)
_WORSE = re.compile(r"(стало|состояние)\s+хуже|ухудш", re.I)


def trend_fact(sessions: list[dict]) -> tuple[str, str] | None:
    """Сравнение последней проверки с прошлой по оценке отклонения (считает код): (направление, фраза)."""
    ok = [s for s in sessions if s.get("status") in ("normal", "anomaly") and s.get("anomaly_score") is not None]
    if len(ok) < 2:
        return None
    a, b = ok[-2], ok[-1]
    da, db = str(a.get("checked_at", ""))[:10].split("-"), str(b.get("checked_at", ""))[:10].split("-")
    day = lambda d: f"{d[2]}.{d[1]}" if len(d) == 3 else ""  # noqa: E731
    sa, sb = a["anomaly_score"], b["anomaly_score"]
    word = "хуже" if sb > sa + 0.02 else "лучше" if sb < sa - 0.02 else "без изменений"
    what = lambda s: (f"отклонение «{s.get('anomaly_title') or s.get('anomaly_type')}»"  # noqa: E731
                      if s["status"] == "anomaly" else "норма")
    return word, (f"По сравнению с прошлой проверкой стало {word}: {day(da)} - {what(a)}, оценка {n2(sa)}; "
                  f"{day(db)} - {what(b)}, оценка {n2(sb)}." if word != "без изменений" else
                  f"По сравнению с прошлой проверкой без заметных изменений: оценка {n2(sa)} и {n2(sb)}.")


def trend_claim_wrong(text: str, sessions: list[dict]) -> bool:
    """Ответ утверждает «стало лучше», а по данным хуже (или наоборот)."""
    t = trend_fact(sessions)
    if not t:
        return False
    better, worse = bool(_BETTER.search(text)), bool(_WORSE.search(text))
    if better == worse:
        return False
    return (t[0] == "хуже" and better) or (t[0] == "лучше" and worse)


_HYP_HEAD = re.compile(r"^\*\*(Гипотезы|Вероятные причины|Возможные причины|Гипотезы о причинах)[^*]*\*\*\s*$", re.I | re.M)


def mark_hypotheses(text: str) -> str:
    """Заголовок раздела гипотез всегда с пометкой, что это не из руководства (требование заказчика)."""
    return _HYP_HEAD.sub("**Гипотезы (не из руководства)**", text)



def part_fits_works(part: str, ops: list[str]) -> bool:
    """Запчасть нужна для работ заявки: основа слова из её названия встречается в работах
    (втулка коромысла -> «заменить втулки коромысел»). Обозначения и короткие слова не считаются."""
    words = {w for w in _stems(re.sub(r"\d[\d.\-]*\S*", " ", part)) if len(w) >= 4}
    work = _stems(" ".join(ops))
    return not words or len(words & work) / len(words) >= 0.66  # «втулка толкателя» не подходит к работам по коромыслам


# Служебные слова ML по-английски механику не показываем (требование заказчика): заменяем русскими
_EN_TERMS = [(r"anomaly[_ ]score|score", "оценка отклонения"), (r"confidence", "точность распознавания"),
             (r"signal[_ ]quality", "качество записи"), (r"check[_ ]failed", "сбой проверки"),
             (r"anomaly", "отклонение"), (r"normal", "норма")]


def russify_terms(text: str) -> tuple[str, int]:
    n = 0
    for pat, ru in _EN_TERMS:
        text, k = re.subn(rf"\b{pat}\b", ru, text, flags=re.I)
        n += k
    return text, n


# «По руководству», «согласно руководству» без ссылки на страницу - гипотеза, выданная за руководство
_MANUAL_CLAIM = re.compile(r"(по|согласно|в соответствии с)\s+руководств|руководств\w*\s+(указан|сказан|предписыва|требует|рекоменду)|"
                           r"(указан|сказан|написан|приведен)\w*\s+в\s+руководств", re.I)
_NO_MANUAL = re.compile(r"(в руководств\w*[^.]{0,40}(нет|не\s))|(не\s[^.]{0,20}в руководств)", re.I)

DIAG_ORDER = ["наблюдени", "гипотез", "проверк", "возможный ремонт", "источник", "огранич"]


def enforce_structure(text: str, result: dict, refs: list[dict]) -> tuple[str, list[str]]:
    """Ответ на результат диагностики - в порядке ТЗ: наблюдения, гипотезы, проверки, возможный ремонт,
    источник, ограничения. Недостающие наблюдения, источник и ограничения код добавляет сам."""
    parts, cur, head_txt = {}, None, {}
    pre = []
    for line in text.split(chr(10)):
        m = re.match(r"^\*\*(.+?)\*\*\s*$", line.strip())
        key = next((k for k in DIAG_ORDER if m and k in m.group(1).lower()), None)
        if key:
            cur = key
            head_txt[key] = line.strip()
            parts.setdefault(key, [])
            continue
        (parts[cur] if cur else pre).append(line)
    added = []
    if "наблюдени" not in parts:
        parts["наблюдени"] = [f"- Проверка {result.get('session_id')}: отклонение «{result.get('anomaly_title')}», "
                              f"оценка отклонения {n2(result.get('anomaly_score') or 0)}, точность распознавания "
                              f"{n2(result.get('confidence') or 0)}."] + ([f"- {result['description']}"] if result.get("description") else [])
        head_txt["наблюдени"] = "**Наблюдения**"
        added.append("наблюдения")
    if "источник" not in parts and refs:
        parts["источник"] = [" ".join(f"[[{r['doc_id']}:{r['page']}]]" for r in refs)]
        head_txt["источник"] = "**Источник**"
        added.append("источник")
    if "огранич" not in parts:
        parts["огранич"] = ["- Вывод сделан по звуку двигателя; окончательное решение принимает механик после дефектовки."]
        head_txt["огранич"] = "**Ограничения**"
        added.append("ограничения")
    out = [x for x in pre if x.strip()]
    for k in DIAG_ORDER:
        if k in parts:
            body = [x for x in parts[k]]
            while body and not body[-1].strip():
                body.pop()
            if out:
                out.append("")
            out += [head_txt[k]] + body
    return chr(10).join(out).strip(), added


def looks_offtopic(text: str, tool_calls: int, refs: list, vehicle: dict) -> bool:
    """Ответ без инструментов, без ссылок и без данных машины, длиннее одной фразы - это не по технике
    (стихи, анекдоты, общие темы): механику показываем стандартную фразу."""
    if tool_calls or refs or len(text) < 40:
        return False
    vt = " ".join(str(vehicle.get(k) or "") for k in ("vehicle_id", "engine_model", "model", "garage_no", "make")).lower()
    words = [w for w in re.findall(r"[\w-]{3,}", vt)]
    tech = re.compile(r"машин|техник|механик|двигател|масл|клапан|зазор|давлен|ремонт|заявк|проверк|дефект|руководств|узел|агрегат|охлажд|топлив", re.I)
    return not tech.search(text) and not any(w in text.lower() for w in words)



def attach_citations(text: str, pages: dict, allowed_numbers: set[str] = frozenset()) -> tuple[str, list[str]]:
    """Блок с нормативами без ссылки: если все его числа и большая часть слов есть на одной из страниц,
    найденных поиском в этом запросе, код ставит ссылку на неё (источник подтверждён дословно).
    pages: {(doc_id, page): текст}. Блоки, для которых страница не нашлась, остаются как есть (их уберёт проверка)."""
    lines = text.split(chr(10))
    block = _block_cites(lines)
    attached, i = [], 0
    while i < len(lines):
        st = lines[i].strip()
        if not st or block[i] or re.match(r"^(\*\*.+\*\*|#+\s.+)$", st):
            i += 1
            continue
        j = i
        while j + 1 < len(lines) and lines[j + 1].strip() and not block[j + 1] \
                and not re.match(r"^(\*\*.+\*\*|#+\s.+)$", lines[j + 1].strip()):
            j += 1
        body = " ".join(lines[i:j + 1])
        clean = _NOT_NORM.sub("", _MODEL.sub("", body))
        for x in sorted(allowed_numbers, key=len, reverse=True):
            clean = clean.replace(x, "")
        nums = _NUM.findall(clean)
        if nums:
            for (d, p), t in pages.items():
                pt = _norm(t)
                if all(_norm(n) in pt for n in nums) and op_supported(body, t):
                    lines[j] = lines[j].rstrip() + f" [[{d}:{p}]]"
                    attached.append(f"{d}:{p}")
                    break
        i = j + 1
    return chr(10).join(lines), attached


# Процедура узла для поиска (раздел руководства, где обычно нормативы и порядок работ)
PROCEDURE_HINT = {
    "F01": "регулировка тепловых зазоров клапанов", "F02": "давление масла манометр неисправности системы смазки",
    "F03": "температура охлаждающей жидкости выше нормы термостат", "F04": "натяжение ремня прогиб",
    "F05": "генератор неисправности натяжение ремня", "F06": "сцепление регулировка свободный ход",
    "F07": "коробка передач неисправности", "F08": "форсунки давление впрыскивания топливный насос",
    "F09": "турбокомпрессор неисправности", "F10": "компрессор пневмосистема насос гидроусилителя",
    "F11": "клапан рециркуляции проверка", "F12": "свечи зажигания зазор катушка",
    "F13": "бензонасос проверка карбюратор жиклеры", "F14": "цепь гидронатяжитель успокоитель замена",
}


def engine_family(name: str) -> str:
    """Семейство двигателя по номеру модели: ЯМЗ-236М2 -> 236, ЗМЗ-40905.10 -> 409, ЗМЗ-5234.10 -> 523."""
    m = re.search(r"(\d{3})", name or "")
    return m.group(1) if m else ""


def page_for_other_engine(page_text: str, engine_model: str) -> bool:
    """Страница общего руководства прямо про другие модели (ЯМЗ-238 в руководстве ЯМЗ-236/238):
    упомянуты модели, и среди них нет семейства двигателя этой машины. Норматив оттуда - ошибка."""
    fam = engine_family(engine_model)
    # ЯМЗ/ЗМЗ и ошибки распознавания страниц-картинок (AM3, ЯM3)
    found = [m[:3] for m in re.findall(r"(?:ЯМЗ|ЗМЗ|[AЯ][MМ][3З])\s*[-–]?\s*(\d{3,5})", page_text or "")]
    # разовая оговорка «для двигателя ЯМЗ-238КМ2 …» на общей странице не делает её чужой: нужно 3+ упоминания
    return bool(fam and found and fam not in found and len(found) >= 3)


def page_usable(doc_id: str, page: int, engine_model: str) -> bool:
    """Страницу можно приводить как источник: не оглавление и не про другую модель двигателя."""
    from app.rag import page_text
    t = page_text(doc_id, page) or ""
    return not is_toc_page(t) and not page_for_other_engine(t, engine_model)


_META = re.compile(r"^\s*[-•*]?\s*(Черновик заявки (подготовлен|показан|готов|сформирован)|Механик (видит|отправит)|"
                   r"Отправьте (заявку|черновик))", re.I)
WORK_SECTIONS = ("проверк", "возможный ремонт")


def label_uncited_works(text: str, pages: dict) -> tuple[str, list[str], list[str]]:
    """Разбор по отклонению: работа в «Проверках» или «Ремонте» без ссылки.
    Если она подтверждается страницей из поиска - ссылку ставит код; иначе помечается, что это не из руководства.
    Служебные фразы о черновике заявки убираются."""
    lines = text.split(chr(10))
    block = _block_cites(lines)
    out, attached, labeled, section = [], [], [], ""
    for i, line in enumerate(lines):
        s = line.strip()
        h = re.match(r"^\*\*(.+?)\*\*\s*$", s)
        if h:
            section = h.group(1).lower()
            out.append(line)
            continue
        if _META.match(s):
            continue
        if not s or CITE.search(s) or block[i] or not any(k in section for k in WORK_SECTIONS) or s.endswith(":"):
            out.append(line)
            continue
        best, share = None, 0.0
        words = {w for w in _stems(s) if len(w) >= 4 and not w[:1].isdigit() and not w.startswith(_GENERIC)}
        for key, t in pages.items():
            if words:
                sh = len(words & _stems(t)) / len(words)
                if sh > share:
                    best, share = key, sh
        if best and share >= 0.6 and op_supported(s, pages[best]):
            out.append(line.rstrip() + f" [[{best[0]}:{best[1]}]]")
            attached.append(f"{best[0]}:{best[1]}")
        else:
            out.append(line.rstrip().rstrip(".") + " (по опыту, в руководстве не найдено).")
            labeled.append(s)
    return chr(10).join(out), attached, labeled


def clean_part(name: str) -> str:
    """Запчасть из строки таблицы руководства: без разделителей и колонок количества, обозначения без повторов."""
    t = re.sub(r"\s*\|\s*", " ", name)
    t = re.sub(r"(?<=\s)\d{1,2}(?=\s|$)", " ", t)  # колонки «количество»
    seen, words = set(), []
    for w in t.split():
        if w in seen and re.match(r"\d", w):
            continue
        seen.add(w)
        words.append(w)
    return re.sub(r"\s+", " ", " ".join(words)).strip(" ;,")
