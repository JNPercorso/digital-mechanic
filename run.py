"""Запуск всего стенда одной командой: заглушки Mock ML (8001), Mock ТОиР (8002) и приложение (8000).

    python run.py
Открыть http://127.0.0.1:8000. Остановка - Ctrl+C.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
procs = [
    subprocess.Popen([PY, "-m", "uvicorn", "mock_ml.app:app", "--port", "8001"], cwd=ROOT / "mocks"),
    subprocess.Popen([PY, "-m", "uvicorn", "mock_toir.app:app", "--port", "8002"], cwd=ROOT / "mocks"),
]
try:
    subprocess.run([PY, "-m", "uvicorn", "app.main:app", "--port", "8000", *sys.argv[1:]], cwd=ROOT)
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()
