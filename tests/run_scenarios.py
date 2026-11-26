"""Прогон сценариев на живом приложении с подробным журналом (python tests/run_scenarios.py [--all] [--ask]).

По умолчанию - «Получить результат диагностики» для каждой машины с отклонением или сбоем проверки
(с --all - для всех машин), с --ask - ещё контрольный вопрос механика по каждой машине с отклонением.
Для каждого запуска сохраняется полный поток событий (SSE), записи журнала правил из базы
(отклонённые ссылки, убранные строки, отброшенные работы) и сводка.

Результат: logs/scenarios/<время>/events.jsonl (все события), summary.csv (по строке на запуск),
report.md (что нарушено и где). Приложение должно быть запущено (python run.py).
"""
import csv
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import settings  # noqa: E402

BASE = "http://localhost:8000"
ASK = "Как проверить этот узел и какие нормативы по руководству?"
# Контрольные вопросы механика (с --ask задаются по очереди): по узлу, о технике вперемешку с посторонним,
# совсем не о технике, об истории машины, о противоречии в руководствах
QUESTIONS = [
    ASK,
    "А какая температура двигателя считается нормальной и какая температура погоды считается хорошей?",
    "Какая сегодня погода?",
    "Когда были прошлые проверки этой машины и что нашли?",
    "Тепловые зазоры клапанов регулировать на горячем или на холодном двигателе?",
    # пограничный: похоже на погоду, но это вопрос о технике (пуск в мороз)
    "На улице минус 30, можно ли заводить двигатель и что сделать перед пуском?",
]
RULE_EVENTS = ("rejected_citations", "rejected_refs", "rejected_operations", "removed_unsupported",
               "removed_unverified", "rule_search_required", "low_confidence_confirmed", "crash")


def sse(url: str, body: dict | None = None) -> list[dict]:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(BASE + url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"} if data else {})
    out = []
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode("utf-8")
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def rule_log(request_id: str) -> list[dict]:
    db = sqlite3.connect(settings.DB_PATH)
    rows = db.execute(f"SELECT kind, data FROM events WHERE request_id=? AND kind IN ({','.join('?' * len(RULE_EVENTS))})",
                      (request_id, *RULE_EVENTS)).fetchall()
    return [{"kind": k, "data": json.loads(d)} for k, d in rows]


def summarize(kind: str, v: dict, evs: list[dict]) -> dict:
    last = v["sessions"][-1]
    m = next((e for e in reversed(evs) if e["type"] == "metrics"), {})
    ans = [e for e in evs if e["type"] == "answer"]
    draft = next((e for e in evs if e["type"] == "ticket_draft"), None)
    rules = rule_log(m.get("request_id", "")) if m else []
    return {
        "scenario": kind, "question": "", "vehicle": v["vehicle_id"], "engine": v["engine_model"], "status": last["status"],
        "fault": last.get("anomaly_type"), "score": last.get("anomaly_score"), "confidence": last.get("confidence"),
        "signal": last.get("signal_quality"),
        "warning": " | ".join(e["text"] for e in evs if e["type"] == "warning"),
        "draft": bool(draft), "urgency": draft["payload"]["urgency"] if draft else "",
        "no_ticket": any(e["type"] == "no_ticket" for e in evs),
        "refs": len(ans[-1]["refs"]) if ans else 0, "rejected_in_answer": ans[-1].get("rejected", 0) if ans else 0,
        "rule_events": ";".join(sorted({r["kind"] for r in rules})),
        "error": " | ".join(e.get("text", "") for e in evs if e["type"] == "error"),
        "seconds": m.get("seconds"), "llm_calls": m.get("llm_calls"), "tokens_in": m.get("tokens_in"),
        "tokens_out": m.get("tokens_out"), "cost_rub": m.get("cost_rub"),
        "_rules": rules, "_answer": ans[-1]["text"] if ans else "",
    }


def problems(s: dict) -> list[str]:
    """Нарушения требований заказчика, которые видно по журналу."""
    p = []
    if s["status"] == "check_failed" and (s["draft"] or s["llm_calls"]):
        p.append("сбой записи, но модель вызвана или есть заявка")
    if s["signal"] == "warning" and not s["warning"]:
        p.append("запись с помехами без предупреждения")
    if s["confidence"] is not None and s["confidence"] < 0.5 and s["draft"]:
        p.append("заявка при точности ниже 0,50")
    if s["scenario"] == "diagnose" and s["status"] == "anomaly" and not s["draft"] and not s["no_ticket"] \
            and not (s["confidence"] is not None and s["confidence"] < 0.5):
        p.append("нет ни заявки, ни объяснения, почему её нет")
    if s["status"] == "normal" and s["draft"]:
        p.append("заявка при норме")
    if s["error"]:
        p.append("ошибка: " + s["error"])
    if s["rule_events"]:
        p.append("сработали правила: " + s["rule_events"])
    return p


def main() -> None:
    out = ROOT / "logs" / "scenarios" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    vehicles = json.loads(urllib.request.urlopen(BASE + "/api/vehicles").read())
    if "--all" not in sys.argv:
        vehicles = [v for v in vehicles if v["sessions"][-1]["status"] != "normal"]
    runs = []
    with open(out / "events.jsonl", "w", encoding="utf-8") as f:
        for v in vehicles:
            plan = [("diagnose", f"/api/diagnose/{v['vehicle_id']}", None)]
            if "--ask" in sys.argv and v["sessions"][-1]["status"] == "anomaly":
                q = QUESTIONS[len(runs) % len(QUESTIONS)]
                plan.append(("ask", "/api/ask", {"vehicle_id": v["vehicle_id"], "text": q}))
            for kind, url, body in plan:
                try:
                    evs = sse(url, body)
                except Exception as ex:  # сценарий не должен останавливать прогон
                    evs = [{"type": "error", "text": repr(ex)}]
                s = summarize(kind, v, evs)
                s["question"] = (body or {}).get("text", "")
                s["problems"] = problems(s)
                runs.append(s)
                f.write(json.dumps({"scenario": kind, "vehicle": v["vehicle_id"], "events": evs,
                                    "rule_log": s["_rules"]}, ensure_ascii=False) + "\n")
                print(f"{kind:8} {v['vehicle_id']:10} {s['status']:12} draft={s['draft']!s:5} "
                      f"{s['seconds']}s {s['cost_rub']}₽ {'; '.join(s['problems'])}", flush=True)
    cols = [k for k in runs[0] if not k.startswith("_")] if runs else []
    with open(out / "summary.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for s in runs:
            w.writerow({**s, "problems": " | ".join(s["problems"])})
    with open(out / "report.md", "w", encoding="utf-8") as f:
        n = len(runs)
        cost = sum(s["cost_rub"] or 0 for s in runs)
        f.write(f"# Прогон сценариев\n\nЗапусков: {n}, стоимость {cost:.2f} ₽, "
                f"среднее время {sum(s['seconds'] or 0 for s in runs) / max(n, 1):.1f} с\n\n")
        for s in runs:
            if s["problems"]:
                f.write(f"## {s['scenario']} {s['vehicle']} ({s['engine']}, {s['status']} {s['fault'] or ''})\n\n")
                f.write("".join(f"- {x}\n" for x in s["problems"]))
                for r in s["_rules"]:
                    f.write(f"  - `{r['kind']}`: {json.dumps(r['data'], ensure_ascii=False)[:600]}\n")
                f.write("\n")
    print("Журнал:", out)


if __name__ == "__main__":
    main()
