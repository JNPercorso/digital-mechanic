"""Сквозной прогон в браузере (Playwright): сценарии С1 (отклонение + заявка) и С3 (сбой записи).

Запуск: python tests/e2e_smoke.py [адрес] [ширина]
Скриншоты - в temp/e2e/.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
WIDTH = int(sys.argv[2]) if len(sys.argv) > 2 else 1280
OUT = Path(__file__).resolve().parent.parent / "temp" / "e2e"
OUT.mkdir(parents=True, exist_ok=True)


def run(page, vehicle_id: str, name: str, send: bool):
    page.goto(URL)
    page.fill("#q", vehicle_id)
    page.click(f'.veh[data-id="{vehicle_id}"]')
    page.click("#bDiag")
    page.wait_for_function("!busy", timeout=120_000)
    page.screenshot(path=str(OUT / f"{name}_{WIDTH}.png"), full_page=False)
    if send:
        assert page.locator('[data-tk="send"]:visible').count() > 0, "черновик заявки не виден"
        page.locator('[data-tk="send"]:visible').first.scroll_into_view_if_needed()
        page.screenshot(path=str(OUT / f"{name}_draft_{WIDTH}.png"))
    errors = page.locator(".note.n-bad").count()
    if send:
        btn = page.locator('[data-tk="send"]:visible').first
        btn.click()
        page.wait_for_selector("text=создана в ТОиР", timeout=30_000)
        page.screenshot(path=str(OUT / f"{name}_sent_{WIDTH}.png"))
    return errors


with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": WIDTH, "height": 900})
    logs = []
    page.on("console", lambda m: logs.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    print("С3 сбой записи, ошибок на экране:", run(page, "P456AT178", "c3_failed", False))
    print("С1 отклонение, ошибок на экране:", run(page, "B418HK98", "c1_anomaly", True))
    print("Ошибки консоли:", logs or "нет")
    b.close()
