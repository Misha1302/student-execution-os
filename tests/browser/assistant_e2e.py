"""The Assistant in the real UI against the real server (§19, §21, §23, §24, §54).

Only the language model is replaced: a deterministic server-side provider returns typed
intent. Previews, the follow-up session, apply, version checks and undo all run through
the production endpoints and the canonical database.
"""
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from playwright.sync_api import expect

from student_execution_os.agent.credentials import CredentialSource, LlmCredentialStore, ResolvedLlm
from tests.browser import final_student_e2e as student_journey

ZONE = ZoneInfo('Europe/Moscow')
TODAY = student_journey.NOW.astimezone(ZONE).replace(hour=0, minute=0)
GUARDED = {'kind': 'SHIFT_WITH_GUARD_AND_FALLBACK', 'delta_minutes': 180,
           'guard': {'not_after_local_time': '22:00'},
           'fallback': {'relative_day': 'NEXT_MORNING', 'preferred_local_time': '10:00', 'precision': 'APPROXIMATE'}}


def _action(command, payload, version=None, **extra):
    return {'command': command, 'payload': payload, 'confidence': 0.95, 'unresolved_fields': [],
            'expected_version': version, 'requires_confirmation': False, **extra}


class BrowserFixtureProvider:
    name = 'browser-fixture'
    model = 'fixture'

    def interpret(self, text, context):
        items = {item['title']: item for item in context['obligations']}
        if text.startswith('Что у меня сегодня вечером'):
            return {'message': 'facts', 'actions': [], 'read_query': {
                'kind': 'AGENDA_WINDOW', 'starts_at': (TODAY + timedelta(hours=18)).isoformat(),
                'ends_at': (TODAY + timedelta(hours=23, minutes=59)).isoformat()}}
        if text.startswith('Перенеси встречу с Ариадной'):
            meeting = items['Встреча с Ариадной']
            return {'message': 'proposal', 'actions': [_action(
                'RESCHEDULE', {'obligation_id': meeting['id'], 'temporal_transform': GUARDED}, meeting['version'])]}
        if text.startswith('Лучше в 10:30'):
            previous = context['assistant_session']['previous_actions'][0]['payload']
            when = datetime.fromisoformat(previous['when']).astimezone(ZONE).replace(hour=10, minute=30)
            meeting = items['Встреча с Ариадной']
            return {'message': 'corrected', 'actions': [_action(
                'RESCHEDULE', {'obligation_id': meeting['id'], 'when': when.isoformat()}, meeting['version'])]}
        if text.startswith('Перенеси пару'):
            lecture = items['Пара по физике']
            return {'message': 'move', 'actions': [_action(
                'RESCHEDULE', {'obligation_id': lecture['id'], 'when': (TODAY + timedelta(hours=21)).isoformat()},
                lecture['version'])]}
        if text.startswith('Перенеси встречу с Димой'):
            meeting = items['Встреча с Димой']
            return {'message': 'plan', 'actions': [
                _action('RESCHEDULE', {'obligation_id': meeting['id'],
                                       'when': (TODAY + timedelta(hours=18)).isoformat()}, meeting['version'],
                        client_ref='move'),
                _action('UPDATE_EVENT', {'obligation_id': meeting['id'], 'remind_before_minutes': 20},
                        meeting['version'], client_ref='remind', depends_on=['move']),
                _action('CREATE_EVENT', {'title': 'Отчёт', 'duration_minutes': 60,
                                         'relative_to': {'action': 'move', 'anchor': 'END', 'offset_minutes': 0}},
                        client_ref='report', depends_on=['move']),
            ]}
        raise ValueError('fixture does not understand this text')


class AssistantBrowserTest(unittest.TestCase):
    rows = student_journey.FinalStudentJourneyTest.rows

    @classmethod
    def setUpClass(cls):
        student_journey.FinalStudentJourneyTest.setUpClass.__func__(cls)
        registered = cls.http.post('/api/v1/auth/register', json={'login': 'assistant-qa', 'password': 'local-qa-password-only'})
        registered.raise_for_status()
        cls.auth = registered.json()
        cls.headers = {'Authorization': f"Bearer {cls.auth['token']}"}
        operations = [{'op_id': f'assistant-seed-{index}', 'type': 'event.create', 'entity_id': entity, 'payload': {
            'title': title, 'starts_at': (TODAY + timedelta(hours=start)).isoformat(),
            'ends_at': (TODAY + timedelta(hours=start + 1)).isoformat()}}
            for index, (entity, title, start) in enumerate((
                ('event-ariadne', 'Встреча с Ариадной', 20), ('event-dima', 'Встреча с Димой', 15),
                ('event-seminar', 'Семинар', 19), ('event-lecture', 'Пара по физике', 11)))]
        seeded = cls.http.post('/api/v1/sync', headers=cls.headers, json={'operations': operations})
        seeded.raise_for_status()
        assert all(result['status'] == 'APPLIED' for result in seeded.json()['results']), seeded.text
        with closing(sqlite3.connect(cls.database)) as connection, connection:  # an imported-schedule lecture
            connection.execute(
                "INSERT INTO external_identities(account_id,source_system_id,external_uid,local_kind,local_id,"
                "first_seen_at,last_seen_at) VALUES (?, 'ical:hse', 'uid-lecture', 'EVENT', 'event-lecture', ?, ?)",
                (cls.auth['user']['account_id'], TODAY.isoformat(), TODAY.isoformat()))
        cls.model = patch.object(LlmCredentialStore, 'resolve', lambda self, account_id: ResolvedLlm(
            source=CredentialSource.PLATFORM_MANAGED, provider=BrowserFixtureProvider()))
        cls.model.start()

    @classmethod
    def tearDownClass(cls):
        cls.model.stop()
        student_journey.FinalStudentJourneyTest.tearDownClass.__func__(cls)

    def page(self):
        context = self.browser.new_context(viewport={'width': 390, 'height': 844}, timezone_id='Europe/Moscow',
                                           reduced_motion='reduce')
        page = context.new_page()
        self.addCleanup(context.close)
        values = {'seos.locale': 'ru', 'seos.token': self.auth['token'], 'seos.user': json.dumps(self.auth['user'])}
        page.add_init_script(f"const values = {json.dumps(values)}; for (const [k, v] of Object.entries(values)) localStorage.setItem(k, v);")
        self.errors = []
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        page.route(self.origin + '/api/v1/ask/capabilities', lambda route: route.fulfill(json={'live_llm_provider': True}))
        page.goto(self.origin)
        page.locator('#workspace[data-view-state="ready"]').wait_for()
        return page

    def say(self, page, text, sheet=None):
        if sheet is None:
            page.locator('.fab').click()
            sheet = page.locator('dialog.sheet[open]')
        sheet.locator('#capture-text').fill(text)
        return sheet

    def starts(self, entity):
        row = self.rows('SELECT starts_at FROM events WHERE obligation_id=?', entity)
        return datetime.fromisoformat(row[0][0]).astimezone(ZONE).strftime('%d %H:%M')

    def test_question_shows_server_facts_and_changes_nothing(self):
        page = self.page()
        before = self.rows('SELECT count(*) FROM obligations')[0][0]
        sheet = self.say(page, 'Что у меня сегодня вечером?')
        card = sheet.locator('.read-card')
        expect(card).to_contain_text('Семинар')
        expect(card).to_contain_text('Встреча с Ариадной')
        expect(card).not_to_contain_text('Встреча с Димой')  # 15:00 is not evening
        expect(sheet.locator('[data-create]')).to_be_disabled()
        self.assertEqual(self.rows('SELECT count(*) FROM obligations')[0][0], before)
        self.assertEqual(self.errors, [])

    def test_guarded_move_refined_by_follow_up_applied_and_undone(self):
        page = self.page()
        sheet = self.say(page, 'Перенеси встречу с Ариадной на три часа позже. Но если позже 22:00, то на утро, часов на 10 примерно.')
        card = sheet.locator('.command-card')
        expect(card).to_contain_text('было')
        expect(card).to_contain_text('примерно')
        expect(card).to_contain_text('10:00')
        card.locator('[data-refine]').click()
        expect(sheet.locator('[data-follow-up]')).to_be_visible()
        self.say(page, 'Лучше в 10:30', sheet)
        expect(sheet.locator('.command-card')).to_contain_text('10:30')
        expect(sheet.locator('.command-card')).not_to_contain_text('примерно')
        sheet.locator('.command-card [data-run]').click()
        sheet.wait_for(state='detached')
        self.assertEqual(self.starts('event-ariadne'), '29 10:30')
        page.locator('.toast-action').click()
        expect(page.locator('.toast').filter(has_text='Отменено')).to_be_visible()
        self.assertEqual(self.starts('event-ariadne'), '28 20:00')
        self.assertEqual(self.errors, [])

    def test_imported_event_move_is_explained_not_offered(self):
        page = self.page()
        sheet = self.say(page, 'Перенеси пару по физике на вечер')
        card = sheet.locator('.command-card')
        expect(card.locator('[data-blocked="IMPORTED_EVENT_SOURCE_OWNED"]')).to_be_visible()
        expect(card.locator('[data-run]')).to_be_disabled()
        self.assertEqual(self.starts('event-lecture'), '28 11:00')
        self.assertEqual(self.errors, [])

    def test_dependent_plan_is_previewed_applied_and_undone_as_one(self):
        page = self.page()
        sheet = self.say(page, 'Перенеси встречу с Димой на шесть, напомни о ней за двадцать минут, а после неё поставь час на отчёт')
        rows = sheet.locator('.command-card .command-row')
        expect(rows).to_have_count(3)
        expect(rows.nth(2)).to_contain_text('Отчёт')
        expect(rows.nth(2)).to_contain_text('19:00')  # derived from the moved meeting's end
        sheet.locator('.command-card [data-run]').click()
        sheet.wait_for(state='detached')
        self.assertEqual(self.starts('event-dima'), '28 18:00')
        report = self.rows("SELECT o.id FROM obligations o WHERE o.title='Отчёт' AND o.lifecycle_status='ACTIVE'")
        self.assertEqual(len(report), 1)
        self.assertEqual(self.starts(report[0][0]), '28 19:00')
        page.locator('.toast-action').click()
        expect(page.locator('.toast').filter(has_text='Отменено')).to_be_visible()
        self.assertEqual(self.starts('event-dima'), '28 15:00')
        self.assertEqual(self.rows("SELECT count(*) FROM obligations WHERE title='Отчёт' AND lifecycle_status='ACTIVE'")[0][0], 0)
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
