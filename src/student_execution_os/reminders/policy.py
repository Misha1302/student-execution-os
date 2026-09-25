"""Pure reminder decision policy.

A reminder is a *new user-facing prompt*, decided from the task's execution state,
the reminder history of its current episode and the user's preferences. It is
deliberately separate from delivery retry (see ``push.py``), which only re-sends a
message that was already decided.

The policy answers, per task and per tick: should a prompt go out now, which one,
and when is it worth looking again. It never mutates anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.domain.errors import ValidationError


class Stage(StrEnum):
    START_SOON = "START_SOON"        # latest safe start is approaching
    START_NOW = "START_NOW"          # latest safe start reached, not started (repeats with backoff)
    CHECK_IN = "CHECK_IN"            # started, no activity for a while (repeats with backoff)
    DONE_CHECK = "DONE_CHECK"        # no remaining effort left but still open
    DEADLINE_24H = "DEADLINE_24H"    # hard deadline within a day, not started
    DEADLINE_2H = "DEADLINE_2H"      # hard deadline within two hours, work remains
    RISK_UP = "RISK_UP"              # planner risk got worse
    OVERDUE = "OVERDUE"              # deadline passed while the task is still open
    GROUP = "GROUP"                  # several tasks need attention in one tick
    REMINDER = "REMINDER"            # the user asked to be reminded now (snooze end, "напомни …")


OPEN_STATUSES = {"ACTIVE", "DRAFT"}
REPEATING = {Stage.START_NOW, Stage.CHECK_IN}
URGENT = {Stage.DEADLINE_2H, Stage.OVERDUE}
RISK_RANK = {"SAFE": 0, "NOT_APPLICABLE": 0, "UNKNOWN": 0, "START_SOON": 1, "AT_RISK": 2, "CRITICAL": 3, "IMPOSSIBLE": 4, "OVERDUE": 4}

# Priority when several stages apply to one task at the same moment.
_PRIORITY = [Stage.OVERDUE, Stage.DEADLINE_2H, Stage.RISK_UP, Stage.DONE_CHECK, Stage.START_NOW,
             Stage.CHECK_IN, Stage.DEADLINE_24H, Stage.START_SOON]


@dataclass(frozen=True)
class Intensity:
    max_unanswered: int      # repeating prompts sent without any reaction before we stop nagging
    base_gap: timedelta      # gap after the first unanswered prompt; doubles per ignored prompt
    daily_cap: int           # prompts per account per 24h (urgent ones may exceed it)
    start_lead: timedelta    # heads-up before the latest safe start
    check_in_after: timedelta  # silence after which an in-progress task gets a check-in


INTENSITIES = {
    "GENTLE": Intensity(1, timedelta(hours=3), 4, timedelta(minutes=15), timedelta(hours=4)),
    "NORMAL": Intensity(3, timedelta(minutes=90), 8, timedelta(minutes=30), timedelta(hours=3)),
    "PERSISTENT": Intensity(5, timedelta(minutes=45), 14, timedelta(minutes=45), timedelta(hours=2)),
}
MIN_TASK_GAP = timedelta(minutes=15)
RECENT_TOUCH = timedelta(minutes=10)   # the user is looking at the task right now
INFORMATIONAL = {Stage.START_SOON, Stage.DEADLINE_24H}
MAX_REPEAT_GAP = timedelta(hours=6)
# A requested reminder that could not go out (server down, task deferred …) is still
# worth sending this long after its moment; older ones are dropped silently.
REMIND_GRACE = timedelta(hours=12)
ACCOUNT_MIN_GAP = timedelta(minutes=10)


def parse_clock(value: str) -> time:
    try:
        result = time.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("quiet hour must be HH:MM") from exc
    if result.second or result.microsecond:
        raise ValidationError("quiet hours use minute precision")
    return result


@dataclass(frozen=True)
class ReminderPrefs:
    enabled: bool = True
    intensity: str = "NORMAL"
    timezone_name: str = "UTC"
    quiet_starts_local: str = "22:00"
    quiet_ends_local: str = "08:00"
    locale: str = "ru"
    version: int = 1

    def __post_init__(self) -> None:
        if self.intensity not in INTENSITIES:
            raise ValidationError("intensity must be GENTLE, NORMAL or PERSISTENT")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError("unknown IANA timezone") from exc
        if parse_clock(self.quiet_starts_local) == parse_clock(self.quiet_ends_local):
            raise ValidationError("quiet hours must not start and end at the same minute")
        if self.locale not in {"ru", "en"}:
            raise ValidationError("locale must be ru or en")

    @property
    def profile(self) -> Intensity:
        return INTENSITIES[self.intensity]

    def quiet_until(self, when: datetime) -> datetime | None:
        """End of the quiet period containing ``when``; None when not quiet."""
        zone = ZoneInfo(self.timezone_name)
        local = when.astimezone(zone)
        start, end = parse_clock(self.quiet_starts_local), parse_clock(self.quiet_ends_local)
        current = local.time().replace(second=0, microsecond=0)
        overnight = start > end
        quiet = (start <= current < end) if not overnight else (current >= start or current < end)
        if not quiet:
            return None
        end_date = local.date() + timedelta(days=1) if overnight and current >= start else local.date()
        naive = datetime.combine(end_date, end)
        for shift in range(0, 181):  # first valid local minute after a DST gap
            probe = naive + timedelta(minutes=shift)
            aware = probe.replace(tzinfo=zone)
            if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == probe:
                return aware.astimezone(timezone.utc)
        raise ValidationError("could not resolve quiet-hours end")


@dataclass(frozen=True)
class TaskFacts:
    task_id: str
    title: str
    status: str
    cutoff_at: datetime | None
    target_at: datetime | None
    latest_safe_start: datetime | None
    risk_state: str | None
    remaining_minutes: int | None
    started_at: datetime | None
    last_progress_at: datetime | None
    actionable_from: datetime | None

    @property
    def episode_key(self) -> str:
        return f"{self.cutoff_at.isoformat() if self.cutoff_at else '-'}|{self.target_at.isoformat() if self.target_at else '-'}"

    @property
    def due_at(self) -> datetime | None:
        return self.cutoff_at or self.target_at


@dataclass(frozen=True)
class ReminderState:
    episode_key: str
    sent_count: int = 0
    ignored_count: int = 0
    last_sent_at: datetime | None = None
    last_stage: str | None = None
    last_risk: str | None = None
    stages_sent: frozenset[str] = field(default_factory=frozenset)
    last_interaction_at: datetime | None = None
    snoozed_until: datetime | None = None
    closed_reason: str | None = None
    next_check_at: datetime | None = None
    remind_at: datetime | None = None

    @classmethod
    def fresh(cls, episode_key: str, *, last_interaction_at: datetime | None = None,
              remind_at: datetime | None = None) -> "ReminderState":
        return cls(episode_key=episode_key, last_interaction_at=last_interaction_at, remind_at=remind_at)

    def remind_due(self, now: datetime) -> bool:
        """A requested reminder moment has come and nothing was sent since it."""
        return (self.remind_at is not None and self.remind_at <= now and now - self.remind_at <= REMIND_GRACE
                and (self.last_sent_at is None or self.last_sent_at < self.remind_at))

    def pending_remind(self, now: datetime) -> datetime | None:
        if self.remind_at is not None and self.remind_at > now and (self.last_sent_at is None or self.last_sent_at < self.remind_at):
            return self.remind_at
        return None


@dataclass(frozen=True)
class Decision:
    stage: Stage | None
    state: ReminderState          # state to persist (already reflecting a send, if any)
    reason: str                   # short machine reason, useful in diagnostics/tests


def _unanswered(state: ReminderState) -> int:
    """Consecutive prompts since the user last touched the task."""
    if state.last_sent_at is None:
        return 0
    if state.last_interaction_at is not None and state.last_interaction_at >= state.last_sent_at:
        return 0
    return max(state.ignored_count, 1)


def repeat_gap(prefs: ReminderPrefs, ignored: int, now: datetime, due_at: datetime | None) -> timedelta:
    gap = prefs.profile.base_gap * (2 ** max(0, ignored - 1))
    gap = min(gap, MAX_REPEAT_GAP)
    if due_at is not None and due_at > now:
        # Still come back before the deadline: never wait longer than half the time left.
        gap = min(gap, (due_at - now) / 2)
    return max(gap, MIN_TASK_GAP)


def _earliest(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def decide(facts: TaskFacts, previous: ReminderState | None, prefs: ReminderPrefs, now: datetime) -> Decision:
    """Decide the reminder for one task at ``now`` (ignoring quiet hours and caps,
    which are account-level and applied by the engine)."""
    state = previous or ReminderState.fresh(facts.episode_key)
    if not state.episode_key:
        # Created by a user action (touch) before the engine ever looked at the task.
        state = replace(state, episode_key=facts.episode_key)
    elif state.episode_key != facts.episode_key or (
        facts.status in OPEN_STATUSES and state.closed_reason in {"COMPLETED", "CANCELLED"}
    ):
        # Rescheduled or reopened: the old escalation history no longer describes this task.
        # A reminder the user asked for and has not received yet still stands.
        unanswered_request = state.remind_at is not None and (state.last_sent_at is None or state.last_sent_at < state.remind_at)
        state = ReminderState.fresh(facts.episode_key, last_interaction_at=state.last_interaction_at,
                                    remind_at=state.remind_at if unanswered_request else None)
    unanswered = _unanswered(state)
    state = replace(state, ignored_count=unanswered)

    if facts.status not in OPEN_STATUSES:
        return Decision(None, replace(state, closed_reason=state.closed_reason or facts.status, next_check_at=None), "NOT_ACTIVE")
    if not prefs.enabled:
        return Decision(None, replace(state, next_check_at=None), "DISABLED")
    pending = state.pending_remind(now)
    if state.snoozed_until is not None and state.snoozed_until > now:
        return Decision(None, replace(state, next_check_at=_earliest(state.snoozed_until, pending)), "SNOOZED")

    candidates, wakeups = _candidates(facts, state, prefs, now)
    if state.remind_due(now):
        # The user asked for this moment (snooze end, "напомни …", "not now"): send it
        # even if the same stage already went out, the episode was closed or the task
        # was touched a minute ago. The prompt still carries the current facts.
        chosen = next((stage for stage in _PRIORITY if stage in candidates), Stage.REMINDER)
        return Decision(chosen, _sent(state, chosen, candidates, wakeups, facts, prefs, now, unanswered), "REMINDER_DUE")

    if state.closed_reason == "OVERDUE_NOTIFIED":
        return Decision(None, replace(state, next_check_at=pending), "EPISODE_CLOSED")
    if facts.actionable_from is not None and facts.actionable_from > now:
        return Decision(None, replace(state, next_check_at=_earliest(facts.actionable_from, pending)), "DEFERRED")

    if state.last_interaction_at is not None and now - state.last_interaction_at < RECENT_TOUCH:
        # Whatever the user just saw in the app is the baseline; don't push about it.
        return Decision(None, replace(state, last_risk=facts.risk_state or state.last_risk,
                                      next_check_at=_earliest(state.last_interaction_at + RECENT_TOUCH, pending)), "RECENTLY_TOUCHED")

    profile = prefs.profile
    due = facts.due_at
    if pending is not None:
        wakeups.append(pending)

    chosen: Stage | None = None
    reason = "NOTHING_DUE"
    next_repeat: datetime | None = None
    for stage in _PRIORITY:
        if stage not in candidates:
            continue
        if stage in REPEATING:
            if unanswered >= profile.max_unanswered:
                reason = "GAVE_UP_UNANSWERED"
                continue
            if state.last_sent_at is not None:
                gap = repeat_gap(prefs, unanswered, now, due) if state.last_stage == stage.value else MIN_TASK_GAP
                ready_at = state.last_sent_at + gap
                if ready_at > now:
                    next_repeat = _earliest(next_repeat, ready_at)
                    reason = "WAITING_REPEAT_GAP"
                    continue
        else:
            if stage.value in state.stages_sent and stage is not Stage.RISK_UP:
                continue  # one-shot stages fire once per episode
            if state.last_sent_at is not None and now - state.last_sent_at < MIN_TASK_GAP:
                next_repeat = _earliest(next_repeat, state.last_sent_at + MIN_TASK_GAP)
                reason = "WAITING_MIN_GAP"
                continue
        chosen = stage
        reason = candidates[stage]
        break

    if chosen is None:
        next_check = _earliest(next_repeat, *[w for w in wakeups if w > now])
        # A pending risk escalation keeps the old baseline so it still fires later.
        risk = state.last_risk if Stage.RISK_UP in candidates else (facts.risk_state or state.last_risk)
        return Decision(None, replace(state, last_risk=risk, next_check_at=next_check), reason)
    return Decision(chosen, _sent(state, chosen, candidates, wakeups, facts, prefs, now, unanswered), reason)


def _sent(state: ReminderState, chosen: Stage, candidates: dict[Stage, str], wakeups: list[datetime],
          facts: TaskFacts, prefs: ReminderPrefs, now: datetime, unanswered: int) -> ReminderState:
    """State after sending ``chosen`` now."""
    profile = prefs.profile
    # The prompt that goes out already conveys any informational stage that applies now.
    covered = {stage.value for stage in candidates if stage in INFORMATIONAL}
    sent = replace(
        state,
        sent_count=state.sent_count + 1,
        ignored_count=unanswered + 1,
        last_sent_at=now,
        last_stage=chosen.value,
        last_risk=facts.risk_state or state.last_risk,
        stages_sent=state.stages_sent | {chosen.value} | covered,
        closed_reason="OVERDUE_NOTIFIED" if chosen is Stage.OVERDUE else state.closed_reason,
    )
    follow_up = None
    if chosen in REPEATING and sent.ignored_count < profile.max_unanswered:
        follow_up = now + repeat_gap(prefs, sent.ignored_count, now, facts.due_at)
    return replace(sent, next_check_at=_earliest(follow_up, *[w for w in wakeups if w > now + MIN_TASK_GAP]))


def _candidates(facts: TaskFacts, state: ReminderState, prefs: ReminderPrefs, now: datetime) -> tuple[dict[Stage, str], list[datetime]]:
    """Stages that apply to the task right now, and the moments worth looking again."""
    profile = prefs.profile
    due = facts.due_at
    started = facts.started_at is not None or facts.last_progress_at is not None
    remaining = facts.remaining_minutes
    last_activity = _earliest_latest(facts.last_progress_at, facts.started_at, state.last_interaction_at)
    risk_rank = RISK_RANK.get(facts.risk_state or "UNKNOWN", 0)
    prev_risk_rank = RISK_RANK.get(state.last_risk or "UNKNOWN", 0)
    candidates: dict[Stage, str] = {}
    wakeups: list[datetime] = []

    cutoff = facts.cutoff_at
    if cutoff is not None and cutoff <= now:
        candidates[Stage.OVERDUE] = "CUTOFF_PASSED"
    elif cutoff is not None:
        left = cutoff - now
        if left <= timedelta(hours=2) and (remaining is None or remaining > 0):
            candidates[Stage.DEADLINE_2H] = "CUTOFF_WITHIN_2H"
        else:
            wakeups.append(cutoff - timedelta(hours=2))
        if left <= timedelta(hours=24) and not started:
            candidates[Stage.DEADLINE_24H] = "CUTOFF_WITHIN_24H"
        elif left > timedelta(hours=24):
            wakeups.append(cutoff - timedelta(hours=24))
        wakeups.append(cutoff)

    if remaining == 0:
        candidates[Stage.DONE_CHECK] = "NO_REMAINING_EFFORT"
    if risk_rank >= RISK_RANK["AT_RISK"] and risk_rank > prev_risk_rank:
        candidates[Stage.RISK_UP] = f"RISK_{facts.risk_state}"

    lss = facts.latest_safe_start
    if not started and lss is not None and (remaining is None or remaining > 0):
        if now >= lss:
            candidates[Stage.START_NOW] = "PAST_LATEST_SAFE_START"
        elif now >= lss - profile.start_lead:
            candidates[Stage.START_SOON] = "LATEST_SAFE_START_SOON"
            wakeups.append(lss)
        else:
            wakeups.append(lss - profile.start_lead)
    if started and (remaining is None or remaining > 0) and last_activity is not None:
        relevant = (due is not None and due - now <= timedelta(hours=48)) or (lss is not None and now >= lss)
        if relevant:
            quiet_for = now - last_activity
            if quiet_for >= profile.check_in_after:
                candidates[Stage.CHECK_IN] = "NO_ACTIVITY"
            else:
                wakeups.append(last_activity + profile.check_in_after)

    return candidates, wakeups


def _earliest_latest(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def is_urgent(stage: Stage) -> bool:
    return stage in URGENT
