import sqlite3
import tempfile
import unittest
from pathlib import Path

from radai_agent.config import AppConfig
from radai_agent.db import connect, migrate
from radai_agent.service import AgentService
from radai_agent.spotify import SpotifyDevice
from radai_agent.spotify_device import find_configured_device
from radai_agent.stream import discard_recording, keep_recording, recording_command


class FakeSpotify:
    def devices(self):
        return (
            SpotifyDevice("phone", "Phone", "Smartphone", True, False),
            SpotifyDevice("vps", "Radai VPS", "Computer", False, False),
        )


class FakeIcecast:
    def __init__(self, reachable=True):
        self.reachable = reachable

    def check_stream(self):
        class Status:
            reachable = self.reachable
            error = None if self.reachable else "connection refused"

        return Status()


class IntegrationSurfaceTests(unittest.TestCase):
    def test_spotify_device_selection_targets_vps_name(self) -> None:
        selection = find_configured_device(FakeSpotify(), "Radai VPS")

        self.assertEqual(selection.device.id, "vps")

    def test_recording_keep_and_discard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recording.mp3"
            path.write_bytes(b"audio")
            kept = keep_recording(path)
            self.assertTrue(kept.path.exists())
            discarded = discard_recording(path)
            self.assertFalse(discarded.path.exists())

    def test_recording_command_copies_stream(self) -> None:
        command = recording_command("http://localhost:8000/radio.mp3", Path("out.mp3"))

        self.assertEqual(command[:5], ("ffmpeg", "-hide_banner", "-y", "-i", "http://localhost:8000/radio.mp3"))
        self.assertIn("-c", command)
        self.assertIn("copy", command)

    def test_service_startstream_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig.from_env(
                {
                    "RADAI_DB_PATH": str(Path(tmp) / "db.sqlite3"),
                    "RADAI_RECORDINGS_DIR": str(Path(tmp) / "recordings"),
                    "ICECAST_URL": "http://localhost:8000/radio.mp3",
                }
            )
            con = connect(config.db_path)
            migrate(con)
            service = AgentService(config, con, FakeIcecast(True))
            reply = service.start_stream()

            self.assertIn("Stream started", reply)
            self.assertIn("VLC URL: http://localhost:8000/radio.mp3", reply)
            row = con.execute("SELECT status, recording_status FROM stream_sessions").fetchone()
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["recording_status"], "recording")

    def test_service_sync_fetches_feed_and_records_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed = root / "feed.xml"
            feed.write_text(
                """<rss><channel><title>Local</title><item><guid>g1</guid><title>Episode</title><enclosure url="https://cdn/e.mp3" type="audio/mpeg" /></item></channel></rss>""",
                encoding="utf-8",
            )
            config = AppConfig.from_env({"RADAI_DB_PATH": str(root / "db.sqlite3")})
            con = connect(config.db_path)
            migrate(con)
            service = AgentService(config, con, FakeIcecast(True))
            reply = service.sync((feed.as_uri(),))

            self.assertIn("1 new episode", reply)
            row = con.execute("SELECT title, audio_url FROM podcast_episodes").fetchone()
            self.assertEqual(row["title"], "Episode")
            self.assertEqual(row["audio_url"], "https://cdn/e.mp3")


if __name__ == "__main__":
    unittest.main()
