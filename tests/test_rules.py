"""Автотесты правил, которые проверяет код (pytest tests/test_rules.py)."""
from app import rules

ANOMALY = {"session_id": "S-1", "status": "anomaly", "anomaly_type": "F02", "anomaly_title": "Низкое давление масла",
           "anomaly_score": 0.7, "confidence": 0.9, "signal_quality": "good"}


# ---------- ссылки ----------
def test_citations_all_formats():
    cases = {
        "[[yamz536_re:173]]": "[[yamz536_re:173]]",
        "[[yamz536_re:173, 215]]": "[[yamz536_re:173]] [[yamz536_re:215]]",
        "[[yamz536_re:173,215]]": "[[yamz536_re:173]] [[yamz536_re:215]]",
        "[[yamz536_re:208; yamz536_rk:20]]": "[[yamz536_re:208]] [[yamz536_rk:20]]",
        "[[yamz536_re:173-175]]": "[[yamz536_re:173]] [[yamz536_re:174]] [[yamz536_re:175]]",
        "[[yamz536_re стр. 173]]": "[[yamz536_re:173]]",
        "[[yamz536_re, стр. 9]]": "[[yamz536_re:9]]",
        "[[YAMZ536_RE:9]]": "[[yamz536_re:9]]",
    }
    for raw, want in cases.items():
        text, unparsed = rules.split_citations(raw)
        assert text == want and not unparsed, raw


def test_unparsed_citation_is_reported_not_silently_lost():
    text, unparsed = rules.split_citations("см. [[Руководство, раздел 5]]")
    assert unparsed == ["[[Руководство, раздел 5]]"]
    _, refs, rejected = rules.filter_citations("см. [[Руководство, раздел 5]]", "ЯМЗ-536", set())
    assert any("нераспознано" in r for r in rejected)


def test_citation_only_from_search_and_engine():
    seen = {("yamz536_re", 173)}
    text, refs, rejected = rules.filter_citations(
        "А [[yamz536_re:173]] Б [[yamz536_re:174]] В [[yamz236_re:31]]", "ЯМЗ-536", seen)
    assert [r["page"] for r in refs] == [173]
    assert "yamz536_re:174" in rejected and "yamz236_re:31" in rejected
    assert "[[yamz536_re:174]]" not in text and "[[yamz236_re:31]]" not in text


# ---------- точность распознавания ----------
def test_bad_quality_is_check_failed():
    assert rules.is_check_failed({**ANOMALY, "signal_quality": "bad"})
    assert rules.warning_text({**ANOMALY, "signal_quality": "bad"}) is None  # отдельный ответ «сбой проверки»


def test_confidence_thresholds():
    assert not rules.is_low_confidence(ANOMALY) and rules.warning_text(ANOMALY) is None
    mid = {**ANOMALY, "confidence": 0.55}
    assert rules.is_low_confidence(mid) and not rules.is_no_ticket(mid)
    assert "подтверждения" in rules.warning_text(mid)
    low = {**ANOMALY, "confidence": 0.48}
    assert rules.is_no_ticket(low) and "не формируется" in rules.warning_text(low)


def test_warning_signal_always_warned():
    w = {**ANOMALY, "signal_quality": "warning"}
    assert rules.is_low_confidence(w) and "помехами" in rules.warning_text(w)


# ---------- приоритет ----------
def test_urgency_base_plus_bounded_adjustment():
    assert rules.urgency_reason(ANOMALY)[0] == "high"                                  # F02 база high
    assert rules.urgency_reason({**ANOMALY, "anomaly_score": 0.5})[0] == "medium"      # минус один уровень
    f05 = {**ANOMALY, "anomaly_type": "F05", "anomaly_title": "Генератор"}
    assert rules.urgency_reason(f05)[0] == "low"
    assert rules.urgency_reason({**f05, "anomaly_score": 0.95})[0] == "medium"         # плюс один, не больше
    assert rules.urgency_reason({**f05, "anomaly_score": 0.3})[0] == "low"             # ниже low не бывает
    assert "до выпуска в рейс" in rules.urgency_reason(ANOMALY)[1]


# ---------- применимость страницы и цитаты ----------
def test_page_node_and_toc():
    page = "Проверка давления масла контрольным манометром. Давление масла должно быть 0,4 МПа."
    assert rules.page_matches_node(page, "F02")
    assert not rules.page_matches_node("Регулировка ремня генератора", "F02")
    toc = "\n".join(["СОДЕРЖАНИЕ"] + [f"Раздел {i} ............ {i * 7}" for i in range(1, 12)])
    assert rules.is_toc_page(toc) and not rules.is_toc_page(page)


def test_quote_must_be_on_page():
    page = "Величины зазоров между чашкой коромысла и торцом\nтраверсы клапанов должны быть: 0,30 – 0,40 мм"
    assert rules.quote_on_page("Величины зазоров между чашкой коромысла и торцом траверсы клапанов", page)
    assert not rules.quote_on_page("Зазор клапанов должен быть 0,25 мм на прогретом двигателе", page)


# ---------- числа без ссылки, работы в заявке ----------
def test_numbers_need_citation():
    text = ("**Наблюдения**\nОценка отклонения 0,72\n**Проверки**\n1. Зазор 0,30 мм\n"
            "2. Зазор 0,40 мм [[yamz536_re:173]]\n3. Снять крышку (рисунок 46)")
    out, removed = rules.unsupported_lines(text, {"0,72"})  # 0,72 - из результата проверки ML
    assert removed == ["1. Зазор 0,30 мм"]
    assert "Оценка отклонения 0,72" in out and "рисунок 46" in out
    # норматив в «Наблюдениях» без ссылки тоже убирается; номер модели двигателя нормативом не считается
    out, removed = rules.unsupported_lines("**Наблюдения**\nЭФУ обеспечивает пуск до -22 °C\nДвигатель ЯМЗ-236М2")
    assert removed == ["ЭФУ обеспечивает пуск до -22 °C"]


def test_operations_must_come_from_pages():
    pages = "Регулировку тепловых зазоров проводить на холодном двигателе. Для впускных клапанов: 0,30 – 0,40 мм"
    assert rules.op_supported("Отрегулировать тепловые зазоры впускных клапанов 0,30-0,40 мм", pages)
    assert not rules.op_supported("Отрегулировать зазоры 0,25 мм", pages)
    assert not rules.op_supported("Заменить турбокомпрессор в сборе", pages)


# ---------- противоречия в документации ----------
def test_conflict_block_shows_all_sources():
    seen = {("yamz236_re", 31)}
    found = rules.conflicts_for_pages(seen)
    assert [c["id"] for c in found] == ["yamz236_valve_clearance"]
    block = rules.conflict_block(found[0])
    for d, p, _ in found[0]["sources"]:
        assert f"[[{d}:{p}]]" in block
    assert "холодном двигателе" in block and "уточните у специалиста" in block
    seen_all = {(d, p) for d, p, _ in found[0]["sources"]}
    text, refs, rejected = rules.filter_citations(block, "ЯМЗ-236М2", seen_all)
    assert len(refs) == 3 and not rejected
    out, removed = rules.unsupported_lines(text)
    assert not removed  # «15 минут» в блоке противоречия - цитата, не норматив без ссылки


def test_disputed_condition_not_presented_as_fact():
    t = ("**Проверки**\n- Регулировать не ранее чем через 15 минут после остановки [[yamz236_rk:276]]\n"
         "- Щупом проверить зазор [[yamz236_rk:43]]\n**Ограничения**\nВ руководствах разночтение про 15 минут")
    out, removed = rules.disputed_lines(t, rules.CONFLICTS)
    assert len(removed) == 1 and "Щупом" in out and "разночтение" in out


def test_ticket_operations_use_conservative_condition():
    ops, changed = rules.resolve_disputed_ops(
        ["Отрегулировать тепловые зазоры на холодном двигателе или не ранее чем через 15 минут после остановки.",
         "Проверить затяжку гаек стоек коромысел"], rules.CONFLICTS)
    assert ops[0] == "Отрегулировать тепловые зазоры на холодном двигателе." and len(changed) == 1


def test_release_ban_only_with_manual_reference():
    out, removed = rules.unsupported_lines("Выпускать на линию нельзя: нужна дефектовка.\nНельзя выпускать в рейс [[yamz236_re:17]]")
    assert removed == ["Выпускать на линию нельзя: нужна дефектовка."]  # запрет без ссылки убран, со ссылкой остался
    rec = rules.release_recommendation(ANOMALY)
    assert "до выпуска в рейс" in rec and "решение о выпуске принимает механик" in rec


def test_trend_claim_checked_against_history():
    sessions = [{"session_id": "S-1", "checked_at": "2026-10-31", "status": "normal", "anomaly_score": 0.12},
                {"session_id": "S-2", "checked_at": "2026-11-15", "status": "anomaly", "anomaly_score": 0.85,
                 "anomaly_title": "Низкое давление масла"}]
    assert rules.trend_claim_wrong("Стало лучше: оценка снизилась", sessions)
    assert not rules.trend_claim_wrong("Стало хуже: оценка выросла", sessions)
    assert "стало хуже" in rules.trend_fact(sessions)[1]


def test_hypotheses_heading_always_marked():
    t = rules.mark_hypotheses("**Наблюдения**\nx\n**Гипотезы**\n- износ\n**Вероятные причины**\n- y")
    assert t.count("**Гипотезы (не из руководства)**") == 2


def test_parts_must_fit_ticket_works():
    ops = ["Осмотреть коромысла и штанги; при износе заменить втулки коромысел"]
    assert rules.part_fits_works("Втулка коромысла 236-1007118-В", ops)
    assert not rules.part_fits_works("Втулка толкателя 236-1007186", ops)
