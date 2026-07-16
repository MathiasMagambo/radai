import unittest
from pathlib import Path

from radai_engine.models import WindowKind
from radai_engine.scheduler import PodcastSegment, ScheduleError, build_time_window_schedule


class SchedulerTests(unittest.TestCase):
    def test_twenty_ten_windows_for_long_episode(self) -> None:
        windows = build_time_window_schedule(
            [PodcastSegment(episode_id=1, media_path=Path("ep.mp3"), duration_sec=2700)],
            podcast_window_sec=1200,
            music_window_sec=600,
            source_id=9,
        )

        self.assertEqual([window.kind for window in windows], [WindowKind.PODCAST, WindowKind.MUSIC, WindowKind.PODCAST, WindowKind.MUSIC, WindowKind.PODCAST])
        self.assertEqual([window.duration_sec for window in windows], [1200, 600, 1200, 600, 300])
        self.assertEqual(windows[1].source_id, 9)

    def test_short_episode_does_not_add_trailing_music(self) -> None:
        windows = build_time_window_schedule(
            [PodcastSegment(episode_id=1, media_path=Path("ep.mp3"), duration_sec=300)],
            podcast_window_sec=1200,
            music_window_sec=600,
        )

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].duration_sec, 300)

    def test_music_between_two_episodes(self) -> None:
        windows = build_time_window_schedule(
            [PodcastSegment(1, Path("a.mp3"), 300), PodcastSegment(2, Path("b.mp3"), 300)],
            podcast_window_sec=1200,
            music_window_sec=600,
        )

        self.assertEqual([window.kind for window in windows], [WindowKind.PODCAST, WindowKind.MUSIC, WindowKind.PODCAST])

    def test_invalid_ratio_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError):
            build_time_window_schedule([], podcast_window_sec=0, music_window_sec=600)


if __name__ == "__main__":
    unittest.main()
