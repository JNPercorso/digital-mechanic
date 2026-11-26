"""Агент «Цифровой механик»: модель сама выбирает инструменты (F5), код следит за правилами.

Инструменты модели: результат проверки (ML), история машины, поиск по руководству (RAG),
черновик заявки. Отправка заявки в ТОиР - только кнопкой механика, модель её не вызывает.
Все шаги идут событиями в интерфейс (SSE): step, warning, answer, ticket_draft, metrics, done.
"""
import json
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
2. После каждого утверждения из руководства ставь ссылку строго в виде [[doc_id:страница]], например [[yamz236_rk:96]]. Ссылаться можно только на страницы из результатов search_manual.
3. Если в найденных фрагментах ответа нет - так и скажи: «В руководстве на этот двигатель этого нет» и рекомендуй дефектовку. Не бери данные с другого двигателя.
4. Ты помощник: окончательное решение о двигателе принимает механик.
5. Отвечай только на заданный вопрос, не добавляй сведения, о которых не спрашивали.
6. Ищи в руководстве конкретными словами (узел, симптом, операция), при необходимости сделай 2-3 разных поиска.

Ответ на результат проверки с отклонением - в таком порядке, с заголовками:
**Вердикт** - одна строка: что вероятно не так и насколько срочно.
**Что слышно** - признаки из проверки.
**Вероятные причины** - 2-3 гипотезы.
**Чем грозит** - риск, если ездить дальше.
**Что проверить** - пошагово, с нормативами и ссылками.
**Возможный ремонт** - работы, инструмент, материалы и запчасти (с каталожными номерами, если они есть в руководстве), со ссылками.
**Ограничения** - чего в руководстве нет или что требует проверки на месте.
Сначала найди процедуру в руководстве, затем вызови draft_ticket, чтобы подготовить черновик заявки на дефектовку, и после этого дай ответ механику. Номер черновика не упоминай: механик видит черновик рядом с ответом.

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
             "doc_id": {"type": "string"}, "page": {"type": "integer"}}},
             "description": "Страницы руководства, на которых основаны работы"}},
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
        self.result: dict | None = None   # последний полученный результат проверки
        self.tool_calls = 0
        self.llm_calls = 0
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.t0 = time.time()
        self.events: list[dict] = []

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
            evs.append(self.ev("step", text=f"Запрашиваю результат проверки {sid}", tool=name))
            try:
                self.result = await services.diagnose(self.v["vehicle_id"], sid)
            except services.ServiceError as e:
                return {"error": str(e)}, evs
            out = _short_result(self.result)
            if rules.is_check_failed(self.result):
                out["rule"] = "Сбой проверки: о двигателе выводов не делать, попросить повторить запись."
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
            hits = rag.search(q, self.engine, k=5)
            for h in hits:
                self.seen_pages.add((h["doc_id"], h["page"]))
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
        refs, bad = rules.check_references(args.get("references") or [], self.engine, self.seen_pages)
        if bad:
            store.log_event("rejected_refs", {"refs": bad}, request_id=self.id, vehicle_id=self.v["vehicle_id"])
        ops = [o for o in (args.get("operations") or []) if str(o).strip()]
        if not refs or not ops:
            return {"error": "Нет работ или ссылок на страницы, найденные поиском. Сначала найди процедуру в руководстве."}, evs
        urgency, why = rules.urgency_reason(r)
        payload = {"vehicle_id": self.v["vehicle_id"], "session_id": r["session_id"],
                   "anomaly_type": r["anomaly_type"], "conclusion": str(args.get("conclusion", "")).strip(),
                   "urgency": urgency, "operations": ops,
                   "spare_parts": [s for s in (args.get("spare_parts") or []) if str(s).strip()],
                   "references": [{"manual": x["manual"], "pdf_page": x["page"]} for x in refs]}
        extra = {"why": why, "refs": refs, "anomaly_title": r.get("anomaly_title"),
                 "low_confidence": rules.is_low_confidence(r)}
        tid = store.save_draft(self.v["vehicle_id"], r["session_id"], payload, extra)
        evs.append(self.ev("step", text="Готовлю черновик заявки на дефектовку", tool="draft_ticket"))
        evs.append({"type": "ticket_draft", "id": tid, "payload": payload, "extra": extra})
        return {"ok": True, "draft_id": tid, "urgency": urgency,
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
            if not fc and self.require_search and not self.seen_pages and not self.nudged:
                # Код не принимает ответ без поиска по руководству: возвращаем модель к инструменту
                self.nudged = True
                store.log_event("rule_search_required", {}, request_id=self.id, vehicle_id=self.v["vehicle_id"])
                messages.append({"role": "assistant", "content": msg.get("content") or ""})
                messages.append({"role": "user", "content": "Ответ не принят: ты не искал в руководстве. Если в ответе есть технические сведения, нормативы или рекомендации - вызови search_manual и ответь только по найденному, со ссылками. Если вопрос только об истории проверок этой машины - ответь без нормативов и рекомендаций по ремонту."})
                continue
            if not fc:
                text, refs, rejected = rules.filter_citations(msg.get("content") or "", self.engine, self.seen_pages)
                if rejected:
                    store.log_event("rejected_citations", {"refs": rejected}, request_id=self.id,
                                    vehicle_id=self.v["vehicle_id"])
                yield self.ev("answer", text=text, refs=refs, rejected=len(rejected),
                              searched=bool(self.seen_pages), engine=self.engine)
                store.add_message(self.v["vehicle_id"], "assistant", text)
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
    if rules.is_low_confidence(run.result):
        yield run.ev("warning", text="Точность распознавания низкая: вывод предварительный, нужна повторная проверка")
    user = ("Механик нажал «Получить результат диагностики». Результат проверки:\n"
            + json.dumps(_short_result(run.result), ensure_ascii=False))
    if run.result["status"] == "normal":
        user += "\nОтклонений нет: коротко скажи, что двигатель в норме и можно продолжать эксплуатацию. Руководство не ищи, заявку не делай."
    elif rules.is_low_confidence(run.result):
        user += "\nТочность низкая: явно скажи, что вывод предварительный и нужна повторная проверка."
    store.add_message(vehicle_id, "user", "Получить результат диагностики")
    async for e in run.loop([_system(run), {"role": "user", "content": user}]):
        yield e
    yield {"type": "done"}


async def ask(vehicle_id: str, text: str) -> AsyncIterator[dict]:
    """Вопрос механика в чате: агент сам решает, какие инструменты нужны."""
    run = Run(vehicle_id, require_search=True)
    store.log_event("question", {"text": text}, actor="mechanic", request_id=run.id, vehicle_id=vehicle_id)
    history = [m for m in store.recent_messages(vehicle_id, 6)]
    store.add_message(vehicle_id, "user", text)
    msgs = [_system(run), *history, {"role": "user", "content": text}]
    async for e in run.loop(msgs):
        yield e
    yield {"type": "done"}
