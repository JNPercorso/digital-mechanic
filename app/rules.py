"""Жёсткие правила заказчика, которые проверяет код, а не только промпт.

- Сбой проверки: диагноз не ставится, модель не вызывается, заявки нет (F3, С3).
- Ссылки: только документы двигателя этой машины и только страницы, найденные поиском
  в этом же запросе (F4: норматив от другого двигателя - ошибка, выдуманных страниц нет).
- Низкая уверенность ML: вывод помечается предварительным.
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


def filter_citations(text: str, engine_model: str, seen_pages: set[tuple]) -> tuple[str, list[dict], list[str]]:
    """Оставляет в тексте только допустимые ссылки [[doc_id:page]].

    Возвращает (текст, список ссылок, список отклонённых ссылок для журнала).
    """
    allowed = allowed_doc_ids(engine_model)
    refs, rejected = [], []

    def repl(m: re.Match) -> str:
        doc_id, page = m.group(1), int(m.group(2))
        if doc_id in allowed and (doc_id, page) in seen_pages:
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


def urgency_reason(result: dict) -> tuple[str, str]:
    """Приоритет заявки по полям ML - считает код, модель только объясняет.

    high: оценка отклонения >= 0.8 при уверенности >= LOW_CONFIDENCE;
    medium: остальные отклонения; low: вывод предварительный (низкая уверенность).
    """
    score, conf = result.get("anomaly_score") or 0, result.get("confidence") or 0
    if is_low_confidence(result):
        return "low", (f"Вывод предварительный: точность распознавания {conf:.2f}"
                       f"{', запись с помехами' if result.get('signal_quality') == 'warning' else ''}"
                       " - нужна повторная проверка")
    if score >= 0.8:
        return "high", f"Сильное отклонение звука от нормы: оценка {score:.2f} при точности {conf:.2f}"
    return "medium", f"Отклонение от нормы: оценка {score:.2f} при точности {conf:.2f}"
