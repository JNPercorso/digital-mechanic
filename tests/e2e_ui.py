"""Сквозной тест интерфейса в браузере (Playwright): python tests/e2e_ui.py [адрес]

Проверяет то, что видит механик, на ширинах телефона и компьютера:
 1. Норма: ответ без заявки, кнопка «Показать снова результат диагностики».
 2. Сбой записи: модель не вызывается, заявки нет, есть «Повторить проверку».
 3. Низкая точность: предупреждение и галочка; без галочки отправить нельзя (и сервер отказывает);
    с галочкой - заявка уходит в ТОиР и появляется в списке заявок машины.
 4. Отклонение: результат с заголовком, черновик с запчастями или пометкой «не найдены»,
    ссылка открывает страницу руководства (текст по умолчанию, подсветка, картинка страницы, листание).
 5. Плашки над строкой ввода: появляются, когда заявка ушла из вида, и прокручивают к ней.
 6. После перезагрузки диалог восстановлен с устройства, кнопка - «Показать снова…».
 7. Вопрос не о технике - стандартный отказ; строка ввода закреплена внизу экрана.
 8. Телефон без выбранной машины: место под нижнюю навигацию не резервируется.
Итог печатается списком «да/НЕТ», скриншоты - в temp/e2e_ui/, отчёт - logs/e2e_ui.md.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "temp" / "e2e_ui"
SHOTS.mkdir(parents=True, exist_ok=True)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, bool(ok), note))
    print(f"{'да ' if ok else 'НЕТ'} {name}{' - ' + note if note else ''}", flush=True)


def open_vehicle(page, vid: str, fresh: bool = True) -> None:
    page.goto(URL)
    page.wait_for_function("typeof V !== 'undefined' && V.length > 0", timeout=30_000)
    if fresh:
        page.evaluate(f"localStorage.removeItem('dm_chat_{vid}')")
        page.goto(URL)
        page.wait_for_function("typeof V !== 'undefined' && V.length > 0", timeout=30_000)
    page.fill("#q", vid)
    page.click(f'.veh[data-id="{vid}"]')
    page.wait_for_selector("#bDiag", timeout=10_000)


def wait_done(page, timeout=180_000) -> None:
    page.wait_for_function("typeof busy !== 'undefined' && !busy", timeout=timeout)
    page.wait_for_timeout(700)


def diagnose(page) -> None:
    page.click("#bDiag")
    page.wait_for_function("busy", timeout=10_000)
    wait_done(page)


def first(vehicles, pred):
    return next(v for v in vehicles if pred(v))


def main() -> None:
    vs = json.loads(urllib.request.urlopen(URL + "/api/vehicles").read())
    last = lambda v: v["sessions"][-1]  # noqa: E731
    v_norm = first(vs, lambda v: last(v)["status"] == "normal")["vehicle_id"]
    v_fail = first(vs, lambda v: last(v)["status"] == "check_failed")["vehicle_id"]
    v_low = first(vs, lambda v: last(v)["status"] == "anomaly" and (last(v).get("confidence") or 1) < 0.6)["vehicle_id"]
    v_anom = first(vs, lambda v: last(v)["status"] == "anomaly" and (last(v).get("confidence") or 0) >= 0.8
                   and last(v)["anomaly_type"] in ("F01", "F09"))["vehicle_id"]
    print("машины:", v_norm, v_fail, v_low, v_anom)

    with sync_playwright() as p:
        b = p.chromium.launch()
        for width, height, tag in ((390, 844, "phone"), (1280, 900, "web")):
            page = b.new_page(viewport={"width": width, "height": height})
            page.on("pageerror", lambda e: results.append((f"ошибка JS: {e}", False, "")))
            try:
                if tag == "phone":
                    page.goto(URL)
                    page.wait_for_function("typeof V !== 'undefined' && V.length > 0", timeout=30_000)
                    pb = page.evaluate("getComputedStyle(document.querySelector('.app')).paddingBottom")
                    check(f"[{tag}] без выбранной машины нет отступа под навигацию", pb == "12px", pb)

                # 1. Норма
                open_vehicle(page, v_norm)
                diagnose(page)
                txt = page.inner_text("#chat")
                check(f"[{tag}] норма: ответ есть, заявки нет", "норм" in txt.lower()
                      and page.locator("#chat .chat-ticket .ticket").count() == 0)
                check(f"[{tag}] норма: кнопка «Показать снова результат диагностики»",
                      "Показать снова" in page.inner_text("#bDiag"))

                # 2. Сбой записи
                open_vehicle(page, v_fail)
                diagnose(page)
                check(f"[{tag}] сбой записи: заявки нет, есть «Повторить проверку»",
                      page.locator("#chat [data-retry]").count() > 0 and page.locator("#chat .chat-ticket .ticket").count() == 0)
                llm = page.evaluate("document.getElementById('sLlm') ? document.getElementById('sLlm').textContent : ''")
                # модель может вызываться один раз - чтобы агент сам запросил результат проверки; выводов после сбоя нет
                check(f"[{tag}] сбой записи: после результата модель не вызывалась", llm in ("0", "1", ""),
                      f"вызовов модели в сессии: {llm}")

                # 3. Низкая точность (только на одной ширине: заявка уходит в ТОиР один раз)
                if tag == "web":
                    open_vehicle(page, v_low)
                    diagnose(page)
                    tk = page.locator("#chat .chat-ticket .ticket").last
                    check(f"[{tag}] низкая точность: черновик с предупреждением и галочкой",
                          tk.count() > 0 and tk.locator("[data-lowok]").count() == 1)
                    send = tk.locator('[data-tk="send"]')
                    check(f"[{tag}] без галочки отправить нельзя", send.is_disabled())
                    dbid = page.evaluate("draft && draft.dbid")
                    req = urllib.request.Request(f"{URL}/api/tickets/{dbid}/send", data=b"{}", method="POST",
                                                 headers={"Content-Type": "application/json"})
                    try:
                        urllib.request.urlopen(req)
                        server_refused = False
                    except urllib.error.HTTPError as e:
                        server_refused = e.code == 409
                    check(f"[{tag}] сервер не принимает отправку без подтверждения", server_refused)
                    tk.locator("[data-lowok]").click()  # черновик перерисовывается после отметки
                    page.wait_for_timeout(300)
                    tk = page.locator("#chat .chat-ticket .ticket").last
                    tk.locator('[data-tk="send"]').click()
                    page.wait_for_selector("text=создана в ТОиР", timeout=30_000)
                    page.wait_for_timeout(800)
                    check(f"[{tag}] с галочкой заявка отправлена и видна в списке заявок машины",
                          "TKT-" in page.inner_text("#paneMain"))
                    page.screenshot(path=str(SHOTS / f"low_sent_{tag}.png"))

                # 4. Отклонение: результат, черновик, ссылка на страницу
                open_vehicle(page, v_anom)
                diagnose(page)
                check(f"[{tag}] отклонение: заголовок «Результат диагностики» с датой проверки",
                      page.locator("#chat .dhead").count() > 0 and "проверка" in page.inner_text("#chat .dhead"))
                tk = page.locator("#chat .chat-ticket .ticket").last
                parts = tk.inner_text() if tk.count() else ""
                check(f"[{tag}] черновик: запчасти или пометка «не найдены в руководстве»",
                      "Запчасти" in parts and (("не найдены" in parts) or ("." in parts.split("Запчасти", 1)[1][:200])))
                bottom = page.evaluate("(()=>{const pm=document.getElementById('paneMain');return Math.round(pm.scrollHeight-pm.clientHeight-pm.scrollTop)})()")
                check(f"[{tag}] после ответа чат прокручен вниз", bottom <= 4, f"до низа {bottom}px")
                page.screenshot(path=str(SHOTS / f"anomaly_{tag}.png"))
                src = page.locator("#chat .msg .src[data-doc]").first
                if src.count():
                    src.click()
                    page.wait_for_selector(".pv .page", timeout=15_000)
                    page.wait_for_timeout(600)
                    narrow = page.evaluate("!document.querySelector('.pv .card').classList.contains('wide')")
                    text_vis = page.locator(".pv .page").is_visible()
                    check(f"[{tag}] окно страницы: текст виден сразу", text_vis, "узкий экран" if narrow else "широкий")
                    check(f"[{tag}] окно страницы: есть подсвеченный фрагмент", page.locator(".pv mark").count() > 0)
                    if narrow:
                        page.click('.pv [data-tab="img"]')
                    page.wait_for_function("document.querySelector('.pv .pimg img') && document.querySelector('.pv .pimg img').naturalWidth > 0", timeout=15_000)
                    check(f"[{tag}] окно страницы: картинка страницы загружена", True)
                    page.screenshot(path=str(SHOTS / f"page_{tag}.png"))
                    n0 = page.inner_text(".pv .pnav .mut")
                    page.click('.pv [data-p="1"]')
                    page.wait_for_timeout(1200)
                    check(f"[{tag}] окно страницы: листание", page.inner_text(".pv .pnav .mut") != n0)
                    page.click(".pv [data-x]")
                else:
                    check(f"[{tag}] в ответе есть ссылка на страницу", False)

                # 5. Плашки
                page.evaluate("document.getElementById('paneMain').scrollTop = 0")
                page.wait_for_timeout(500)
                page.evaluate("document.getElementById('paneMain').scrollTop = document.getElementById('paneMain').scrollHeight")
                page.wait_for_timeout(500)
                page.evaluate("(()=>{const pm=document.getElementById('paneMain');const d=lastDiagEl();if(d)pm.scrollTop=d.offsetTop-40;})()")
                page.wait_for_timeout(600)
                has_pin = page.locator('[data-pin="tk"]').count() > 0
                if has_pin:
                    page.click('[data-pin="tk"]')
                    page.wait_for_timeout(1000)
                    under = page.evaluate("(()=>{const t=lastTkEl().getBoundingClientRect().top,c=document.getElementById('vcard').getBoundingClientRect().bottom;return Math.round(t-c)})()")
                    check(f"[{tag}] плашка «Заявка» прокручивает к черновику под карточкой машины", under >= 0, f"отступ {under}px")
                else:
                    check(f"[{tag}] плашка «Заявка» появляется, когда черновик ушёл из вида", False)

                # 6. Восстановление после перезагрузки
                open_vehicle(page, v_anom, fresh=False)
                page.wait_for_timeout(800)
                check(f"[{tag}] после перезагрузки диалог восстановлен",
                      page.locator("#chat .dhead").count() > 0 and "Показать снова" in page.inner_text("#bDiag"))

                # 7. Вопрос не о технике, строка ввода внизу
                page.fill("#ask", "Какая сегодня погода?")
                page.click("#bAsk")
                page.wait_for_function("busy", timeout=10_000)
                wait_done(page)
                check(f"[{tag}] вопрос не о технике - стандартный отказ",
                      "помогаю только по технике" in page.inner_text("#chat"))
                gap = page.evaluate("(()=>{const pm=document.getElementById('paneMain').getBoundingClientRect(),c=document.querySelector('.cbar').getBoundingClientRect();return Math.round(pm.bottom-c.bottom)})()")
                check(f"[{tag}] строка ввода прижата к низу экрана", abs(gap) <= 2, f"зазор {gap}px")
                page.screenshot(path=str(SHOTS / f"chat_{tag}.png"))
            except Exception as ex:  # тест не обрывается на первой ошибке
                check(f"[{tag}] сценарий прервался", False, repr(ex)[:200])
                page.screenshot(path=str(SHOTS / f"error_{tag}.png"))
            page.close()
        b.close()

    ok = sum(1 for _, x, _ in results if x)
    out = ROOT / "logs" / "e2e_ui.md"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# Тест интерфейса {time.strftime('%Y-%m-%d %H:%M')}\n\nПройдено {ok} из {len(results)}\n\n")
        f.writelines(f"- {'да' if x else 'НЕТ'}: {n}{' - ' + c if c else ''}\n" for n, x, c in results)
    print(f"Пройдено {ok} из {len(results)}. Отчёт: {out}")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
