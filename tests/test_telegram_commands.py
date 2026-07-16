import unittest

from radai_agent.telegram_bot import BotReply, TelegramCommand, TelegramError, build_router, parse_command, parse_ratio


class Actions:
    def __init__(self) -> None:
        self.calls = []

    def sync(self, feed_urls):
        self.calls.append(("sync", feed_urls))
        return "synced"

    def episodes(self):
        return "episodes"

    def playlists(self):
        return "playlists"

    def set_ratio(self, podcast_minutes, music_minutes):
        self.calls.append(("ratio", podcast_minutes, music_minutes))
        return "ratio set"

    def set_music_playlist(self, name):
        self.calls.append(("playlist", name))
        return "playlist set"

    def set_music_radio(self, seed):
        self.calls.append(("radio", seed))
        return "radio set"

    def play(self, query):
        self.calls.append(("play", query))
        return "Stream started.\nVLC URL: http://localhost:8000/radio.mp3"

    def start_stream(self):
        self.calls.append(("startstream",))
        return "Stream started.\nVLC URL: http://localhost:8000/radio.mp3"

    def stop_stream(self):
        return "stopped"

    def keep_recording(self):
        return "kept"

    def discard_recording(self):
        return "discarded"

    def status(self):
        return "status"


class TelegramCommandTests(unittest.TestCase):
    def test_parse_simple_startstream_text(self) -> None:
        command = parse_command("start stream")

        self.assertEqual(command.name, "startstream")

    def test_parse_ratio(self) -> None:
        self.assertEqual(parse_ratio(TelegramCommand("ratio", ("20:10",))), (20, 10))

    def test_bad_ratio_rejected(self) -> None:
        with self.assertRaises(TelegramError):
            parse_ratio(TelegramCommand("ratio", ("0:10",)))

    def test_router_music_playlist(self) -> None:
        actions = Actions()
        reply = build_router(actions).handle_text("/music playlist Driving Mix")

        self.assertEqual(reply, BotReply("playlist set"))
        self.assertEqual(actions.calls[-1], ("playlist", "Driving Mix"))

    def test_router_startstream_returns_confirmation(self) -> None:
        actions = Actions()
        reply = build_router(actions).handle_text("/startstream")

        self.assertIn("VLC URL", reply.text)
        self.assertEqual(actions.calls[-1], ("startstream",))

    def test_router_play_passes_filter(self) -> None:
        actions = Actions()
        reply = build_router(actions).handle_text("/play lex fridman")

        self.assertIn("VLC URL", reply.text)
        self.assertEqual(actions.calls[-1], ("play", "lex fridman"))


if __name__ == "__main__":
    unittest.main()
