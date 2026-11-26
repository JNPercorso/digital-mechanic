"""Веб-приложение: страница механика, API и поток событий агента (SSE)."""
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import agent, llm, rag, rules, services, store, tts
from app.manuals import BY_ID, MANUALS_DIR

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Цифровой механик")

# --- простое ограничение частоты для открытого стенда: не больше N запросов к агенту в минуту с адреса
_RATE: dict[str, list[float]] = {}
RATE_LIMIT = 20


def _rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _RATE.get(ip, []) if now - t < 60]
    hits.append(now)
    _RATE[ip] = hits
    return len(hits) <= RATE_LIMIT


def _sse(gen):
    async def stream():
        try:
            async for e in gen:
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
        except Exception as ex:  # интерфейс не должен падать: показываем понятную ошибку
            store.log_event("crash", {"error": repr(ex)})
            yield f"data: {json.dumps({'type': 'error', 'text': 'Внутренняя ошибка, повторите запрос'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _vehicle_or_404(vehicle_id: str) -> dict:
    v = services.vehicle(vehicle_id)
    if not v:
        raise HTTPException(404, "Машина не найдена")
    return v


@app.get("/api/vehicles")
def vehicles():
    """Парк с историей проверок (для списка, тренда и окна проверки)."""
    return services.vehicles()


@app.get("/api/vehicles/{vehicle_id}")
def vehicle(vehicle_id: str):
    v = _vehicle_or_404(vehicle_id)
    return {**v, "tickets": store.tickets_for(vehicle_id),
            "low_confidence_threshold": rules.LOW_CONFIDENCE}


@app.get("/api/fault-types")
def fault_types():
    return services.fault_types()


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=6000)


class Ask(BaseModel):
    vehicle_id: str
    text: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=12)  # диалог хранится на устройстве механика


@app.post("/api/diagnose/{vehicle_id}")
async def diagnose(vehicle_id: str, request: Request):
    _vehicle_or_404(vehicle_id)
    if not _rate_ok(request.client.host):
        raise HTTPException(429, "Слишком много запросов, подождите минуту")
    return _sse(agent.diagnose(vehicle_id))


@app.post("/api/ask")
async def ask(body: Ask, request: Request):
    _vehicle_or_404(body.vehicle_id)
    if not _rate_ok(request.client.host):
        raise HTTPException(429, "Слишком много запросов, подождите минуту")
    history = [t.model_dump() for t in body.history]
    return _sse(agent.ask(body.vehicle_id, body.text.strip(), history))


@app.get("/api/tickets/{tid}")
def ticket(tid: int):
    """Текущее состояние черновика (для диалога, восстановленного с устройства)."""
    t = store.get_ticket(tid)
    if not t:
        raise HTTPException(404, "Черновик не найден")
    return t


class TicketEdit(BaseModel):
    urgency: str | None = Field(default=None, pattern="^(low|medium|high)$")
    operations: list[str] | None = None
    spare_parts: list[str] | None = None


@app.put("/api/tickets/{tid}")
def edit_ticket(tid: int, body: TicketEdit):
    """Правка черновика механиком: приоритет, работы, запчасти. Источники меняет только агент."""
    t = store.get_ticket(tid)
    if not t or t["status"] == "sent":
        raise HTTPException(409, "Черновик не найден или заявка уже отправлена")
    p, extra = t["payload"], t["extra"]
    changes = body.model_dump(exclude_none=True)
    if "operations" in changes:
        changes["operations"] = [o.strip() for o in changes["operations"] if o.strip()]
        if not changes["operations"]:
            raise HTTPException(422, "Нужна хотя бы одна работа")
    if "spare_parts" in changes:
        changes["spare_parts"] = [s.strip() for s in changes["spare_parts"] if s.strip()]
    if extra.get("low_confidence") and "operations" in changes:
        must = rules.recheck_operation({})
        changes["operations"] = [must] + [o for o in changes["operations"] if o != must]
    p.update(changes)
    extra["edited_by_mechanic"] = True
    store.update_ticket(tid, payload=p, extra=extra)
    store.log_event("ticket_edit", changes, actor="mechanic", vehicle_id=t["vehicle_id"])
    return store.get_ticket(tid)


class SendBody(BaseModel):
    confirmed_low_confidence: bool = False  # механик подтвердил предупреждение о предварительном выводе


@app.post("/api/tickets/{tid}/send")
async def send_ticket(tid: int, body: SendBody | None = None):
    """«Отправить в ТОиР» - только по нажатию механика; при предварительном выводе - после его подтверждения."""
    t = store.get_ticket(tid)
    if not t:
        raise HTTPException(404, "Черновик не найден")
    if t["status"] == "sent":
        return t
    if t["extra"].get("low_confidence") and not (body and body.confirmed_low_confidence):
        raise HTTPException(409, "Вывод предварительный: подтвердите предупреждение перед отправкой")
    if t["extra"].get("low_confidence"):
        store.log_event("low_confidence_confirmed", {"ticket": tid}, actor="mechanic", vehicle_id=t["vehicle_id"])
    try:
        res = await services.create_ticket(t["payload"])
    except services.ServiceError as e:
        store.update_ticket(tid, status="error")
        store.log_event("ticket_send_error", {"error": str(e)}, actor="mechanic", vehicle_id=t["vehicle_id"])
        raise HTTPException(502, str(e))
    store.update_ticket(tid, status="sent", toir_ticket_id=res["ticket_id"], sent_at=time.time())
    store.log_event("ticket_sent", res, actor="mechanic", vehicle_id=t["vehicle_id"])
    return store.get_ticket(tid)


_PARA_START = re.compile(r"^(\d+(\.\d+)*[.)]?\s|[-–—•●]\s?|Рис\.|Рисунок|Таблица|ВНИМАНИЕ|ПРЕДУПРЕЖДЕНИЕ|Примечани)")


def _reflow(text: str) -> list:
    """Склеивает строки PDF в абзацы: в части страниц каждое слово лежит отдельной строкой.

    Строки таблиц (ячейки через rag.TABLE_SEP) остаются отдельными: {"row": [ячейка, ...]}.
    """
    paras, cur = [], ""
    for line in text.split(chr(10)):
        s = line.strip()
        if not s:
            continue
        if rag.TABLE_SEP.strip() in s and rag.TABLE_SEP in line + " ":
            if cur:
                paras.append(cur)
                cur = ""
            paras.append({"row": [c.strip() for c in (line + " ").split(rag.TABLE_SEP)]})
            continue
        # Новый абзац: нумерация/маркер, конец предложения или строка с заглавной буквы;
        # аббревиатуры (ЯМЗ, «КАСО») абзац не начинают
        title = re.match(r"^[А-ЯЁA-Z][а-яёa-z]", s) and len(cur.split()) >= 3
        if cur and (_PARA_START.match(s) or re.search(r"[.:;!?]$", cur) or title):
            paras.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    if cur:
        paras.append(cur)
    return paras


def _split_sents(p: str) -> list[dict]:
    return [{"t": x, "h": False} for x in re.split(r"(?<=[.!?;])\s+", p) if x]


def _highlight(paras: list, q: str) -> list:
    """Помечает предложения страницы, которые лучше всего совпадают с фразой агента (по основам слов и числам).

    Абзац -> список предложений; строка таблицы -> {"row": [предложения ячейки, ...]}. В таблице
    подсвечивается вся строка (причина вместе со способом устранения).
    """
    qt = set(rag.tokenize(q))
    out, scored = [], []
    for pi, p in enumerate(paras):
        if isinstance(p, dict):
            cells = [_split_sents(c) for c in p["row"]]
            out.append({"row": cells})
            groups = [(pi, " ".join(c for c in p["row"]), [x for c in cells for x in c])]
        else:
            sents = _split_sents(p)
            out.append(sents)
            groups = [(pi, x["t"], [x]) for x in sents]
        for _, text, objs in groups:
            st = set(rag.tokenize(text))
            if qt and st and len(text) <= 600:
                score = len(qt & st) / len(qt) + 0.15 * len({t for t in qt & st if t[:1].isdigit()})
                scored.append((score, len(scored), objs))
    for score, _, objs in sorted(scored, key=lambda x: -x[0])[:3]:
        if score >= 0.3:
            for x in objs:
                x["h"] = True
    return out


def _sentences(paras: list) -> list[dict]:
    """Все предложения страницы подряд (из абзацев и ячеек таблиц)."""
    out = []
    for p in paras:
        out += [x for c in p["row"] for x in c] if isinstance(p, dict) else p
    return out


def _marked(text: str, q: str, sq: str) -> list[list[dict]]:
    """Абзацы с подсветкой: по фразе агента, а если она не нашлась на странице - по запросу поиска."""
    paras = _reflow(text)
    out = _highlight(paras, q[:500])
    if sq and not any(x["h"] for x in _sentences(out)):
        out = _highlight(paras, sq[:300])
    return out


@app.get("/api/manuals/{doc_id}/{page}")
def manual_page(doc_id: str, page: int, q: str = "", sq: str = ""):
    """Страница PDF для окна просмотра ссылки: абзацы и подсветка фрагмента, на который сослался агент."""
    m = BY_ID.get(doc_id)
    text = rag.page_text(doc_id, page) if m else None
    if text is None:
        raise HTTPException(404, "Нет такой страницы")
    return {"doc_id": doc_id, "manual": m.title, "page": page, "pages": len(rag._pages()[doc_id]),
            "paras": _marked(text, q, sq)}


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", s.lower().replace("ё", "е"))


def _word_rects(pg, sents: list[str]) -> list:
    """Прямоугольники слов страницы PDF, из которых состоят подсвеченные предложения.

    Ищем предложение по тройкам подряд идущих слов; из повторов фразы на странице («заменить вышедшие
    из строя детали») берём ближайший к предыдущему совпадению, чтобы не красить одинаковые строки рядом.
    Возвращает прямоугольники, объединённые по строкам.
    """
    import pymupdf
    words = pg.get_text("words")  # x0, y0, x1, y1, слово, блок, строка, номер
    toks, owners = [], []
    i = 0
    while i < len(words):  # «заме-» + «нить» -> «заменить»
        t, own = _norm(words[i][4]), [i]
        while words[i][4].endswith(("-", chr(173))) and i + 1 < len(words):
            i += 1
            t += _norm(words[i][4])
            own.append(i)
        if t:
            toks.append(t)
            owners.append(own)
        i += 1
    marked = set()

    def match(st: list[str], n: int) -> set:
        got, last = set(), None
        for k in range(len(st) - n + 1):
            sh = st[k:k + n]
            if n == 1 and len(sh[0]) < 5:
                continue  # одиночные короткие слова встречаются везде
            pos = [j for j in range(len(toks) - n + 1) if toks[j:j + n] == sh]
            if not pos:
                continue
            j = pos[0] if last is None else min(pos, key=lambda x: abs(x - last))
            if last is not None and abs(j - last) > 40:
                continue
            last = j
            for jj in range(j, j + n):
                got.update(owners[jj])
        return got

    for s in sents:
        st = [w for w in (_norm(w) for w in s.split()) if w]
        # Тройки слов подряд; в таблицах слова ячейки могут идти в другом порядке - тогда пары и отдельные слова
        for n in (3, 2, 1):
            got = match(st, min(n, len(st))) if st else set()
            if got:
                marked |= got
                break
    lines = {}
    for wi in marked:
        w = words[wi]
        key = (w[5], w[6])
        r = pymupdf.Rect(w[:4])
        lines[key] = lines[key] | r if key in lines else r
    return list(lines.values())


@app.get("/api/manuals/{doc_id}/{page}/image")
def manual_page_image(doc_id: str, page: int, q: str = "", sq: str = ""):
    """Картинка страницы PDF (рисунки, таблицы); строки подсвеченного фрагмента закрашены маркером."""
    import pymupdf
    m = BY_ID.get(doc_id)
    if not m:
        raise HTTPException(404)
    doc = pymupdf.open(MANUALS_DIR / m.file)
    if not 1 <= page <= len(doc):
        raise HTTPException(404)
    pg = doc[page - 1]
    text = rag.page_text(doc_id, page)
    if text and (q or sq):
        sents = [x["t"] for x in _sentences(_marked(text, q, sq)) if x["h"]]
        for r in _word_rects(pg, sents):
            pg.draw_rect(r + (-1.5, -1, 1.5, 1), color=None, fill=(1, 0.8, 0.1), fill_opacity=0.38, overlay=True)
    png = pg.get_pixmap(dpi=150).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


class Speak(BaseModel):
    text: str = Field(min_length=1, max_length=900)


@app.post("/api/tts")
def speak(body: Speak):
    """Озвучка фрагмента ответа голосом Silero на нашем сервере; 503 - озвучит браузер."""
    data = tts.synth(body.text)
    if data is None:
        raise HTTPException(503, "Озвучка на сервере недоступна")
    return Response(data, media_type="audio/wav", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/status")
def status():
    return {"queue": llm.queue_state()}


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # Без кеша браузера, чтобы обновление приложения доходило сразу
    return FileResponse(WEB / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB), name="web")
