"""Настройки из .env. Адреса сервисов меняются без правки кода (F6 ТЗ)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(value: str) -> str:
    p = Path(value)
    return str(p if p.is_absolute() else ROOT / p)


MOCK_ML_URL = os.getenv("MOCK_ML_URL", "http://127.0.0.1:8001").rstrip("/")
MOCK_TOIR_URL = os.getenv("MOCK_TOIR_URL", "http://127.0.0.1:8002").rstrip("/")
VEHICLES_PATH = _path(os.getenv("VEHICLES_PATH", "mocks/data/vehicles.json"))
FAULT_TYPES_PATH = _path(os.getenv("FAULT_TYPES_PATH", "mocks/data/fault_types.json"))

GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_CA_CERT = _path(os.getenv("GIGACHAT_CA_CERT", "certs/russian_trusted_root_ca_pem.crt"))
GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGACHAT_API_URL = os.getenv("GIGACHAT_API_URL", "https://api.giga.chat/v1").rstrip("/")
# Основная и запасная модель: переключение одной строкой в .env
LLM_MODEL = os.getenv("LLM_MODEL", "GigaChat-3-Ultra")
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "GigaChat-3-Pro")
# Одновременных запросов к модели (freemium GigaChat - 1 поток)
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "1"))
# Цены за 1 млн токенов, ₽ без НДС (Cloud.ru, GigaChat 3.5 Ultra) - для расчёта стоимости
PRICE_IN_PER_M = float(os.getenv("PRICE_IN_PER_M", "78.87"))
PRICE_OUT_PER_M = float(os.getenv("PRICE_OUT_PER_M", "236.56"))

DB_PATH = _path(os.getenv("DB_PATH", "data/mechanic.db"))
LOG_DIR = _path(os.getenv("LOG_DIR", "logs"))
