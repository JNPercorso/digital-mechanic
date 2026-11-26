"""Клиент GigaChat: токен доступа, очередь запросов, повторы, запасная модель, учёт токенов.

Все вызовы модели идут через одну очередь (семафор): на бесплатном тарифе GigaChat
разрешён 1 одновременный запрос. Пока запрос ждёт, интерфейс получает событие «в очереди».
"""
import asyncio
import base64
import time
import uuid

import httpx

from app import settings

_token: str | None = None
_token_expires = 0.0
_token_lock = asyncio.Lock()
_queue = asyncio.Semaphore(settings.LLM_CONCURRENCY)
_waiting = 0  # сколько запросов сейчас ждут очереди (для техпанели)


class LLMError(Exception):
    """Модель недоступна после всех повторов - показываем механику понятное сообщение."""


def _client() -> httpx.AsyncClient:
    # Сертификат Минцифры передаётся только клиенту GigaChat, проверка SSL не отключается
    return httpx.AsyncClient(verify=settings.GIGACHAT_CA_CERT, timeout=httpx.Timeout(90, connect=15))


def queue_state() -> dict:
    return {"waiting": _waiting, "concurrency": settings.LLM_CONCURRENCY,
            "token_ttl_s": max(0, int(_token_expires - time.time())) if _token else 0}


async def _get_token() -> str:
    """Токен живёт 30 минут; обновляем за 2 минуты до истечения."""
    global _token, _token_expires
    async with _token_lock:
        if _token and time.time() < _token_expires - 120:
            return _token
        key = settings.GIGACHAT_AUTH_KEY
        if not key:
            raise LLMError("Не задан GIGACHAT_AUTH_KEY в .env")
        async with _client() as c:
            r = await c.post(settings.GIGACHAT_OAUTH_URL,
                             headers={"Authorization": f"Basic {key}", "RqUID": str(uuid.uuid4()),
                                      "Accept": "application/json"},
                             data={"scope": settings.GIGACHAT_SCOPE})
            r.raise_for_status()
            j = r.json()
        _token = j["access_token"]
        # expires_at приходит в миллисекундах
        _token_expires = j.get("expires_at", (time.time() + 1800) * 1000) / 1000
        return _token


async def chat(messages: list[dict], functions: list[dict] | None = None,
               temperature: float = 0.1, on_wait=None) -> dict:
    """Один вызов модели. Возвращает {"message", "usage", "model", "seconds"}.

    on_wait - асинхронная функция, вызывается, если запрос ждёт очереди дольше 1 с.
    Повторы при 429/5xx и сетевых ошибках: 1, 2, 4 с; после - запасная модель.
    """
    global _waiting
    _waiting += 1
    notified = False
    try:
        while True:
            try:
                await asyncio.wait_for(_queue.acquire(), timeout=1)
                break
            except asyncio.TimeoutError:
                if on_wait and not notified:
                    notified = True
                    await on_wait()
    finally:
        _waiting -= 1
    try:
        last_err = None
        for model in (settings.LLM_MODEL, settings.LLM_FALLBACK_MODEL):
            for pause in (0, 1, 2, 4):
                if pause:
                    await asyncio.sleep(pause)
                body = {"model": model, "messages": messages, "temperature": temperature}
                if functions:
                    body["functions"] = functions
                    body["function_call"] = "auto"
                t0 = time.time()
                try:
                    token = await _get_token()
                    async with _client() as c:
                        r = await c.post(f"{settings.GIGACHAT_API_URL}/chat/completions", json=body,
                                         headers={"Authorization": f"Bearer {token}"})
                    if r.status_code == 401:
                        global _token
                        _token = None  # токен отозван - получить заново
                        last_err = "401"
                        continue
                    if r.status_code == 429 or r.status_code >= 500:
                        last_err = f"HTTP {r.status_code}"
                        continue
                    r.raise_for_status()
                    j = r.json()
                    return {"message": j["choices"][0]["message"],
                            "finish_reason": j["choices"][0].get("finish_reason"),
                            "usage": j.get("usage", {}), "model": model,
                            "seconds": round(time.time() - t0, 2)}
                except (httpx.TransportError, httpx.HTTPStatusError) as e:
                    last_err = str(e)
        raise LLMError(f"Модель недоступна: {last_err}")
    finally:
        _queue.release()


def cost_rub(usage: dict) -> float:
    """Стоимость вызова по ценам Cloud.ru без НДС."""
    return (usage.get("prompt_tokens", 0) * settings.PRICE_IN_PER_M
            + usage.get("completion_tokens", 0) * settings.PRICE_OUT_PER_M) / 1_000_000
