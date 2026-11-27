"""Загрузка материалов задачи на стенд при развёртывании (python -m app.fetch_materials).

Руководства (PDF) и данные заглушек - материалы заказчика и производителей двигателей, в открытом
репозитории их нет. Стенд скачивает их по ссылке из переменной окружения MATERIALS_URL (архив .zip
из материалов задачи), раскладывает по местам и собирает базу знаний:
  - PDF -> manuals/ (имена файлов приводятся к реестру app/manuals.py);
  - данные заглушек (карточки машин, ответы диагностики, справочник неисправностей) -> mocks/data/;
  - затем: python -m app.rag build, python -m app.sections build.

Поддерживаются прямая ссылка на файл и ссылка Google Drive на файл.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from app import settings
from app.manuals import MANUALS

ROOT = Path(__file__).resolve().parents[1]
MOCK_DATA = ROOT / "mocks" / "data"

# Исходные имена PDF в материалах задачи -> имена в реестре руководств
SOURCE_NAMES = {
    "ЯМЗ_236_руководство по эксплуатации.pdf": "yamz-236_re_2010.pdf",
    "ЯМЗ_236_руководство по РЕМОНТУ.pdf": "yamz-236_rk_2014.pdf",
}


def _direct(url: str) -> str:
    """Ссылка Google Drive на файл -> прямое скачивание (для больших файлов - с подтверждением)."""
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)([\w-]{20,})", url)
    if m:
        return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t"
    return url


def download(url: str) -> bytes:
    req = urllib.request.Request(_direct(url), headers={"User-Agent": "digital-mechanic-deploy"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def unpack(data: bytes) -> dict:
    """Раскладывает архив: возвращает {что: сколько файлов}."""
    manuals_dir = Path(settings.MANUALS_DIR)
    manuals_dir.mkdir(parents=True, exist_ok=True)
    MOCK_DATA.mkdir(parents=True, exist_ok=True)
    known = {m.file for m in MANUALS}
    stats = {"pdf": 0, "mock_data": 0}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = info.filename
            # имена в zip из Windows бывают в cp866
            if not info.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("cp866")
                except UnicodeError:
                    pass
            base = Path(name).name
            if info.is_dir() or not base:
                continue
            if base.lower().endswith(".pdf"):
                target = SOURCE_NAMES.get(base, base)
                if target in known:
                    (manuals_dir / target).write_bytes(z.read(info))
                    stats["pdf"] += 1
            elif re.search(r"(^|/)mock/data/[^/]+\.json$", name.replace("\\", "/")):
                (MOCK_DATA / base).write_bytes(z.read(info))
                stats["mock_data"] += 1
    return stats


def main() -> None:
    url = os.getenv("MATERIALS_URL", "").strip()
    if not url:
        sys.exit("Не задана переменная окружения MATERIALS_URL (ссылка на архив материалов задачи)")
    print("Скачиваю материалы задачи...", flush=True)
    stats = unpack(download(url))
    print(f"Разложено: PDF {stats['pdf']}, данные заглушек {stats['mock_data']}")
    missing = [m.file for m in MANUALS if not (Path(settings.MANUALS_DIR) / m.file).exists()]
    if missing:
        sys.exit(f"Нет руководств: {missing}. Проверьте архив по ссылке MATERIALS_URL.")
    if "--no-index" not in sys.argv:
        for mod in ("app.rag", "app.sections"):
            subprocess.run([sys.executable, "-m", mod, "build"], check=True, cwd=ROOT)
        subprocess.run([sys.executable, "-m", "app.rag", "fix-symbols"], check=True, cwd=ROOT)
    print("Готово")


if __name__ == "__main__":
    main()
