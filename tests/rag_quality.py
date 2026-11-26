"""Качество ответов по руководствам (python tests/rag_quality.py): контрольные вопросы с эталоном.

Эталон взят со страниц руководств вручную: норматив и страницы PDF, где он написан.
Ответ засчитывается, если в нём есть эталонное число и ссылка хотя бы на одну эталонную страницу,
и нет ссылок на руководства другого двигателя. «Ловушки» - вопросы, ответа на которые в руководстве
этого двигателя нет: засчитывается честное «нет в руководстве» без выдуманных чисел.
Отчёт: logs/rag_quality.md. Приложение должно быть запущено.
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

# машина, вопрос, числа (достаточно одного), страницы-эталоны ("doc:стр")
CASES = [
    ("B418HK98", "Какие тепловые зазоры клапанов нужно выставить?", ["0,25", "0,20"], ["yamz236_re:31", "yamz236_rk:43", "yamz236_rk:269"]),
    ("B418HK98", "Какое давление масла должно быть на прогретом двигателе?", ["400"], ["yamz236_re:19", "yamz236_rk:267"]),
    ("B418HK98", "Какая нормальная температура охлаждающей жидкости?", ["75"], ["yamz236_re:19", "yamz236_re:4", "yamz236_rk:267"]),
    ("B418HK98", "Какое давление начала впрыскивания у форсунок?", ["22,6"], ["yamz236_re:8"]),
    ("C206CC178", "Какие тепловые зазоры клапанов нужно выставить?", ["0,30", "0,40"], ["yamz536_re:173"]),
    ("C206CC178", "Какое давление масла в системе смазки прогретого двигателя?", ["400", "635"], ["yamz536_re:49", "yamz536_re:158", "yamz536_re:159"]),
    ("C206CC178", "Какая рабочая температура охлаждающей жидкости?", ["80"], ["yamz536_re:5", "yamz536_re:158", "yamz536_re:159"]),
    ("M739BA198", "Какой зазор между электродами свечей зажигания?", ["0,85"], ["zmz5234_rr:9", "zmz5234_rr:158"]),
    ("M739BA198", "При какой температуре открывается термостат?", ["80"], ["zmz5234_rr:8", "zmz5234_rr:31"]),
    ("M739BA198", "Какой тепловой зазор между коромыслом и клапаном на холодном двигателе?", ["0,20", "0,30"], ["zmz5234_rr:21"]),
    ("Y815BM178", "Какой зазор между электродами свечей зажигания?", ["0,70", "0,85"], ["zmz409_re:25", "zmz409_re:77"]),
    ("Y815BM178", "Какое максимальное давление масла в системе смазки?", ["6,0"], ["zmz409_re:49"]),
    ("Y815BM178", "При какой температуре открывается основной клапан термостата?", ["82"], ["zmz409_re:25"]),
    ("Y815BM178", "Какой должен быть прогиб ремня привода водяного насоса?", ["14"], ["zmz409_re:99"]),
    # ловушки: у дизеля ЯМЗ нет свечей зажигания, у ЗМЗ нет форсунок высокого давления ТНВД
    ("B418HK98", "Какой зазор между электродами свечей зажигания?", None, []),
    ("M739BA198", "Какое давление начала впрыскивания у форсунок ТНВД?", None, []),
]
ENGINE_DOCS = {"B418HK98": "yamz236", "C206CC178": "yamz536", "M739BA198": "zmz5234", "Y815BM178": "zmz409"}
NOT_FOUND = re.compile(r"нет|не найден|не приведен|не указан|отсутству|не применя|не предусмотрен", re.I)


def ask(vid: str, q: str) -> list[dict]:
    body = json.dumps({"vehicle_id": vid, "text": q, "history": [], "diagnosed": True}).encode()
    req = urllib.request.Request(BASE + "/api/ask", data=body, method="POST", headers={"Content-Type": "application/json"})
    out = []
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode("utf-8")
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def main() -> None:
    rows, ok_n = [], 0
    for vid, q, nums, pages in CASES:
        evs = ask(vid, q)
        ans = next((e for e in reversed(evs) if e["type"] == "answer"), {"text": "", "refs": []})
        refs = {f"{r['doc_id']}:{r['page']}" for r in ans.get("refs", [])}
        foreign = [r for r in refs if not r.startswith(ENGINE_DOCS[vid])]
        text = ans["text"].replace(".", ",")
        if nums is None:  # ловушка
            ok = bool(NOT_FOUND.search(ans["text"])) and not foreign
            why = "честно: нет в руководстве" if ok else "выдумал или взял чужой двигатель"
        else:
            has_num = any(n in text for n in nums)
            has_page = bool(refs & set(pages))
            ok = has_num and has_page and not foreign
            why = f"число {'да' if has_num else 'НЕТ'}, эталонная страница {'да' if has_page else 'НЕТ'}" \
                  f"{', чужой двигатель: ' + ', '.join(foreign) if foreign else ''}; ссылки: {', '.join(sorted(refs)) or 'нет'}"
        ok_n += ok
        rows.append((vid, q, ok, why, ans["text"][:300].replace("\n", " ")))
        print(f"{'да ' if ok else 'НЕТ'} {vid} {q[:55]:55} | {why}", flush=True)
    out = ROOT / "logs" / "rag_quality.md"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# Качество ответов по руководствам {time.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"Верно: {ok_n} из {len(rows)}\n\n| Машина | Вопрос | Итог | Почему | Начало ответа |\n|---|---|---|---|---|\n")
        f.writelines(f"| {v} | {q} | {'да' if ok else 'НЕТ'} | {w} | {a} |\n" for v, q, ok, w, a in rows)
    print(f"Верно {ok_n} из {len(rows)}. Отчёт: {out}")


if __name__ == "__main__":
    main()
