import json
import unittest

from radai_agent.transcripts import normalize_transcript_text


class TranscriptTests(unittest.TestCase):
    def test_normalize_vtt_removes_timestamps(self) -> None:
        raw = """WEBVTT

00:00:01.000 --> 00:00:03.000
<v Speaker>Hello there.</v>

00:00:04.000 --> 00:00:06.000
General Kenobi.
"""

        self.assertEqual(normalize_transcript_text(raw, "text/vtt"), "Hello there.\nGeneral Kenobi.\n")

    def test_normalize_json_collects_text_segments(self) -> None:
        raw = json.dumps({"segments": [{"start": 0, "text": "First"}, {"start": 2, "text": "Second"}]})

        self.assertEqual(normalize_transcript_text(raw, "application/json"), "First\nSecond\n")

    def test_normalize_html_extracts_visible_text(self) -> None:
        raw = "<html><body><nav>Skip</nav><p>Transcript line.</p><script>bad()</script></body></html>"

        self.assertEqual(normalize_transcript_text(raw, "text/html"), "Transcript line.\n")


if __name__ == "__main__":
    unittest.main()
