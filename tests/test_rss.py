import unittest

from radai_engine.rss import parse_feed_xml


SAMPLE = b'''<?xml version="1.0"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Example Show</title>
    <item>
      <guid>episode-1</guid>
      <title>Episode One</title>
      <pubDate>Tue, 28 May 2024 12:00:00 GMT</pubDate>
      <link>https://example.com/episode-one</link>
      <description><![CDATA[Read the <a href="/episode-one-transcript">transcript</a>.]]></description>
      <enclosure url="https://cdn.example.com/e1.mp3" length="1234" type="audio/mpeg" />
      <podcast:transcript url="https://example.com/e1.vtt" type="text/vtt" language="en" />
    </item>
    <item>
      <guid>no-audio</guid>
      <title>No Audio</title>
    </item>
  </channel>
</rss>
'''


class RssParsingTests(unittest.TestCase):
    def test_parse_feed_extracts_audio_and_transcripts(self) -> None:
        parsed = parse_feed_xml(SAMPLE, "https://example.com/feed.xml")

        self.assertEqual(parsed.title, "Example Show")
        self.assertEqual(len(parsed.episodes), 1)
        episode = parsed.episodes[0]
        self.assertEqual(episode.guid, "episode-1")
        self.assertEqual(episode.audio_url, "https://cdn.example.com/e1.mp3")
        self.assertEqual(episode.audio_bytes, 1234)
        self.assertEqual({ref.source for ref in episode.transcripts}, {"podcast:transcript", "description-link"})
        self.assertEqual(episode.transcripts[0].url, "https://example.com/e1.vtt")
        self.assertEqual(episode.transcripts[1].url, "https://example.com/episode-one-transcript")

    def test_guid_falls_back_to_enclosure_url(self) -> None:
        xml = b'''<rss><channel><title>X</title><item><title>T</title><enclosure url="https://a/b.mp3" type="audio/mpeg" /></item></channel></rss>'''
        parsed = parse_feed_xml(xml, "https://example.com/feed.xml")

        self.assertEqual(parsed.episodes[0].stable_key, "https://a/b.mp3")


if __name__ == "__main__":
    unittest.main()
