"""Внешние данные и сервисы по контракту заказчика: парк машин, Mock ML API, Mock ТОиР."""
import json
from functools import lru_cache

import httpx

from app import settings


class ServiceError(Exception):
    """Внешний сервис не ответил или ответил ошибкой; текст - для механика."""


@lru_cache(maxsize=1)
def _fleet() -> dict:
    with open(settings.VEHICLES_PATH, encoding="utf-8") as f:
        return {v["vehicle_id"]: v for v in json.load(f)["vehicles"]}


@lru_cache(maxsize=1)
def fault_types() -> dict:
    with open(settings.FAULT_TYPES_PATH, encoding="utf-8") as f:
        return {x["code"]: x["title"] for x in json.load(f)}


def vehicles() -> list[dict]:
    return list(_fleet().values())


def vehicle(vehicle_id: str) -> dict | None:
    return _fleet().get(vehicle_id)


def latest_session(vehicle_id: str) -> dict | None:
    v = vehicle(vehicle_id)
    return v["sessions"][-1] if v and v["sessions"] else None


async def diagnose(vehicle_id: str, session_id: str) -> dict:
    """POST /diagnose Mock ML API."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{settings.MOCK_ML_URL}/diagnose",
                             json={"vehicle_id": vehicle_id, "session_id": session_id})
    except httpx.TransportError as e:
        raise ServiceError("ML-сервис диагностики не отвечает, повторите позже") from e
    if r.status_code == 404:
        raise ServiceError("ML-сервис не нашёл такую машину или проверку")
    if r.status_code != 200:
        raise ServiceError(f"ML-сервис вернул ошибку {r.status_code}")
    return r.json()


async def create_ticket(payload: dict) -> dict:
    """POST /tickets Mock ТОиР. Возвращает {"ticket_id", "status"}."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{settings.MOCK_TOIR_URL}/tickets", json=payload)
    except httpx.TransportError as e:
        raise ServiceError("Система ТОиР не отвечает, заявка сохранена, отправьте повторно") from e
    if r.status_code != 201:
        raise ServiceError(f"ТОиР не принял заявку (код {r.status_code}): {r.text[:300]}")
    return r.json()
