"""Веб-приложение: страница механика, API и поток событий агента (SSE)."""
import json
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


class Ask(BaseModel):
    vehicle_id: str
    text: str = Field(min_length=1, max_length=1000)


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
    return _sse(agent.ask(body.vehicle_id, body.text.strip()))


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


@app.get("/api/manuals/{doc_id}/{page}")
def manual_page(doc_id: str, page: int):
    """Текст страницы PDF для окна просмотра ссылки."""
    m = BY_ID.get(doc_id)
    text = rag.page_text(doc_id, page) if m else None
    if text is None:
        raise HTTPException(404, "Нет такой страницы")
    return {"doc_id": doc_id, "manual": m.title, "page": page, "text": text,
            "pages": len(rag._pages()[doc_id])}


@app.get("/api/manuals/{doc_id}/{page}/image")
def manual_page_image(doc_id: str, page: int):
    """Картинка страницы PDF (рисунки, таблицы) - рендер по запросу."""
    import pymupdf
    m = BY_ID.get(doc_id)
    if not m:
        raise HTTPException(404)
    doc = pymupdf.open(MANUALS_DIR / m.file)
    if not 1 <= page <= len(doc):
        raise HTTPException(404)
    png = doc[page - 1].get_pixmap(dpi=110).tobytes("png")
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
