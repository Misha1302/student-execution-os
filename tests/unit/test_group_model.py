"""Normative state machines, capabilities and typed payloads of collaborative groups."""
from __future__ import annotations

import unittest

from student_execution_os.domain.errors import ValidationError
from student_execution_os.groups.model import (
    ASSESSMENT_KINDS,
    Attendance,
    Capability,
    Criticality,
    EventKind,
    GroupJoinPolicy,
    GroupSettings,
    GroupStatus,
    IllegalTransition,
    MembershipStatus,
    ProposalStatus,
    Role,
    SharedKind,
    SharedStatus,
    UserGroupPreferences,
    UserSharedEventState,
    can_rejoin,
    capabilities,
    effective_attendance,
    parse_payload,
    patch_event_state,
    require_group_transition,
    require_membership_transition,
    require_proposal_transition,
    require_shared_transition,
)

S = MembershipStatus


class StateMachineTest(unittest.TestCase):
    def test_group(self):
        for current, target in ((GroupStatus.ACTIVE, GroupStatus.ARCHIVED), (GroupStatus.ARCHIVED, GroupStatus.ACTIVE),
                                (GroupStatus.ACTIVE, GroupStatus.DELETED), (GroupStatus.ARCHIVED, GroupStatus.DELETED)):
            require_group_transition(current, target)
        for target in GroupStatus:
            with self.assertRaises(IllegalTransition):
                require_group_transition(GroupStatus.DELETED, target)

    def test_membership(self):
        allowed = {(S.PENDING, S.ACTIVE), (S.PENDING, S.REMOVED), (S.PENDING, S.BLOCKED), (S.PENDING, S.LEFT),
                   (S.ACTIVE, S.LEFT), (S.ACTIVE, S.REMOVED), (S.ACTIVE, S.BLOCKED),
                   (S.LEFT, S.ACTIVE), (S.LEFT, S.PENDING), (S.LEFT, S.BLOCKED),
                   (S.REMOVED, S.ACTIVE), (S.REMOVED, S.PENDING), (S.REMOVED, S.BLOCKED),
                   (S.BLOCKED, S.ACTIVE), (S.BLOCKED, S.REMOVED)}
        for current in S:
            for target in S:
                if (current, target) in allowed:
                    require_membership_transition(current, target)
                else:
                    with self.assertRaises(IllegalTransition, msg=f"{current}->{target}"):
                        require_membership_transition(current, target)

    def test_rejoin_follows_the_join_policy(self):
        strict, lenient = GroupJoinPolicy(), GroupJoinPolicy(allow_rejoin_after_removal=True)
        self.assertTrue(can_rejoin(S.LEFT, strict))
        self.assertFalse(can_rejoin(S.REMOVED, strict))
        self.assertTrue(can_rejoin(S.REMOVED, lenient))
        self.assertFalse(can_rejoin(S.BLOCKED, lenient))  # only an explicit admin action

    def test_shared_entities_and_terminal_states(self):
        for kind, closed in ((SharedKind.SHARED_EVENT, SharedStatus.CANCELLED), (SharedKind.SHARED_OBLIGATION, SharedStatus.CANCELLED),
                             (SharedKind.SHARED_ANNOUNCEMENT, SharedStatus.RETRACTED)):
            require_shared_transition(kind, SharedStatus.PUBLISHED, SharedStatus.PUBLISHED)
            require_shared_transition(kind, SharedStatus.PUBLISHED, closed)
            with self.assertRaises(IllegalTransition):
                require_shared_transition(kind, closed, SharedStatus.PUBLISHED)
        with self.assertRaises(IllegalTransition):
            require_shared_transition(SharedKind.SHARED_EVENT, SharedStatus.PUBLISHED, SharedStatus.RETRACTED)

    def test_proposal(self):
        for target in (ProposalStatus.APPROVED, ProposalStatus.REJECTED, ProposalStatus.WITHDRAWN):
            require_proposal_transition(ProposalStatus.PENDING, target)
            for later in ProposalStatus:
                with self.assertRaises(IllegalTransition):
                    require_proposal_transition(target, later)


class CapabilityTest(unittest.TestCase):
    def test_role_table(self):
        settings = GroupSettings()
        member, scheduler, admin, owner = (capabilities(r, settings) for r in (Role.MEMBER, Role.SCHEDULER, Role.ADMIN, Role.OWNER))
        self.assertIn(Capability.CREATE_PROPOSAL, member)
        self.assertNotIn(Capability.PUBLISH_SHARED, member)
        self.assertTrue({Capability.PUBLISH_SHARED, Capability.MODERATE_PROPOSALS} <= scheduler)
        self.assertNotIn(Capability.MANAGE_MEMBERS, scheduler)
        self.assertTrue({Capability.MANAGE_MEMBERS, Capability.MANAGE_SETTINGS, Capability.PUBLISH_SHARED} <= admin)
        self.assertNotIn(Capability.MANAGE_ROLES, admin)
        self.assertNotIn(Capability.DELETE_GROUP, admin)
        self.assertEqual(owner, frozenset(Capability))

    def test_members_publish_only_when_the_group_allows_it(self):
        self.assertIn(Capability.PUBLISH_SHARED, capabilities(Role.MEMBER, GroupSettings(allow_members_to_publish=True)))
        self.assertNotIn(Capability.MODERATE_PROPOSALS, capabilities(Role.MEMBER, GroupSettings(allow_members_to_publish=True)))

    def test_authorization_never_compares_roles(self):
        """Capability checks only: no ordering of roles anywhere in the group package."""
        import pathlib
        import re
        package = pathlib.Path(__file__).resolve().parents[2] / "src/student_execution_os/groups"
        for path in package.glob("*.py"):
            self.assertIsNone(re.search(r"role\s*[<>]=?|Role\.\w+\s*[<>]", path.read_text()), path.name)


class PayloadTest(unittest.TestCase):
    base = {"kind": "SHARED_EVENT", "title": "КР", "event_kind": "CONTROL_WORK",
            "starts_at": "2026-10-15T12:10:00+03:00", "ends_at": "2026-10-15T13:30:00+03:00"}

    def test_tagged_union_with_assessment_defaults(self):
        payload = parse_payload(self.base, default_timezone="Europe/Moscow")
        self.assertEqual((payload.group_criticality, payload.attendance_default, payload.timezone),
                         (Criticality.CRITICAL, Attendance.REQUIRED, "Europe/Moscow"))
        lecture = parse_payload({**self.base, "event_kind": "LECTURE"}, default_timezone="UTC")
        self.assertEqual((lecture.group_criticality, lecture.attendance_default), (Criticality.NORMAL, Attendance.PREFERRED))
        self.assertEqual(ASSESSMENT_KINDS, {EventKind.QUIZ, EventKind.TEST, EventKind.CONTROL_WORK, EventKind.COLLOQUIUM, EventKind.EXAM})

    def test_rejections(self):
        for bad in ({**self.base, "kind": "EVENT"}, {**self.base, "starts_at": "2026-10-15T12:10"},
                    {**self.base, "ends_at": self.base["starts_at"]}, {**self.base, "extra": True},
                    {**self.base, "timezone": "Mars/Base"}, {"kind": "SHARED_ANNOUNCEMENT", "title": "x"}):
            with self.assertRaises(ValidationError, msg=bad):
                parse_payload(bad, default_timezone="UTC")
        with self.assertRaises(ValidationError):
            parse_payload(self.base, default_timezone="UTC", expected_kind=SharedKind.SHARED_OBLIGATION)

    def test_typed_settings_reject_unknown_fields(self):
        with self.assertRaises(ValidationError):
            GroupSettings().patched({"whatever": 1})
        with self.assertRaises(ValidationError):
            GroupJoinPolicy().patched({"approval_required": "yes"})


class OverlayTest(unittest.TestCase):
    def test_attendance_and_criticality_are_independent_and_personal(self):
        state = patch_event_state(UserSharedEventState("a", "e"), {"attendance_override": "SKIP"})
        self.assertIsNone(state.criticality_override)
        prefs = UserGroupPreferences("a", "g", default_attendance_behavior=Attendance.OPTIONAL)
        self.assertEqual(effective_attendance(Attendance.PREFERRED, prefs, None, EventKind.LECTURE), Attendance.OPTIONAL)
        # The personal default for classes never lowers an assessment.
        self.assertEqual(effective_attendance(Attendance.REQUIRED, prefs, None, EventKind.EXAM), Attendance.REQUIRED)
        self.assertEqual(effective_attendance(Attendance.REQUIRED, prefs, state, EventKind.EXAM), Attendance.SKIP)

    def test_last_seen_never_goes_back_and_unknown_fields_fail(self):
        state = patch_event_state(UserSharedEventState("a", "e", last_seen_version=5), {"last_seen_version": 2})
        self.assertEqual(state.last_seen_version, 5)
        with self.assertRaises(ValidationError):
            patch_event_state(state, {"title": "not mine to change"})


if __name__ == "__main__":
    unittest.main()
