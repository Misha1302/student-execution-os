from __future__ import annotations

import unittest

from student_execution_os.agent.evaluation import evaluate_semantic_corpus, load_semantic_corpus
from student_execution_os.agent.providers import ProviderUnavailable


class AssistantEvaluationTest(unittest.TestCase):
    def test_versioned_corpus_covers_required_semantic_classes(self):
        corpus = load_semantic_corpus()
        capabilities = {case["capability"] for case in corpus["cases"]}
        self.assertTrue({
            "creation", "field_extraction", "update", "relative_reschedule",
            "conditional_reschedule", "approximate_time", "target_ambiguity",
            "multi_turn", "multi_action", "voice_correction", "planner_constraint",
            "read_query", "destructive_action", "unsupported_intent", "undo",
        }.issubset(capabilities))
        self.assertGreaterEqual(len(corpus["cases"]), 20)
        self.assertGreaterEqual(sum(len(case["turns"]) for case in corpus["cases"]), 24)

    def test_evaluator_distinguishes_target_semantic_format_and_transport_failures(self):
        corpus = {"version": 1, "cases": [
            {"id": "correct", "capability": "creation", "turns": [{
                "text": "a", "expected": {"kind": "ACTION", "command": "CREATE_TASK", "payload_subset": {"title": "A"}},
            }]},
            {"id": "target", "capability": "update", "turns": [{
                "text": "b", "expected": {"kind": "ACTION", "command": "UPDATE_EVENT", "target": {"obligation_id": "allowed"}},
            }]},
            {"id": "semantic", "capability": "creation", "turns": [{
                "text": "c", "expected": {"kind": "ACTION", "command": "CREATE_NOTE"},
            }]},
            {"id": "format", "capability": "creation", "turns": [{
                "text": "d", "expected": {"kind": "ACTION", "command": "CREATE_TASK"},
            }]},
            {"id": "transport", "capability": "creation", "turns": [{
                "text": "e", "expected": {"kind": "ACTION", "command": "CREATE_TASK"},
            }]},
        ]}

        class Provider:
            name = "fixture"
            model = "fixture-v1"

            def interpret(self, text, context):
                if text == "d":
                    raise ProviderUnavailable("bad json", "FORMAT")
                if text == "e":
                    raise ProviderUnavailable("offline", "NETWORK")
                if text == "a":
                    return {"actions": [{"command": "CREATE_TASK", "payload": {"title": "A"}}]}
                if text == "b":
                    return {"actions": [{"command": "UPDATE_EVENT", "payload": {"obligation_id": "other"}}]}
                return {"actions": [{"command": "CREATE_TASK", "payload": {"title": "C"}}]}

        report = evaluate_semantic_corpus(Provider(), corpus)
        self.assertEqual(report["counts"], {
            "CORRECT": 1,
            "TARGET_RESOLUTION_MISMATCH": 1,
            "SEMANTIC_MISMATCH": 1,
            "FORMAT_FAILURE": 1,
            "TRANSPORT_FAILURE": 1,
        })
        self.assertNotIn("text", str(report))


if __name__ == "__main__":
    unittest.main()
