"""Reminder engine: evaluates every open task of an account and turns the policy's
per-task decisions into user-facing messages.

Account-level rules live here, not in the per-task policy:
* quiet hours hold every prompt until they end;
* prompts to one account are spaced by ``ACCOUNT_MIN_GAP`` (urgent ones excepted);
* a daily cap bounds non-urgent prompts;
* prompts due in the same tick are grouped into one notification.

Held prompts are not recorded as sent, so the policy re-decides them later with
fresh task state instead of delivering something that may have become stale.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository

from .messages import compose, compose_standalone
from .policy import ACCOUNT_MIN_GAP, OPEN_STATUSES, Decision, ReminderState, Stage, TaskFacts, decide, is_urgent
from .store import ReminderStore

log = logging.getLogger("student_execution_os.reminders")


@dataclass(frozen=True)
class TickResult:
    account_id: str
    evaluated: int
    messages: list[str]
    held_reason: str | None = None


def task_facts(repo: SQLiteCanonicalRepository, account_id: str, now: datetime) -> list[TaskFacts]:
    """Open tasks with the planner's risk and latest-safe-start where available."""
    from student_execution_os.planning import PlanningService, SQLitePlanningStateSource, build_planning_snapshot
    from student_execution_os.planning.model import PlanningPolicy
    from student_execution_os.planning.outlook import SQLitePlanningProfileRepository, off_hours_constraints

    source = SQLitePlanningStateSource(repo)
    tasks = [task for task in source.list_tasks(account_id) if task.obligation.lifecycle_status.value in {"ACTIVE", "DRAFT"}]
    risks: dict[str, object] = {}
    minute = now.replace(second=0, microsecond=0)
    try:
        profile = SQLitePlanningProfileRepository(repo).get(account_id)
        snapshot = build_planning_snapshot(
            source, account_id=account_id,
            analysis_horizon_start=minute, analysis_horizon_end=minute + timedelta(hours=48),
            plan_output_horizon_end=minute + timedelta(hours=36),
            policy=PlanningPolicy(version=f"reminders-v1:{profile.version}:{profile.optional_event_policy}",
                                  optional_event_policy=profile.optional_event_policy),
            derived_constraints=lambda start, end: off_hours_constraints(profile, account_id, start, end),
            assume_attendance=profile.optional_event_policy == "FAIL_CLOSED",
        )
        risks = {risk.task_id: risk for risk in PlanningService().build(snapshot, now=minute).risks}
    except Exception:  # planning failure must not silence deadline reminders
        log.exception("reminder planning failed for account %s", account_id)
    facts = []
    for task in tasks:
        risk = risks.get(task.obligation.id)
        facts.append(TaskFacts(
            task_id=task.obligation.id, title=task.obligation.title,
            status=task.obligation.lifecycle_status.value,
            cutoff_at=task.actual_cutoff.at, target_at=task.target_at,
            latest_safe_start=getattr(risk, "latest_safe_start", None),
            risk_state=getattr(getattr(risk, "state", None), "value", None),
            remaining_minutes=task.remaining_effort_minutes,
            started_at=task.started_at, last_progress_at=task.last_progress_at,
            actionable_from=task.actionable_from, importance=task.obligation.importance.value,
        ))
    facts.extend(event_facts(repo, account_id, minute))
    return facts


def event_facts(repo: SQLiteCanonicalRepository, account_id: str, now: datetime) -> list[TaskFacts]:
    """Active fixed-time events around now; only those with a reminder request can fire."""
    rows = repo.connection.execute(
        "SELECT o.id,o.title,o.lifecycle_status,e.starts_at,e.ends_at FROM obligations o "
        "JOIN events e ON e.obligation_id=o.id JOIN reminder_states r ON r.account_id=o.account_id AND r.task_id=o.id "
        "WHERE o.account_id=? AND o.kind='EVENT' AND o.lifecycle_status='ACTIVE' AND r.remind_at IS NOT NULL "
        "AND e.ends_at>?",
        (account_id, (now - timedelta(hours=1)).isoformat()),
    ).fetchall()
    return [TaskFacts(
        task_id=row["id"], title=row["title"], status=row["lifecycle_status"],
        cutoff_at=None, target_at=datetime.fromisoformat(row["starts_at"]), latest_safe_start=None, risk_state=None,
        remaining_minutes=None, started_at=None, last_progress_at=None, actionable_from=None,
        kind="EVENT", ends_at=datetime.fromisoformat(row["ends_at"]),
    ) for row in rows]


class ReminderEngine:
    def __init__(self, database: str) -> None:
        self.database = database

    def tick_all(self, now: datetime) -> list[TickResult]:
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(now)) as repo:
            repo.initialize()
            accounts = [row[0] for row in repo.connection.execute("SELECT id FROM accounts ORDER BY id")]
        results = []
        for account_id in accounts:
            try:
                results.append(self.tick(account_id, now))
            except Exception:
                log.exception("reminder tick failed for account %s", account_id)
        return results

    def tick(self, account_id: str, now: datetime) -> TickResult:
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(now)) as repo:
            repo.initialize()
            return self._tick(repo, account_id, now)

    def _tick(self, repo: SQLiteCanonicalRepository, account_id: str, now: datetime) -> TickResult:
        store = ReminderStore(repo)
        prefs = store.prefs(account_id)
        states = store.states(account_id)
        facts = task_facts(repo, account_id, now)
        by_id = {item.task_id: item for item in facts}
        decisions: dict[str, Decision] = {}
        for item in facts:
            if item.status not in OPEN_STATUSES:
                continue
            decisions[item.task_id] = decide(item, states.get(item.task_id), prefs, now)
        # Tasks that disappeared from the open set: close their episodes.
        for task_id, state in states.items():
            if task_id not in decisions and state.closed_reason is None:
                status = by_id[task_id].status if task_id in by_id else "CLOSED"
                store.save_state(account_id, task_id, replace(state, closed_reason=status, next_check_at=None))

        sending = {task_id: d for task_id, d in decisions.items() if d.stage is not None}
        held_reason = None
        hold_until: datetime | None = None
        if sending:
            quiet_end = prefs.quiet_until(now)
            last = store.last_message_at(account_id)
            sent_today = store.count_since(account_id, now - timedelta(days=1))
            urgent = {task_id for task_id, d in sending.items() if is_urgent(d.stage, d.reason)}
            # A reminder the user explicitly asked for goes out at the requested moment,
            # past quiet hours, the account gap and the daily cap.
            requested = {task_id for task_id, d in sending.items() if d.reason == "REMINDER_DUE"}
            urgent |= requested
            if quiet_end is not None:
                held_reason, hold_until = "QUIET_HOURS", quiet_end
                sending = {k: v for k, v in sending.items() if k in requested}
            else:
                if last is not None and now - last < ACCOUNT_MIN_GAP:
                    held_reason, hold_until = "ACCOUNT_GAP", last + ACCOUNT_MIN_GAP
                    sending = {k: v for k, v in sending.items() if k in urgent}
                if sent_today >= prefs.profile.daily_cap:
                    held_reason, hold_until = "DAILY_CAP", now + timedelta(hours=1)
                    sending = {k: v for k, v in sending.items() if k in urgent}

        for task_id, decision in decisions.items():
            if task_id in sending:
                continue
            state = decision.state
            if decision.stage is not None:
                # Held: persist the pre-send state and look again when the hold ends.
                previous = states.get(task_id) or ReminderState.fresh(decision.state.episode_key)
                if previous.episode_key != decision.state.episode_key:
                    previous = ReminderState.fresh(decision.state.episode_key, last_interaction_at=previous.last_interaction_at)
                state = replace(previous, next_check_at=hold_until)
            store.save_state(account_id, task_id, state)

        messages: list[str] = self._fire_standalone(repo, store, prefs, account_id, now)
        if sending:
            ordered = sorted(sending, key=lambda tid: (not is_urgent(sending[tid].stage, sending[tid].reason),
                                                       by_id[tid].due_at or datetime.max.replace(tzinfo=timezone.utc)))
            if len(ordered) == 1:
                task_id = ordered[0]
                decision = sending[task_id]
                repeat = decision.stage is Stage.START_NOW and (states.get(task_id) or decision.state).last_stage == Stage.START_NOW.value
                content = compose(decision.stage, [by_id[task_id]], repeat=repeat, now=now,
                                  timezone_name=prefs.timezone_name, locale=prefs.locale)
                stage = decision.stage.value
            else:
                content = compose(Stage.GROUP, [by_id[tid] for tid in ordered], repeat=False, now=now,
                                  timezone_name=prefs.timezone_name, locale=prefs.locale)
                stage = Stage.GROUP.value
            dedupe = hashlib.sha256(
                "|".join([account_id, stage, now.isoformat(), *ordered]).encode()
            ).hexdigest()[:32]
            with repo._tx():
                message_id = store.add_message(account_id, stage=stage, task_ids=ordered, content=content,
                                               dedupe_key=dedupe, now=now)
                for task_id in ordered:
                    store.save_state(account_id, task_id, sending[task_id].state)
            if message_id:
                messages.append(message_id)
        return TickResult(account_id, len(decisions), messages, held_reason)

    @staticmethod
    def _fire_standalone(repo, store: ReminderStore, prefs, account_id: str, now: datetime) -> list[str]:
        """Standalone reminders whose moment came: one message each, at that moment.

        The user named the moment, so quiet hours, spacing and the daily cap do not
        hold it (like a snoozed task reminder). A reminder missed for longer than the
        grace period (the worker was down) is closed without a stale notification.
        """
        from .standalone import FIRE_GRACE, SQLiteReminderRepository
        reminders = SQLiteReminderRepository(repo)
        sent: list[str] = []
        for reminder in reminders.due(account_id, now):
            at = datetime.fromisoformat(reminder["remind_at"])
            with repo._tx():
                if now - at <= FIRE_GRACE:
                    content = compose_standalone(reminder, now=now, timezone_name=prefs.timezone_name, locale=prefs.locale)
                    message_id = store.add_message(
                        account_id, stage="REMINDER", task_ids=[], content=content,
                        dedupe_key=f"standalone:{reminder['id']}:{reminder['remind_at']}", now=now,
                        reminder_id=reminder["id"], delivery=reminder["delivery"],
                    )
                    if message_id:
                        sent.append(message_id)
                reminders.mark_fired(account_id, reminder["id"], now)
        return sent
