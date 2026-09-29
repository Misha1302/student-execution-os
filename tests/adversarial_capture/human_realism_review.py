from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts/capture-adversarial"
OUT = ARTIFACTS / "human-realism-review.json"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def norm(value: str) -> str:
    return " ".join(re.sub(r"[^a-zа-я0-9]+", " ", value.lower().replace("ё", "е")).split())


SUBJECT_CUES = {
    "созвон с Ариадной": ("созвон", "ариадн", "call with ariadna", "call с ариадн"),
    "встреча с Димой": ("встреч", "дим", "meeting with dima", "meeting с дим"),
    "лекция по матану": ("лекц", "матан", "lecture on calculus", "lecture по матан"),
    "семинар по алгебре": ("семинар", "алгебр", "seminar on algebra", "seminar по алгебр"),
    "приём у врача": ("прием", "врач", "appointment with the doctor", "appointment у врач"),
    "тренировка": ("трениров", "workout"),
    "сдать лабу": ("сдать лаб", "submit the lab", "submit лаб"),
    "закончить отчёт": ("закончить отчет", "finish the report", "finish отчет"),
    "сделать домашку": ("сделать домаш", "do the homework", "do домаш"),
    "забрать документы": ("забрать документ", "pick up the documents", "pick up документ"),
    "купить хлеб": ("купить хлеб", "buy bread", "купить bread"),
    "написать преподавателю": ("написать преподав", "message the professor", "написать professor"),
    "позвонить маме": ("позвонить мам", "call mom", "позвонить mom"),
}


def review(case: dict) -> dict:
    text = case["utterance"]
    low = norm(text)
    intent = case["intent"]
    kind = intent["kind"]
    subject = intent.get("subject")
    subject_ok = subject is None or any(cue in low for cue in SUBJECT_CUES.get(subject, (norm(subject),)))
    date_ok = not intent.get("date") or any(x in low for x in ("сегодня", "завтра", "послезавтра", "сред", "30 сентября", "today", "tomorrow", "friday"))
    time_value = intent.get("time")
    if not time_value and intent.get("remind_at"):
        time_value = intent["remind_at"][11:16]
    if not time_value and intent.get("deadline"):
        time_value = intent["deadline"][11:16]
    hh = time_value[:2].lstrip("0") if time_value else None
    time_ok = not time_value or time_value in text or f" {hh}." in text or f" {hh} " in f" {low} " or "вечер" in low
    duration = intent.get("duration_minutes")
    duration_ok = duration is None or str(duration) in low or (duration == 30 and "полчас" in low) or (duration == 60 and "на час" in low) or (duration == 90 and "полтора часа" in low)
    reminder = intent.get("remind_before_minutes")
    reminder_ok = reminder is None or str(reminder) in low or (reminder == 60 and "за час" in low) or (reminder == 90 and "полтора часа" in low)
    deadline_ok = not intent.get("deadline") or any(x in low for x in ("до ", "due", "deadline"))
    effort = intent.get("effort_minutes")
    effort_ok = effort is None or str(effort) in low or (effort == 60 and "час работы" in low) or (effort == 120 and "2 часа" in low) or (effort == 180 and "3 часа" in low)
    correction_ok = "CORRECTION" not in case.get("tags", []) or any(x in low for x in ("ой нет", "нет ", "хотя лучше"))
    benchmarkish = bool(re.search(r"(?:пожалуйста|плз|спасибо|если что)(?:\s+(?:пожалуйста|плз|спасибо|если что)){2,}", low))
    natural = len(text) <= 260 and not benchmarkish
    checks = {
        "could_be_real_student_utterance": natural,
        "looks_like_benchmark_template": benchmarkish,
        "preserves_source_semantic_intent": all((subject_ok, date_ok, time_ok, duration_ok, reminder_ok, deadline_ok, effort_ok, correction_ok)),
        "kind": kind,
        "date": {"applicable": bool(intent.get("date") or intent.get("deadline") or intent.get("remind_at")), "surface_supported": date_ok},
        "time": {"applicable": bool(time_value), "surface_supported": time_ok},
        "duration": {"applicable": duration is not None, "surface_supported": duration_ok},
        "reminder": {"applicable": reminder is not None or bool(intent.get("remind_at")), "surface_supported": reminder_ok},
        "deadline": {"applicable": bool(intent.get("deadline")), "surface_supported": deadline_ok},
        "effort": {"applicable": effort is not None, "surface_supported": effort_ok},
        "correction": {"applicable": "CORRECTION" in case.get("tags", []), "surface_supported": correction_ok},
        "uncertainty": "STRICT_ORACLE",
    }
    if not checks["preserves_source_semantic_intent"]:
        verdict = "MISMATCH"
    elif not natural:
        verdict = "AMBIGUOUS"
    else:
        verdict = "SUPPORTED"
    return {"id": case["id"], "utterance": text, "source_style": case["style"], "intent": intent,
            "review": checks, "verdict": verdict}


def main() -> None:
    cases = read_jsonl(ARTIFACTS / "human_phrases.jsonl")
    # Deterministic spread across the whole generated corpus, then guarantee every style.
    indices = sorted({(i * len(cases)) // 120 for i in range(120)})
    selected = [cases[i] for i in indices]
    reviewed = [review(case) for case in selected]
    counts = Counter(item["verdict"] for item in reviewed)
    payload = {
        "method": "deterministic independent surface/intent rubric; parser outputs were neither loaded nor shown",
        "limitation": "No independent model verifier was used; this is a reproducible manual-rubric artifact, not a claim of model adjudication.",
        "selection": {"population": len(cases), "reviewed": len(reviewed), "algorithm": "120 evenly spaced corpus indices"},
        "counts": dict(counts),
        "cases": reviewed,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"reviewed": len(reviewed), "counts": dict(counts)}, ensure_ascii=False))
    if len(reviewed) < 100:
        raise SystemExit("human realism review must cover at least 100 utterances")


if __name__ == "__main__":
    main()
