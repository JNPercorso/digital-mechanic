"""Веб-приложение: страница механика, API и поток событий агента (SSE)."""
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import agent, llm, rag, rules, services, store
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


@app.post("/api/tickets/{tid}/send")
async def send_ticket(tid: int):
    """«Отправить в ТОиР» - только по нажатию механика."""
    t = store.get_ticket(tid)
    if not t:
        raise HTTPException(404, "Черновик не найден")
    if t["status"] == "sent":
        return t
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


def _reflow(text: str) -> list[str]:
    """Склеивает строки PDF в абзацы: в части страниц каждое слово лежит отдельной строкой."""
    paras, cur = [], ""
    for line in text.split(chr(10)):
        s = line.strip()
        if not s:
            continue
        # Новый абзац: нумерация/маркер, конец предложения или строка с заглавной буквы (ячейки таблиц);
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


def _highlight(paras: list[str], q: str) -> list[list[dict]]:
    """Помечает предложения страницы, которые лучше всего совпадают с фразой агента (по основам слов и числам)."""
    qt = set(rag.tokenize(q))
    out = []
    scored = []
    for pi, p in enumerate(paras):
        sents = re.split(r"(?<=[.!?;])\s+", p)
        out.append([{"t": s, "h": False} for s in sents])
        for si, s in enumerate(sents):
            st = set(rag.tokenize(s))
            if qt and st:
                score = len(qt & st) / len(qt) + 0.15 * len({t for t in qt & st if t[:1].isdigit()})
                scored.append((score, pi, si))
    for score, pi, si in sorted(scored, reverse=True)[:3]:
        if score >= 0.3 and len(out[pi][si]["t"]) <= 400:
            out[pi][si]["h"] = True
    return out


def _marked(text: str, q: str, sq: str) -> list[list[dict]]:
    """Абзацы с подсветкой: по фразе агента, а если она не нашлась на странице - по запросу поиска."""
    paras = _reflow(text)
    out = _highlight(paras, q[:500])
    if sq and not any(x["h"] for p in out for x in p):
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
        sents = [_norm(x["t"]) for p in _marked(text, q, sq) for x in p if x["h"]]
        for b in pg.get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                ln = _norm("".join(sp["text"] for sp in line["spans"]))
                if len(ln) >= 4 and any(ln in s or (len(s) >= 12 and (s[:12] in ln or s[-12:] in ln)) for s in sents):
                    pg.draw_rect(pymupdf.Rect(line["bbox"]) + (-2, -1, 2, 1), color=None,
                                 fill=(1, 0.8, 0.1), fill_opacity=0.38, overlay=True)
    png = pg.get_pixmap(dpi=150).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


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
