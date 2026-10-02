from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.agent.assistant import validate_proposal
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import ValidationError
from student_execution_os.persistence import SQLiteCanonicalRepository


class AssistantNoteTest(unittest.TestCase):
    def test_note_is_a_validated_first_class_creation_with_idempotent_apply(self):
        content = 'Идея для курсовой: расписание как граф\nСохранить порядок строк'
        proposal = {'command': 'CREATE_NOTE', 'payload': {'content': content}, 'confidence': 0.95,
                    'unresolved_fields': [], 'expected_version': None, 'requires_confirmation': False}

        class Provider:
            name = 'note-fixture'

            def interpret(self, text, context):
                return [proposal]

        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / 'notes.sqlite')
            with SQLiteCanonicalRepository(database, clock=FrozenClock(datetime(2026, 9, 30, tzinfo=timezone.utc))) as repository:
                repository.initialize()
                repository.create_account('account')
                validate_proposal(proposal, repository, 'account')
                for payload in ({'content': ''}, {'content': 3}, {'content': content, 'remind_at': '2026-10-01T10:00:00Z'}):
                    with self.assertRaises(ValidationError):
                        validate_proposal({**proposal, 'payload': payload}, repository, 'account')
                assistant = SQLiteAssistantService(repository, AuthenticatedPrincipal('account', 'user', 'test-client'), provider=Provider())
                preview = assistant.interpret(content)
                action_id = preview['actions'][0]['id']
                corrected = content.replace('граф', 'ориентированный граф')
                request = {'batch_id': preview['batch_id'], 'action_ids': [action_id], 'idempotency_key': 'note-test',
                           'edits': {action_id: {'content': corrected}}}
                result = assistant.apply(request)
                self.assertTrue(assistant.apply(request)['replayed'])
                row = repository.connection.execute('SELECT content FROM notes WHERE account_id=?', ('account',)).fetchall()
                self.assertEqual([record['content'] for record in row], [corrected])
                correction_metrics = repository.connection.execute(
                    "SELECT dimensions_json FROM operational_metrics "
                    "WHERE account_id=? AND metric_name='assistant_user_correction_count'", ('account',),
                ).fetchall()
                self.assertEqual(len(correction_metrics), 1)
                self.assertNotIn(corrected, correction_metrics[0]['dimensions_json'])
