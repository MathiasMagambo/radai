import io
import unittest
import urllib.error
from unittest.mock import patch

from radai_agent.deepseek import DeepSeekClient, DeepSeekError, EpisodePlanningInput, parse_deepseek_plan


class DeepSeekPlanTests(unittest.TestCase):
    def test_valid_plan_parses(self) -> None:
        plan = parse_deepseek_plan(
            {
                "episode_id": "ep1",
                "ad_cuts": [{"start_sec": 10, "end_sec": 20, "reason": "ad"}],
                "music_insertions": [{"after_sec": 1200, "window_sec": 600, "mood": "calm", "reason": "cadence"}],
                "confidence": 0.7,
                "warnings": [],
            },
            duration_sec=2000,
        )

        self.assertEqual(plan.episode_id, "ep1")
        self.assertEqual(plan.ad_cuts[0].reason, "ad")
        self.assertEqual(plan.music_insertions[0].window_sec, 600)

    def test_overlapping_cuts_are_rejected(self) -> None:
        with self.assertRaises(DeepSeekError):
            parse_deepseek_plan(
                {
                    "episode_id": "ep1",
                    "ad_cuts": [
                        {"start_sec": 10, "end_sec": 20, "reason": "ad"},
                        {"start_sec": 19, "end_sec": 30, "reason": "ad"},
                    ],
                    "music_insertions": [],
                    "confidence": 0.7,
                    "warnings": [],
                },
                duration_sec=2000,
            )

    def test_out_of_bounds_insertion_is_rejected(self) -> None:
        with self.assertRaises(DeepSeekError):
            parse_deepseek_plan(
                {
                    "episode_id": "ep1",
                    "ad_cuts": [],
                    "music_insertions": [{"after_sec": 5000, "window_sec": 600}],
                    "confidence": 0.7,
                    "warnings": [],
                },
                duration_sec=100,
            )

    def test_non_strict_plan_discards_invalid_suggestions(self) -> None:
        plan = parse_deepseek_plan(
            {
                "episode_id": "ep1",
                "ad_cuts": [
                    {"start_sec": 40, "end_sec": 20, "reason": "reversed"},
                    {"start_sec": 50, "end_sec": 60, "reason": "valid"},
                ],
                "music_insertions": [
                    {"after_sec": -1, "window_sec": 600},
                    {"after_sec": 1200, "window_sec": 600},
                ],
                "confidence": 0.7,
                "warnings": [],
            },
            validate=False,
        )

        self.assertEqual([(cut.start_sec, cut.end_sec) for cut in plan.ad_cuts], [(50, 60)])
        self.assertEqual([item.after_sec for item in plan.music_insertions], [1200])
        self.assertEqual(
            plan.warnings,
            ("Discarded an invalid ad cut", "Discarded an invalid music insertion"),
        )


    def test_credit_exhaustion_has_an_actionable_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            402,
            "Payment Required",
            None,
            io.BytesIO(b'{"error":{"message":"Insufficient Balance"}}'),
        )
        client = DeepSeekClient("test-key")

        with patch("radai_agent.deepseek.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(DeepSeekError, "credits are exhausted"):
                client.plan_episode(EpisodePlanningInput("ep", "title", "show", "", "text"))

    def test_unresponsive_api_has_an_actionable_error(self) -> None:
        client = DeepSeekClient("test-key")

        with patch(
            "radai_agent.deepseek.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timed out"),
        ):
            with self.assertRaisesRegex(DeepSeekError, "not responding"):
                client.plan_episode(EpisodePlanningInput("ep", "title", "show", "", "text"))



if __name__ == "__main__":
    unittest.main()
