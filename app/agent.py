"""Агент «Цифровой механик»: модель сама выбирает инструменты (F5), код следит за правилами.

Инструменты модели: результат проверки (ML), история машины, поиск по руководству (RAG),
черновик заявки. Отправка заявки в ТОиР - только кнопкой механика, модель её не вызывает.
Все шаги идут событиями в интерфейс (SSE): step, warning, answer, ticket_draft, metrics, done.
"""
import json
import re
import time
import uuid
from typing import AsyncIterator

from app import llm, rag, rules, services, store
from app.manuals import manuals_for_engine

MAX_TOOL_CALLS = 6

SYSTEM = """Ты - «Цифровой механик», помощник механика автопарка. Отвечаешь по-русски, коротко и просто, без жаргона ML.
Машина: {vehicle}. Двигатель: {engine}. Руководства этого двигателя: {manuals}.

Жёсткие правила:
1. Любые нормативы, моменты затяжки, зазоры, давления, обороты, скорости, порядок работ и номера деталей бери ТОЛЬКО из фрагментов, которые search_manual вернул В ЭТОМ запросе. Ссылки из прошлых сообщений диалога не действуют - ищи заново. Своих цифр не придумывай: цифра без ссылки на страницу запрещена.
2. После каждого утверждения из руководства ставь ссылку строго в виде [[doc_id:страница]], например [[yamz236_rk:96]]. Одна ссылка - одна страница: несколько страниц пиши подряд [[yamz536_re:173]] [[yamz536_re:215]]. Ссылаться можно только на страницы из результатов search_manual.
3. Если в найденных фрагментах ответа нет - так и скажи: «В руководстве на этот двигатель этого нет» и рекомендуй дефектовку. Не бери данные с другого двигателя.
4. Ты помощник: окончательное решение о двигателе принимает механик.
5. Отвечай только на заданный вопрос, не добавляй сведения, о которых не спрашивали.
6. Не используй английские термины (confidence, score, anomaly): говори «точность распознавания», «оценка отклонения»; дробные числа пиши через запятую.
7. Результат проверки, который уже есть в сообщении, повторно не запрашивай.
8. Ищи в руководстве конкретными словами (узел, симптом, операция), при необходимости сделай 2-3 разных поиска.

Ответ на результат проверки с отклонением - в таком порядке, с заголовками:
**Наблюдения** - что показала проверка: признаки, оценка отклонения, точность распознавания.
**Гипотезы (не из руководства)** - 2-3 вероятные причины и чем грозит езда дальше. Это твои инженерные предположения: без чисел, без ссылок, не выдавай их за текст руководства.
**Проверки** - пошагово по руководству, с нормативами и ссылкой после каждого пункта.
**Возможный ремонт** - работы, инструмент, материалы и запчасти по руководству (с каталожными номерами, если они есть), со ссылками.
**Источник** - список страниц руководства, на которые опирается ответ, ссылками [[doc_id:страница]].
**Ограничения** - чего в руководстве нет или что требует проверки на месте.
Запрет эксплуатации или выпуска в рейс пиши только если на найденной странице руководства прямо сказано, что эксплуатация запрещена, и ставь ссылку на неё; иначе это рекомендация.
Сначала найди процедуру проверки, регулировки или ремонта именно этого узла, затем вызови draft_ticket и после этого дай ответ механику. В draft_ticket к каждой странице приложи дословную цитату с неё (одно-два предложения), работы бери только с этих страниц. Оглавление и общие разделы, где узел не упомянут, не годятся. Если подходящей процедуры нет - draft_ticket не вызывай и скажи, что в руководстве на этот двигатель нет инструкции по этой неисправности. Номер черновика не упоминай: механик видит черновик рядом с ответом.

На вопрос механика отвечай по той же логике, но только нужными пунктами; источник обязателен."""

FUNCTIONS = [
    {"name": "get_check_result",
     "description": "Результат акустической проверки двигателя из ML-сервиса: статус, тип неисправности, оценка отклонения, точность, качество записи. Без session_id - последняя проверка.",
     "parameters": {"type": "object", "properties": {
         "session_id": {"type": "string", "description": "Номер проверки, например S-0005"}}}},
    {"name": "get_vehicle_history",
     "description": "История проверок этой машины (от ранней к поздней) и ранее созданные заявки. Нужна для сравнения и вопросов о динамике.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "search_manual",
     "description": "Поиск по руководствам двигателя ЭТОЙ машины. Возвращает фрагменты с doc_id и номером страницы PDF.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Что искать: узел, симптом, операция, норматив"}},
         "required": ["query"]}},
    {"name": "draft_ticket",
     "description": "Подготовить черновик заявки на дефектовку (НЕ отправляет). Только для проверки с отклонением.",
     "parameters": {"type": "object", "properties": {
         "conclusion": {"type": "string", "description": "Диагноз одной-двумя фразами"},
         "operations": {"type": "array", "items": {"type": "string"},
                        "description": "Работы по дефектовке/ремонту по руководству"},
         "spare_parts": {"type": "array", "items": {"type": "string"},
                         "description": "Запчасти с каталожными номерами из руководства; пусто, если не нашёл"},
         "references": {"type": "array", "items": {"type": "object", "properties": {
             "doc_id": {"type": "string"}, "page": {"type": "integer"},
             "quote": {"type": "string", "description": "Дословная цитата с этой страницы: процедура или норматив по узлу"}},
             "required": ["doc_id", "page", "quote"]},
             "description": "Страницы с процедурой по узлу неисправности, на которых основаны работы"}},
         "required": ["conclusion", "operations", "references"]}},
]


def _vehicle_line(v: dict) -> str:
    return f"{v.get('make')} {v.get('model') or ''} госномер {v['vehicle_id']}, гаражный {v.get('garage_no')}".replace("  ", " ")


def _short_result(r: dict) -> dict:
    keys = ("session_id", "status", "anomaly_type", "anomaly_title", "anomaly_score", "confidence",
            "signal_quality", "description")
    return {k: r.get(k) for k in keys}


class Run:
    """Один запрос механика: состояние для правил (что нашёл поиск, какая проверка)."""

    def __init__(self, vehicle_id: str, require_search: bool = False):
        self.require_search = require_search  # вопрос механика: ответ только после поиска по руководству
        self.nudged = False
        self.id = uuid.uuid4().hex[:12]
        self.v = services.vehicle(vehicle_id)
        self.engine = self.v["engine_model"]
        self.seen_pages: set[tuple] = set()
        self.page_queries: dict[tuple, str] = {}  # страница -> запрос, которым её нашли (для подсветки в окне страницы)
        self.result: dict | None = None   # последний полученный результат проверки
        self.node = ""                    # тип неисправности, по которому отвечаем (кнопка диагностики)
        self.draft_nudged = False
        self.recur = ""                   # повтор того же дефекта в истории машины (строка для наблюдений)
        self.drafted = False              # черновик заявки создан в этом запросе
        self.draft_problem = ""           # почему черновик не принят (для ответа механику)
        self.warned: set[str] = set()     # проверки, по которым предупреждение о качестве уже показано
        self.tool_calls = 0
        self.llm_calls = 0
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.t0 = time.time()
        self.events: list[dict] = []

    def _tag(self, refs: list[dict]) -> None:
        """Каждой ссылке - поисковый запрос, которым нашли страницу: запасной вариант для подсветки."""
        for r in refs:
            r["sq"] = self.page_queries.get((r["doc_id"], r["page"]), "")

    def ml_numbers(self) -> set[str]:
        """Числа из результата проверки (оценка, точность) в том виде, как их пишет модель."""
        out = set()
        for k in ("anomaly_score", "confidence"):
            x = (self.result or {}).get(k)
            if isinstance(x, (int, float)):
                out |= {f"{x:.2f}".replace(".", ","), f"{x:.2f}", f"{x:g}".replace(".", ","), f"{x:g}"}
        return out

    def quality_warning(self) -> list[dict]:
        """Предупреждение о помехах или низкой точности - всегда, но один раз на проверку."""
        t = rules.warning_text(self.result or {})
        if not t or self.result["session_id"] in self.warned:
            return []
        self.warned.add(self.result["session_id"])
        return [self.ev("warning", text=t)]

    def ev(self, kind: str, **data) -> dict:
        e = {"type": kind, **data}
        store.log_event(kind, data, request_id=self.id, vehicle_id=self.v["vehicle_id"])
        return e

    def metrics(self) -> dict:
        return {"type": "metrics", "request_id": self.id, "seconds": round(time.time() - self.t0, 1),
                "llm_calls": self.llm_calls, "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "cost_rub": round(self.cost, 4)}

    # ---------- инструменты ----------

    async def tool(self, name: str, args: dict) -> tuple[dict, list[dict]]:
        """Выполняет инструмент. Возвращает (результат для модели, события для интерфейса)."""
        self.tool_calls += 1
        evs = []
        if name == "get_check_result":
            sid = args.get("session_id") or services.latest_session(self.v["vehicle_id"])["session_id"]
            if self.result and self.result.get("session_id") == sid:
                # Результат этой проверки уже получен - повторно в ML-сервис не ходим
                return _short_result(self.result), evs
            evs.append(self.ev("step", text=f"Запрашиваю результат проверки {sid}", tool=name))
            try:
                self.result = await services.diagnose(self.v["vehicle_id"], sid)
            except services.ServiceError as e:
                return {"error": str(e)}, evs
            out = _short_result(self.result)
            evs += self.quality_warning()
            if rules.is_check_failed(self.result):
                out["rule"] = "Сбой проверки: о двигателе выводов не делать, попросить повторить запись."
            elif rules.is_no_ticket(self.result):
                out["rule"] = "Точность ниже порога: вывод ненадёжен, заявку не делать, попросить повторить запись."
            elif rules.is_low_confidence(self.result):
                out["rule"] = "Низкая точность распознавания: вывод только предварительный, нужна повторная проверка."
            return out, evs
        if name == "get_vehicle_history":
            evs.append(self.ev("step", text="Смотрю историю проверок и заявок машины", tool=name))
            hist = [{**_short_result(s), "checked_at": s["checked_at"]} for s in self.v["sessions"]]
            tickets = [{"ticket_id": t["toir_ticket_id"], "anomaly_type": t["payload"]["anomaly_type"],
                        "conclusion": t["payload"]["conclusion"], "urgency": t["payload"]["urgency"]}
                       for t in store.tickets_for(self.v["vehicle_id"])]
            return {"checks": hist, "tickets": tickets}, evs
        if name == "search_manual":
            q = (args.get("query") or "").strip()
            evs.append(self.ev("step", text=f"Ищу в руководстве {self.engine}: «{q}»", tool=name))
            if args.get("node_first") and self.result:
                # Первый поиск по неисправности: в запросе слова узла, страницы про узел - первыми
                hits = rag.search(rules.node_query(self.result), self.engine, k=10)
                code = self.result.get("anomaly_type")
                hits = sorted(hits, key=lambda h: not rules.page_matches_node(h["text"], code))[:5]
            else:
                hits = rag.search(q, self.engine, k=5)
            for h in hits:
                self.seen_pages.add((h["doc_id"], h["page"]))
                self.page_queries.setdefault((h["doc_id"], h["page"]), q)
            if not hits:
                return {"found": [], "note": "В руководствах этого двигателя ничего не найдено"}, evs
            return {"found": [{"doc_id": h["doc_id"], "manual": h["manual"], "page": h["page"],
                               "text": h["text"]} for h in hits]}, evs
        if name == "draft_ticket":
            return await self._draft(args, evs)
        return {"error": f"нет инструмента {name}"}, evs

    async def _draft(self, args: dict, evs: list) -> tuple[dict, list]:
        r = self.result
        if not r or r.get("status") != "anomaly":
            return {"error": "Заявка на дефектовку формируется только по проверке с отклонением"}, evs
        if rules.is_no_ticket(r):
            return {"error": "Точность распознавания ниже порога: заявка не формируется, нужна повторная запись"}, evs
        raw = [x for x in (args.get("references") or []) if isinstance(x, dict)]
        refs, bad = rules.check_references(raw, self.engine, self.seen_pages)
        # Применимость: страница про узел неисправности, не оглавление, цитата есть на странице дословно
        quotes = {(x.get("doc_id"), int(x.get("page") or 0)): str(x.get("quote") or "") for x in raw}
        ok = []
        for x in refs:
            text = rag.page_text(x["doc_id"], x["page"]) or ""
            q = quotes.get((x["doc_id"], x["page"]), "")
            why = ("оглавление" if rules.is_toc_page(text) else
                   "страница не про узел неисправности" if not rules.page_matches_node(text, r["anomaly_type"]) else
                   "цитаты нет на странице" if not rules.quote_on_page(q, text) else "")
            if why:
                bad.append({**x, "why": why})
            else:
                ok.append({**x, "quote": q})
        refs = ok
        self._tag(refs)
        if bad:
            store.log_event("rejected_refs", {"refs": bad}, request_id=self.id, vehicle_id=self.v["vehicle_id"])
        pages_text = chr(10).join(rag.page_text(x["doc_id"], x["page"]) or "" for x in refs)
        ops, bad_ops = [], []
        for o in (str(o).strip() for o in (args.get("operations") or [])):
            if o:
                (ops if rules.op_supported(o, pages_text) else bad_ops).append(o)
        if bad_ops:
            store.log_event("rejected_operations", {"ops": bad_ops}, request_id=self.id,
                            vehicle_id=self.v["vehicle_id"])
        if not refs or not ops:
            self.draft_problem = ("нет страницы с процедурой по этому узлу" if not refs
                                  else "работы не подтверждаются страницами руководства")
            reasons = "; ".join(f"{b.get('doc_id')}:{b.get('page')} - {b.get('why', 'страница не из поиска')}" for b in bad)
            return {"error": f"Черновик не принят: {self.draft_problem}. {reasons}"
                    + (f". Работы без опоры на страницы: {bad_ops}" if bad_ops else "")
                    + ". Найди процедуру этого узла и приложи дословные цитаты, либо скажи, что инструкции нет."}, evs
        low = rules.is_low_confidence(r)
        if low:
            ops = [rules.recheck_operation(r)] + ops
        urgency, why = rules.urgency_reason(r)
        payload = {"vehicle_id": self.v["vehicle_id"], "session_id": r["session_id"],
                   "anomaly_type": r["anomaly_type"],
                   "conclusion": ("Предварительно: " if low else "") + str(args.get("conclusion", "")).strip(),
                   "urgency": urgency, "operations": ops,
                   "spare_parts": [s for s in (args.get("spare_parts") or []) if str(s).strip()],
                   "references": [{"manual": x["manual"], "pdf_page": x["page"]} for x in refs]}
        extra = {"why": why, "refs": refs, "anomaly_title": r.get("anomaly_title"), "low_confidence": low,
                 "confidence": r.get("confidence"), "signal_quality": r.get("signal_quality"),
                 "removed_operations": bad_ops}
        tid = store.save_draft(self.v["vehicle_id"], r["session_id"], payload, extra)
        self.drafted = True
        evs.append(self.ev("step", text="Готовлю черновик заявки на дефектовку", tool="draft_ticket"))
        evs.append({"type": "ticket_draft", "id": tid, "payload": payload, "extra": extra})
        return {"ok": True, "draft_id": tid, "urgency": urgency, "operations": ops,
                "note": "Черновик показан механику, отправит он сам кнопкой"}, evs

    # ---------- цикл ----------

    async def loop(self, messages: list[dict]) -> AsyncIterator[dict]:
        waiting = []

        async def on_wait():
            waiting.append(1)

        for _ in range(MAX_TOOL_CALLS + 2):
            funcs = FUNCTIONS if self.tool_calls < MAX_TOOL_CALLS else None
            try:
                res = await llm.chat(messages, funcs, on_wait=on_wait)
            except llm.LLMError as e:
                yield self.ev("error", text="Модель сейчас недоступна. Повторите запрос через минуту.", detail=str(e))
                return
            if waiting:
                yield self.ev("warning", text="Модель была занята, запрос подождал в очереди")
                waiting.clear()
            self.llm_calls += 1
            u = res["usage"]
            self.tokens_in += u.get("prompt_tokens", 0)
            self.tokens_out += u.get("completion_tokens", 0)
            self.cost += llm.cost_rub(u)
            msg = res["message"]
            fc = msg.get("function_call")
            step = {"get_check_result": "выбор: результат проверки", "get_vehicle_history": "выбор: история машины",
                    "search_manual": "выбор: поиск в руководстве", "draft_ticket": "черновик заявки"}.get(
                fc["name"] if fc else "", "ответ механику")
            store.log_usage(self.id, fc["name"] if fc else "answer", res["model"], u, res["seconds"])
            yield {"type": "llm", "name": step, "model": res["model"], "tin": u.get("prompt_tokens", 0),
                   "tout": u.get("completion_tokens", 0), "ms": int(res["seconds"] * 1000)}
            yield self.metrics()
            if (not fc and self.node and not self.drafted and not self.draft_problem and not self.draft_nudged
                    and self.result and not rules.is_no_ticket(self.result)):
                # По отклонению сначала черновик заявки (или явный отказ, если процедуры нет), потом ответ
                self.draft_nudged = True
                store.log_event("rule_draft_required", {}, request_id=self.id, vehicle_id=self.v["vehicle_id"])
                messages.append({"role": "assistant", "content": msg.get("content") or ""})
                messages.append({"role": "user", "content": "Ответ пока не показан механику: сначала вызови draft_ticket - работы только со страниц с процедурой этого узла, к каждой странице дословная цитата. Если подходящей процедуры в найденном нет - поищи ещё; если её нет в руководстве, повтори ответ и скажи об этом."})
                continue
            if not fc and self.require_search and not self.seen_pages and not self.nudged:
                # Код не принимает ответ без поиска по руководству: возвращаем модель к инструменту
                self.nudged = True
                store.log_event("rule_search_required", {}, request_id=self.id, vehicle_id=self.v["vehicle_id"])
                messages.append({"role": "assistant", "content": msg.get("content") or ""})
                messages.append({"role": "user", "content": "Ответ не принят: ты не искал в руководстве. Если в ответе есть технические сведения, нормативы или рекомендации - вызови search_manual и ответь только по найденному, со ссылками. Если вопрос только об истории проверок этой машины - ответь без нормативов и рекомендаций по ремонту." + (" По результату с отклонением найди процедуру узла, вызови draft_ticket с дословными цитатами, затем ответь." if self.result and self.result.get("status") == "anomaly" and not rules.is_no_ticket(self.result) else "")})
                continue
            if not fc:
                text, refs, rejected = rules.filter_citations(msg.get("content") or "", self.engine, self.seen_pages)
                self._tag(refs)
                text, removed = rules.unsupported_lines(text, self.ml_numbers())
                text, unverified = rules.unverified_cited_lines(text, rag.page_text, self.node)
                if unverified:  # утверждение со ссылкой, которого нет на этой странице, - гипотеза под видом цитаты
                    store.log_event("removed_unverified", {"lines": unverified}, request_id=self.id,
                                    vehicle_id=self.v["vehicle_id"])
                    text += (f"{chr(10)}{chr(10)}_Убрано утверждений, которых нет на указанных страницах "
                             f"руководства: {len(unverified)}._")
                    rejected += [f"нет на странице: {x[:60]}" for x in unverified]
                if removed:  # число без ссылки на страницу - ошибка: строку убираем и говорим об этом
                    store.log_event("removed_unsupported", {"lines": removed}, request_id=self.id,
                                    vehicle_id=self.v["vehicle_id"])
                    text += (f"{chr(10)}{chr(10)}_Убрано утверждений с числами без ссылки на руководство: "
                             f"{len(removed)}. Нормативы смотрите по руководству._")
                    rejected += [f"без ссылки: {x[:60]}" for x in removed]
                if rejected:
                    store.log_event("rejected_citations", {"refs": rejected}, request_id=self.id,
                                    vehicle_id=self.v["vehicle_id"])
                if self.recur:  # повтор дефекта - факт из истории, добавляет код в начало наблюдений
                    m = re.search(r"^\*\*Наблюдения\*\*\s*$", text, re.M)
                    line = f"- **{self.recur}**"
                    text = (text[:m.end()] + chr(10) + line + text[m.end():]) if m else f"{line}{chr(10)}{chr(10)}{text}"
                yield self.ev("answer", text=text, refs=refs, rejected=len(rejected),
                              searched=bool(self.seen_pages), engine=self.engine)
                return
            args = fc.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            out, evs = await self.tool(fc["name"], args)
            for e in evs:
                yield e
            # Сбой проверки, полученный по запросу из чата: дальше модель не зовём
            if fc["name"] == "get_check_result" and self.result and rules.is_check_failed(self.result):
                yield self.ev("answer", text=rules.check_failed_text(self.result), refs=[])
                return
            messages.append({"role": "assistant", "content": "", "function_call": {"name": fc["name"], "arguments": args}})
            messages.append({"role": "function", "name": fc["name"], "content": json.dumps(out, ensure_ascii=False)})
        yield self.ev("error", text="Агент не уложился в лимит шагов, уточните вопрос")


def _system(run: Run) -> dict:
    manuals = "; ".join(f"{m.doc_id} - {m.title}" for m in manuals_for_engine(run.engine)) or "нет руководств"
    return {"role": "system", "content": SYSTEM.format(vehicle=_vehicle_line(run.v), engine=run.engine, manuals=manuals)}


async def diagnose(vehicle_id: str) -> AsyncIterator[dict]:
    """Кнопка «Получить результат диагностики» (F2): код вызывает ML, дальше решает агент.

    Сбой проверки и норма обрабатываются без лишних шагов; при сбое модель не вызывается (F3).
    """
    run = Run(vehicle_id)
    store.log_event("diagnose_click", actor="mechanic", request_id=run.id, vehicle_id=vehicle_id)
    sess = services.latest_session(vehicle_id)
    yield run.ev("step", text=f"Запрашиваю результат проверки {sess['session_id']}", tool="get_check_result")
    try:
        run.result = await services.diagnose(vehicle_id, sess["session_id"])
    except services.ServiceError as e:
        yield run.ev("error", text=str(e))
        return
    yield {"type": "check_result", "result": run.result, "low_confidence": rules.is_low_confidence(run.result)}
    if rules.is_check_failed(run.result):
        yield run.ev("answer", text=rules.check_failed_text(run.result), refs=[])
        yield run.metrics()
        yield {"type": "done"}
        return
    for e in run.quality_warning():
        yield e
    if rules.is_no_ticket(run.result):  # точность ниже порога: модель не вызываем, заявки нет
        yield run.ev("answer", text=rules.no_ticket_text(run.result), refs=[])
        yield run.metrics()
        yield {"type": "done"}
        return
    user = ("Механик нажал «Получить результат диагностики». Результат проверки:\n"
            + json.dumps(_short_result(run.result), ensure_ascii=False))
    if run.result["status"] == "anomaly":
        run.require_search = True  # по отклонению ответ принимается только после поиска процедуры в руководстве
    if run.result["status"] == "normal":
        user += "\nОтклонений нет: коротко скажи, что двигатель в норме и можно продолжать эксплуатацию. Руководство не ищи, заявку не делай."
    elif rules.is_low_confidence(run.result):
        user += ("\nТочность распознавания низкая: явно скажи, что вывод предварительный и машина направляется на повторную дефектовку. "
                 "В заявку включай только проверки и дефектовку по руководству, без ремонтных работ: повторную запись код добавит сам.")
    if run.result["status"] == "anomaly":
        # Первый поиск по узлу неисправности делает код: модели не нужен лишний вызов, чтобы его начать
        run.node = run.result.get("anomaly_type") or ""
        run.recur = rules.recurrence_note(run.v["sessions"], run.result, store.tickets_for(vehicle_id))
        q = run.result.get("anomaly_title") or run.result.get("anomaly_type")
        found, evs = await run.tool("search_manual", {"query": q, "node_first": True})
        for e in evs:
            yield e
        user += (f"\nКод уже выполнил search_manual по запросу «{q}»: "
                 + json.dumps(found, ensure_ascii=False)
                 + "\nЕсли процедуры узла здесь нет, поищи ещё конкретными словами.")
    async for e in run.loop([_system(run), {"role": "user", "content": user}]):
        yield e
    if run.result["status"] == "anomaly" and not run.drafted:
        yield run.ev("no_ticket", text=rules.no_instruction_text(run.result, run.engine, run.draft_problem))
    yield {"type": "done"}


async def ask(vehicle_id: str, text: str, history: list[dict] | None = None) -> AsyncIterator[dict]:
    """Вопрос механика в чате: агент сам решает, какие инструменты нужны."""
    run = Run(vehicle_id, require_search=True)
    store.log_event("question", {"text": text}, actor="mechanic", request_id=run.id, vehicle_id=vehicle_id)
    msgs = [_system(run), *(history or [])[-6:], {"role": "user", "content": text}]
    async for e in run.loop(msgs):
        yield e
    yield {"type": "done"}
