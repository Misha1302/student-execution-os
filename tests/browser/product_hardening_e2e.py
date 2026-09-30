import json
import os
from time import perf_counter
from pathlib import Path
import unittest

from playwright.sync_api import expect

from tests.browser import final_student_e2e as student_journey


class ProductHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        student_journey.FinalStudentJourneyTest.setUpClass.__func__(cls)
        result = cls.http.post('/api/v1/auth/register', json={'login': 'hardening-qa', 'password': 'local-qa-password-only'})
        result.raise_for_status()
        cls.auth = result.json()

    @classmethod
    def tearDownClass(cls):
        student_journey.FinalStudentJourneyTest.tearDownClass.__func__(cls)

    def page(self, width=390, height=844, locale='ru', theme='light', authenticate=True):
        context = self.browser.new_context(viewport={'width': width, 'height': height}, timezone_id='Europe/Moscow', reduced_motion='reduce')
        page = context.new_page()
        self.addCleanup(context.close)
        values = {'seos.locale': locale, 'seos.theme': theme}
        if authenticate:
            values.update({'seos.token': self.auth['token'], 'seos.user': json.dumps(self.auth['user'])})
        page.add_init_script(f"const values = {json.dumps(values)}; for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value);")
        self.errors = []
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        page.goto(self.origin)
        page.locator('#workspace[data-view-state="ready"]').wait_for()
        return page

    def capture(self, page, text):
        page.locator('.fab').click()
        sheet = page.locator('dialog.sheet[open]')
        sheet.locator('#capture-text').fill(text)
        sheet.locator('.capture-card').wait_for()
        return sheet

    def screenshot(self, page, name):
        directory = os.environ.get('UI_QA_SCREENSHOT_DIR')
        if directory:
            Path(directory).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(directory) / name), full_page=page.locator('dialog[open]').count() == 0)

    def wait_requests(self, page, pending, count):
        for attempt in range(150):
            if len(pending) >= count:
                return
            page.wait_for_timeout(20)
        self.fail(f'Expected {count} requests; got {len(pending)}')

    def test_capture_latency_against_real_interpret_endpoint(self):
        page = self.page(locale='en')
        page.route(self.origin + '/api/v1/ask/capabilities', lambda route: route.fulfill(json={'live_llm_provider': True}))
        requests = []
        page.on('request', lambda request: requests.append(perf_counter()) if request.url.endswith('/assistant/interpret') else None)
        started = perf_counter()
        sheet = self.capture(page, 'Meeting tomorrow at 18:00 for half an hour')
        preview_ms = round((perf_counter() - started) * 1000)
        self.wait_requests(page, requests, 1)
        for attempt in range(30):
            if sheet.locator('[data-engine]').get_attribute('data-engine') != 'thinking':
                break
            page.wait_for_timeout(20)
        self.assertEqual(len(requests), 1)
        print(json.dumps({'capture_preview_ms': preview_ms, 'interpret_request_ms': round((requests[0] - started) * 1000)}))

    def test_fresh_registration_first_value_and_welcome(self):
        for locale, theme in (('ru', 'light'), ('en', 'dark')):
            page = self.page(320, 700, locale, theme, authenticate=False)
            self.screenshot(page, f'hardening-welcome-320-{locale}-{theme}.png')
            page.context.close()
        page = self.page(authenticate=False)
        page.fill('input[name="login"]', 'hardening-first-value')
        page.fill('input[name="password"]', 'local-first-value-only')
        page.fill('input[name="password2"]', 'local-first-value-only')
        page.locator('button[type="submit"]').click()
        sheet = page.locator('dialog.sheet[open]')
        sheet.locator('[data-guidance]').wait_for()
        self.screenshot(page, 'hardening-fresh-first-run.png')
        sheet.locator('#capture-text').fill('Купить молоко')
        expect(sheet.locator('[data-create]')).to_be_enabled()
        expect(sheet.locator('.capture-card')).to_contain_text('Предварительно')
        self.screenshot(page, 'hardening-first-real-interpretation.png')
        sheet.locator('[data-create]').click()
        sheet.wait_for(state='detached')
        expect(page.locator('.toast')).to_contain_text('Сегодня')
        self.assertEqual(self.errors, [])

    def test_voice_semantic_turns_cancel_and_manual_authority(self):
        page = self.page()
        page.add_init_script("""window.SpeechRecognition = class {
          start() { window.__speech = this; queueMicrotask(() => this.onstart?.()); }
          stop() { this.onend?.(); }
          abort() { this.onend?.(); }
        };""")
        page.reload()
        page.locator('.fab').click()
        sheet = page.locator('dialog.sheet[open]')

        def say(text):
            sheet.locator('[data-mic]').click()
            expect(sheet.locator('[data-mic]')).to_have_attribute('aria-pressed', 'true')
            page.evaluate("text => window.__speech.onresult({resultIndex: 0, results: [{0: {transcript: text}, isFinal: true}]})", text)
            self.screenshot(page, 'hardening-voice-recording.png')
            sheet.locator('[data-voice-stop]').click()
            expect(sheet.locator('[data-mic]')).to_have_attribute('aria-pressed', 'false')

        say('Завтра в шесть созвон с Ариадной на полчаса, напомни за 50 минут')
        expect(sheet.locator('.event-card')).to_be_visible()
        sheet.locator('[data-fact="title"]').click()
        editor = page.locator('dialog[data-inline-editor]')
        editor.locator('[data-inline-title]').fill('Моё название')
        editor.locator('[data-inline-save]').click()
        editor.wait_for(state='detached')
        say('Нет, не завтра, а в пятницу')
        expect(sheet.locator('.capture-title')).to_contain_text('Моё название')
        expect(sheet.locator('[data-event-when]')).to_contain_text('18:00–18:30')
        say('И напомни за час')
        expect(sheet.locator('[data-reminder-editor] summary')).to_contain_text('60 мин')
        say('без напоминания')
        self.assertNotIn('60 мин', sheet.locator('[data-reminder-editor] summary').inner_text())
        before = sheet.locator('#capture-text').input_value()
        sheet.locator('[data-mic]').click()
        sheet.locator('[data-voice-cancel]').click()
        expect(sheet.locator('#capture-text')).to_have_value(before)
        say('нет, это задача')
        self.assertEqual(sheet.locator('[data-chip-group="capture-kind"] .on').get_attribute('data-value'), 'TASK')
        self.assertEqual(self.errors, [])

    def test_late_ai_and_concrete_clarification(self):
        page = self.page()
        page.route(self.origin + '/api/v1/ask/capabilities', lambda route: route.fulfill(json={'live_llm_provider': True}))
        pending = []
        page.route(self.origin + '/api/v1/assistant/interpret', lambda route: pending.append(route))
        sheet = self.capture(page, 'Созвон завтра в 18:00 на полчаса')
        self.wait_requests(page, pending, 1)
        sheet.locator('[data-fact="title"]').click()
        editor = page.locator('dialog[data-inline-editor]')
        editor.locator('[data-inline-title]').fill('Ручное название')
        editor.locator('[data-inline-save]').click()
        editor.wait_for(state='detached')
        sheet.locator('#capture-text').fill('Созвон завтра в 18:00 на полчаса, напомни за 50 минут')
        result = {'engine': 'AI', 'model': 'test-model', 'batch_id': 'ai-fixture', 'actions': [{'id': 'action', 'command': 'CREATE_EVENT', 'confidence': 0.95, 'unresolved_fields': [], 'payload': {'title': 'Название ИИ', 'starts_at': '2026-10-02T15:00:00Z', 'ends_at': '2026-10-02T15:30:00Z', 'duration_minutes': 30, 'remind_before_minutes': 50}}]}
        pending[0].fulfill(json=result)
        expect(sheet.locator('[data-clarification]')).to_be_empty()
        self.wait_requests(page, pending, 2)
        pending[1].fulfill(json=result)
        expect(sheet.locator('[data-create]')).to_be_disabled()
        expect(sheet.locator('[data-meaning]')).to_have_count(2)
        self.assertNotIn('локальный', sheet.locator('[data-clarification]').inner_text().lower())
        self.screenshot(page, 'hardening-concrete-clarification.png')
        sheet.locator('[data-meaning="1"]').click()
        expect(sheet.locator('.capture-title')).to_contain_text('Ручное название')
        expect(sheet.locator('[data-create]')).to_be_enabled()
        self.assertEqual(json.loads(sheet.get_attribute('data-field-provenance'))['title'], 'USER_EDIT')
        self.assertEqual(self.errors, [])

    def test_today_action_precedes_passive_status_and_more_is_accessible(self):
        self.http.post('/api/v1/tasks', headers={'Authorization': 'Bearer ' + self.auth['token']}, json={
            'title': 'Купить молоко', 'provisional_effort': True, 'actual_cutoff': {'state': 'ABSENT'},
        }).raise_for_status()
        page = self.page()
        card = page.locator('.now-card').first
        card.wait_for()
        self.assertLess(card.bounding_box()['y'], page.locator('[data-planner-status]').bounding_box()['y'])
        self.assertEqual(card.locator('.now-actions > button').count(), 2)
        card.locator('.now-actions details summary').click()
        expect(card.locator('[data-action="defer-task"]')).to_be_visible()
        self.assertEqual(page.locator('#workspace').get_attribute('data-view'), 'today')
        self.assertEqual(self.errors, [])

    def test_latest_explicit_correction_beats_machine_and_note_is_first_class(self):
        page = self.page()
        page.route(self.origin + '/api/v1/ask/capabilities', lambda route: route.fulfill(json={'live_llm_provider': True}))
        candidate = {'engine': 'AI', 'batch_id': 'turn-authority', 'actions': [{'id': 'turn', 'command': 'CREATE_EVENT',
                     'confidence': 0.95, 'unresolved_fields': [], 'payload': {'title': 'Созвон',
                     'starts_at': '2026-10-01T15:00:00Z', 'ends_at': '2026-10-01T15:30:00Z'}}]}
        page.route(self.origin + '/api/v1/assistant/interpret', lambda route: route.fulfill(json=candidate))
        sheet = self.capture(page, 'Созвон завтра в 18:00 на полчаса. Нет, не завтра, а в пятницу')
        expect(sheet.locator('[data-event-when]')).to_contain_text('Пт')
        page.wait_for_timeout(400)
        expect(sheet.locator('[data-create]')).to_be_enabled()
        expect(sheet.locator('[data-clarification]')).to_be_empty()
        candidate['actions'] = [{'id': 'note', 'command': 'CREATE_NOTE', 'confidence': 0.95,
                                 'unresolved_fields': [], 'payload': {'content': 'Идея для курсовой: расписание как граф'}}]
        sheet.locator('#capture-text').fill('Идея для курсовой: расписание как граф')
        expect(sheet.locator('.note-card')).to_be_visible()
        expect(sheet.locator('[data-create]')).to_be_enabled()
        self.screenshot(page, 'hardening-ai-note.png')
        self.assertEqual(self.errors, [])

    def test_recovery_reload_create_and_exactly_once(self):
        page = self.page()
        phrase = 'Завтра в 18:00 созвон с Ариадной\nна полчаса\nнапомни за 50 минут'
        sheet = self.capture(page, phrase)
        sheet.locator('[data-fact="title"]').click()
        editor = page.locator('dialog[data-inline-editor]')
        title = editor.locator('[data-inline-title]')
        title.fill('Ручное название')
        editor.locator('[data-inline-save]').click()
        editor.wait_for(state='detached')
        page.keyboard.press('Escape')
        page.reload()
        page.locator('#workspace[data-view-state="ready"]').wait_for()
        page.locator('.fab').click()
        sheet = page.locator('dialog.sheet[open]')
        expect(sheet.locator('#capture-text')).to_have_value(phrase)
        expect(sheet.locator('.capture-title')).to_contain_text('Ручное название')
        self.screenshot(page, 'hardening-restored-draft.png')
        sheet.locator('#capture-text').fill(phrase + '. Нет, не завтра, а в пятницу')
        expect(sheet.locator('.capture-title')).to_contain_text('Ручное название')
        expect(sheet.locator('[data-event-when]')).to_contain_text('18:00–18:30')
        self.screenshot(page, 'hardening-conversation.png')
        sheet.locator('[data-create]').evaluate('(button) => { button.click(); button.click(); }')
        sheet.wait_for(state='detached')
        page.locator('.fab').click()
        expect(page.locator('#capture-text')).to_have_value('')
        self.assertEqual(page.evaluate("Object.entries(localStorage).filter(([key]) => key.startsWith('seos.capture-draft.')).length"), 0)
        self.assertEqual(self.errors, [])

    def test_tutorial_close_does_not_complete_and_real_create_does(self):
        page = self.page(locale='en')
        page.goto(self.origin + '/#/settings')
        page.locator('[data-action="tutorial-open"]').click()
        sheet = page.locator('dialog.sheet[open]')
        expect(sheet.locator('#capture-text')).to_be_visible()
        self.screenshot(page, 'hardening-first-guided-capture.png')
        page.keyboard.press('Escape')
        self.assertEqual(page.evaluate("Object.keys(localStorage).filter(key => key.startsWith('seos.tutorial.capture-v1:')).length"), 0)
        page.locator('[data-action="tutorial-open"]').click()
        sheet = page.locator('dialog.sheet[open]')
        sheet.locator('#capture-text').fill('Buy milk')
        sheet.locator('.capture-card').wait_for()
        sheet.locator('[data-create]').click()
        sheet.wait_for(state='detached')
        page.wait_for_function("() => Object.entries(localStorage).some(([key, value]) => key.startsWith('seos.tutorial.capture-v1:') && value === 'done')")
        self.assertEqual(self.errors, [])

    def test_quick_intents_degraded_ai_keyboard_and_transient_source(self):
        page = self.page(320, 700)
        page.route(self.origin + '/api/v1/ask/capabilities', lambda route: route.fulfill(json={'live_llm_provider': True}))
        page.route(self.origin + '/api/v1/assistant/interpret', lambda route: route.fulfill(status=503, json={'detail': 'Temporarily unavailable'}))
        for index, phrase in enumerate(('Купить молоко', 'Завтра в 18:00 созвон с Ариадной на полчаса',
                                       'Завтра в шесть созвон с Ариадной на полчаса, напомни за 50 минут',
                                       'Напомни через 50 минут выключить духовку')):
            sheet = self.capture(page, phrase)
            expect(sheet.locator('[data-create]')).to_be_enabled()
            page.wait_for_timeout(400)
            expect(sheet.locator('[data-create]')).to_be_enabled()
            self.screenshot(page, f'hardening-quick-intent-{index}-ai-degraded.png')
            page.keyboard.press('Tab')
            self.assertTrue(sheet.evaluate('(element) => element.contains(document.activeElement)'))
            sheet.locator('[data-create]').click()
            sheet.wait_for(state='detached')
            print(json.dumps({'intent': phrase, 'required_actions': ['open', 'express', 'verify', 'create'], 'required_extra_fields': 0}))
        today = self.http.get('/api/v1/today', headers={'Authorization': 'Bearer ' + self.auth['token']}).json()
        today['source_health'] = [{'health_status': 'STALE', 'latest_failure_reason': 'HTTP_503',
                                   'last_successful_sync_at': '2026-09-28T08:20:00+03:00'}]
        page.route(self.origin + '/api/v1/today', lambda route: route.fulfill(json=today))
        page.reload()
        page.locator('.now-card').first.wait_for()
        source = page.locator('[data-source-status]')
        source.locator('summary').click()
        expect(source).to_contain_text('Используем последние данные')
        self.assertEqual(source.locator('[data-nav="settings"]').count(), 0)
        self.screenshot(page, 'hardening-transient-source-320.png')
        self.assertEqual(self.errors, [])

    def test_inline_time_and_duration_correction_without_advanced_form(self):
        page = self.page(360, 800)
        sheet = self.capture(page, 'Созвон завтра в 18:00 на полчаса')
        sheet.locator('[data-fact="event-time"]').click()
        editor = page.locator('dialog[data-inline-editor]')
        start = editor.locator('[data-inline-start]').input_value()
        editor.locator('[data-inline-start]').fill(start[:11] + '19:30')
        editor.locator('[data-inline-duration]').fill('45')
        self.screenshot(page, 'hardening-inline-time-360.png')
        editor.locator('[data-inline-save]').click()
        editor.wait_for(state='detached')
        expect(sheet.locator('[data-event-when]')).to_contain_text('19:30–20:15')
        self.assertFalse(sheet.locator('[data-more]').evaluate('(element) => element.open'))
        self.assertTrue(sheet.evaluate('(element) => element.contains(document.activeElement)'))
        self.assertEqual(self.errors, [])

    def test_visual_matrix_and_compact_event_editor(self):
        for width, height in ((320, 700), (360, 800), (390, 844), (412, 915), (768, 1024), (1280, 800)):
            for locale in ('ru', 'en'):
                for theme in ('light', 'dark'):
                    with self.subTest(width=width, locale=locale, theme=theme):
                        page = self.page(width, height, locale, theme)
                        self.screenshot(page, f'hardening-today-{width}-{locale}-{theme}.png')
                        page.goto(self.origin + '/#/plan')
                        page.locator('#workspace[data-view="plan"][data-view-state="ready"]').wait_for()
                        self.screenshot(page, f'hardening-plan-{width}-{locale}-{theme}.png')
                        text = 'Созвон завтра в 18:00\nна полчаса\nнапомни за 50 минут' if locale == 'ru' else 'Meeting tomorrow at 18:00\nfor half an hour\nremind me 50 minutes before'
                        sheet = self.capture(page, text)
                        expect(sheet.locator('.event-card')).to_be_visible()
                        self.assertFalse(sheet.locator('[data-reminder-editor]').evaluate('(element) => element.open'))
                        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                        self.assertLessEqual(sheet.evaluate('(element) => element.scrollWidth'), sheet.evaluate('(element) => element.clientWidth'))
                        self.screenshot(page, f'hardening-event-{width}-{locale}-{theme}.png')
                        self.assertEqual(self.errors, [])
                        page.context.close()
