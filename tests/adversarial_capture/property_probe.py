from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from student_execution_os.agent.nlparse import parse_task

OUT = Path("artifacts/capture-adversarial/property-testing.json")
TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=TZ)


def kind(parsed: dict) -> str:
    return parsed.get("kind") or "TASK"


def event_signature(parsed: dict) -> tuple:
    return (
        parsed.get("title"),
        parsed.get("category"),
        parsed.get("starts_at"),
        parsed.get("ends_at"),
        parsed.get("duration_minutes"),
    )


def main() -> None:
    subjects = ["созвон с Ариадной", "встреча с Димой", "лекция по матану", "тренировка"]
    dates = ["завтра", "послезавтра", "в пятницу"]
    times = ["в 10:00", "в 15:30", "в 18:00", "в 7 вечера"]
    durations = ["на 20 минут", "на полчаса", "на час"]
    offsets = [5, 20, 50, 60, 120]

    add_reminder = []
    interval_checks = []
    idx = 0
    for subject in subjects:
        for day in dates:
            for clock in times:
                duration = durations[idx % len(durations)]
                idx += 1
                base_text = f"{day} {clock} {subject} {duration}"
                base = parse_task(base_text, now=NOW, timezone_name="Europe/Moscow")
                if kind(base) == "EVENT":
                    start = datetime.fromisoformat(base["starts_at"])
                    end = datetime.fromisoformat(base["ends_at"])
                    interval_checks.append({
                        "utterance": base_text,
                        "pass": start < end and int((end - start).total_seconds() // 60) == base["duration_minutes"],
                        "actual": base,
                    })
                for offset in offsets:
                    text = f"{base_text}, напомни за {offset} минут до начала"
                    actual = parse_task(text, now=NOW, timezone_name="Europe/Moscow")
                    preserved = kind(base) == "EVENT" and kind(actual) == "EVENT" and event_signature(actual) == event_signature(base)
                    correct_lead = actual.get("remind_before_minutes") == offset
                    add_reminder.append({
                        "utterance": text,
                        "offset": offset,
                        "pass": preserved and correct_lead,
                        "base_kind": kind(base),
                        "actual_kind": kind(actual),
                        "base_signature": event_signature(base),
                        "actual_signature": event_signature(actual),
                        "actual_lead": actual.get("remind_before_minutes"),
                    })

    task_pairs = [
        ("сдать лабу завтра до 20:00, напомни сегодня в 17:00", "2026-09-30T20:00", "2026-09-29T17:00"),
        ("закончить отчёт в пятницу до 18:00, напомни завтра в 16:00", "2026-10-02T18:00", "2026-09-30T16:00"),
    ]
    task_checks = []
    for text, _deadline, _remind in task_pairs:
        actual = parse_task(text, now=NOW, timezone_name="Europe/Moscow")
        cutoff = actual.get("actual_cutoff") or {}
        task_checks.append({
            "utterance": text,
            "pass": cutoff.get("state") == "KNOWN" and actual.get("remind_at") is not None
                    and cutoff.get("at") != actual.get("remind_at"),
            "actual": actual,
        })

    results = {
        "baseline_sha": "7ba92ae0fa99c1526b083cd6bc8afed82dc993fc",
        "method": "deterministic generated property checks (Hypothesis-equivalent finite domain)",
        "properties": {
            "event_interval_consistency": {
                "cases": len(interval_checks),
                "passes": sum(x["pass"] for x in interval_checks),
                "failures": [x for x in interval_checks if not x["pass"]][:20],
            },
            "adding_relative_reminder_preserves_event_semantics": {
                "cases": len(add_reminder),
                "passes": sum(x["pass"] for x in add_reminder),
                "failures": [x for x in add_reminder if not x["pass"]][:30],
            },
            "task_deadline_and_reminder_are_distinct_roles": {
                "cases": len(task_checks),
                "passes": sum(x["pass"] for x in task_checks),
                "failures": [x for x in task_checks if not x["pass"]],
            },
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: {"cases": v["cases"], "passes": v["passes"]} for k, v in results["properties"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
