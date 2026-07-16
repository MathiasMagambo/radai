import unittest
from pathlib import Path

from radai_agent.audio import build_prepare_command
from radai_agent.models import CutRange


class AudioCommandTests(unittest.TestCase):
    def test_prepare_command_contains_cut_and_normalize_filters(self) -> None:
        command = build_prepare_command(
            Path("input.mp3"),
            Path("out/output.mp3"),
            cuts=(CutRange(10.0, 20.0, "ad"),),
            normalize=True,
        )

        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-af", command)
        filter_arg = command[command.index("-af") + 1]
        self.assertIn("aselect", filter_arg)
        self.assertIn("loudnorm", filter_arg)
        self.assertEqual(command[-1], "out/output.mp3")


if __name__ == "__main__":
    unittest.main()
