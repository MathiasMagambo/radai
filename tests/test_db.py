import tempfile
import unittest
from pathlib import Path

from radai_engine.db import connect, list_new_episodes, migrate, upsert_episode, upsert_feed
from radai_engine.models import PodcastEpisode


class DatabaseTests(unittest.TestCase):
    def test_episode_upsert_dedupes_by_feed_and_guid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "db.sqlite3")
            migrate(con)
            feed_id = upsert_feed(con, "https://example.com/feed.xml", "Example")
            episode = PodcastEpisode("https://example.com/feed.xml", "guid-1", "First", "https://cdn/e1.mp3")
            first_id = upsert_episode(con, feed_id, episode)
            second_id = upsert_episode(con, feed_id, PodcastEpisode("https://example.com/feed.xml", "guid-1", "Renamed", "https://cdn/e1.mp3"))

            self.assertEqual(first_id, second_id)
            rows = list_new_episodes(con)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "Renamed")


if __name__ == "__main__":
    unittest.main()
