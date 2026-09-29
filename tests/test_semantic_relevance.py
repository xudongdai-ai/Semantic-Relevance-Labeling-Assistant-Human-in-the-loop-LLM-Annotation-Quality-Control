import unittest

from semantic_relevance_assistant import (
    ValidationError,
    build_plan,
    load_routes,
    normalized_payload,
    parse_grade,
    parse_source_grades,
)


def make_item(item_id, email, status, candidate_grades):
    return {
        "item": {"id": item_id, "item_key": f"sample-{item_id}"},
        "song": {"title": f"虚构歌曲 {item_id}"},
        "assignments": [
            {"id": item_id + 100, "annotator_email": email, "status": status}
        ],
        "semantic_context": {
            "pairs": [
                {
                    "pair_id": f"pair-{item_id}",
                    "description": "合成测试 Query",
                    "llm_grade_final": 2,
                    "llm_grades": candidate_grades,
                }
            ]
        },
    }


class SemanticRelevanceTests(unittest.TestCase):
    def test_grade_parser_accepts_only_zero_through_three(self):
        self.assertEqual(parse_grade("3", "pair-1"), 3)
        with self.assertRaises(ValidationError):
            parse_grade(4, "pair-1")

    def test_source_grade_parser_reads_json_text_and_discards_invalid_values(self):
        self.assertEqual(parse_source_grades('[2, "1", 9, true, "bad"]'), [2, 1])

    def test_plan_splits_ready_review_and_ignores_other_or_submitted_assignments(self):
        data = {
            "project": {"id": 123, "task_type": "semantic_relevance"},
            "items": [
                make_item(1, "you@example.com", "assigned", [2, 2, 1]),
                make_item(2, "you@example.com", "assigned", [0, 0, 2]),
                make_item(3, "other@example.com", "assigned", [2, 2]),
                make_item(4, "you@example.com", "submitted", [2, 2]),
            ],
        }

        ready, review = build_plan(
            export_data=data,
            email="you@example.com",
            project_id=123,
            selected_item_ids=set(),
            min_agreement=0.6,
        )

        self.assertEqual([item.item_id for item in ready], [1])
        self.assertEqual([item.item_id for item in review], [2])

    def test_normalized_payload_ignores_missing_optional_text(self):
        left = {"answers": [{"pair_id": "p1", "relevance_grade": 2}]}
        right = {
            "answers": [
                {
                    "pair_id": "p1",
                    "relevance_grade": 2,
                    "unable_to_judge": False,
                    "reason": "",
                }
            ],
            "note": "",
        }

        self.assertEqual(normalized_payload(left), normalized_payload(right))

    def test_route_config_rejects_unfilled_template(self):
        import json
        import tempfile
        from pathlib import Path

        empty_routes = {
            "login": "",
            "session": "",
            "project": "",
            "project_export": "",
            "item_detail": "",
            "submit_item": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "routes.json"
            config.write_text(json.dumps(empty_routes), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_routes(config)


if __name__ == "__main__":
    unittest.main()
