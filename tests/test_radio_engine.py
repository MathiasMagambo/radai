from collections import deque
from io import BytesIO
import signal
import threading
from pathlib import Path
from types import SimpleNamespace

from radai_agent.deepseek import DeepSeekError
from radai_agent.models import CutRange, MusicInsertion
from radai_agent.radio_engine import RadioEngine, RadioSettings, RadioStatus, StateStore, _clean_time, _dedupe_insertions, _merge_cuts, _transcript_chunks
from radai_agent.spotify_desktop import SpotifyDesktopController
from radai_agent.web import BufferedAudioStream, _mp3_frame_start, _render_html_template


def test_transcript_chunks_preserve_every_line() -> None:
    transcript = "\n".join(f"[{index:02d}:00] line {index}" for index in range(20))

    chunks = _transcript_chunks(transcript, 55)

    assert "\n".join(chunks) == transcript
    assert all(len(chunk) <= 55 for chunk in chunks)


def test_overlapping_ad_cuts_merge_and_shift_insertion_time() -> None:
    cuts = _merge_cuts(
        [
            CutRange(600, 660, "sponsor"),
            CutRange(650, 700, "promo"),
            CutRange(1200, 1230, "sponsor"),
        ]
    )

    assert [(cut.start_sec, cut.end_sec) for cut in cuts] == [(600, 700), (1200, 1230)]
    assert _clean_time(1300, cuts) == 1170


def test_music_breaks_remain_at_least_ten_minutes_apart() -> None:
    insertions = [
        MusicInsertion(600, 600, "calm", "chapter"),
        MusicInsertion(900, 600, "upbeat", "too close"),
        MusicInsertion(1200, 600, "focused", "next chapter"),
    ]

    result = _dedupe_insertions(insertions)

    assert [item.after_sec for item in result] == [600, 1200]


def test_pcm_source_transitions_only_write_complete_stereo_frames() -> None:
    engine = object.__new__(RadioEngine)
    writes: list[bytes] = []
    engine._write_pcm = writes.append  # type: ignore[method-assign]

    remainder = engine._write_complete_frames(b"\x01\x02\x03")
    remainder = engine._write_complete_frames(remainder + b"\x04\x05\x06\x07\x08\x09")

    assert b"".join(writes) == b"\x01\x02\x03\x04\x05\x06\x07\x08"
    assert all(len(chunk) % 4 == 0 for chunk in writes)
    assert remainder == b"\x09"


def test_delayed_pause_suspends_decoder_and_starts_silent_keepalive() -> None:
    signals: list[int] = []
    decoder = SimpleNamespace(
        poll=lambda: None,
        send_signal=signals.append,
    )
    engine = object.__new__(RadioEngine)
    engine._lock = threading.RLock()
    engine._pause_generation = 1
    engine._playback_paused = threading.Event()
    engine._playback_paused.set()
    engine._status = RadioStatus(state="paused", mode="podcast")
    engine._active_decoder = decoder
    engine._source_paused = False
    engine._pcm_source_active = threading.Event()
    engine._pcm_source_active.set()

    engine._pause_source_after_delay(1, 0)

    assert signals == [signal.SIGSTOP]
    assert engine.source_paused()
    assert not engine._pcm_source_active.is_set()


def test_resume_continues_decoder_and_reactivates_pcm_source() -> None:
    signals: list[int] = []
    decoder = SimpleNamespace(
        poll=lambda: None,
        send_signal=signals.append,
    )
    engine = object.__new__(RadioEngine)
    engine._lock = threading.RLock()
    engine._pause_generation = 1
    engine._playback_paused = threading.Event()
    engine._playback_paused.set()
    engine._source_paused = True
    engine._status = RadioStatus(state="paused", mode="podcast")
    engine._active_decoder = decoder
    engine._pcm_source_active = threading.Event()
    engine._thread = threading.current_thread()
    engine.store = SimpleNamespace(settings=SimpleNamespace(songs_per_break=3))

    status = engine.resume()

    assert signals == [signal.SIGCONT]
    assert engine._pcm_source_active.is_set()
    assert not engine.source_paused()
    assert not engine._playback_paused.is_set()
    assert status.state == "running"


def test_spotify_drain_resets_pcm_phase_at_track_boundary(monkeypatch, tmp_path: Path) -> None:
    engine = object.__new__(RadioEngine)
    engine._stop = threading.Event()
    engine._spotify_audio_enabled = threading.Event()
    engine._spotify_audio_enabled.set()
    engine._spotify_audio_ready = threading.Event()
    engine._pcm_source_active = threading.Event()
    engine.spotifyd_audio_pipe = tmp_path / "spotify.pcm"
    writes: list[bytes] = []
    engine._write_pcm = writes.append  # type: ignore[method-assign]
    reads = iter(
        (
            b"\x01\x02\x03",
            b"",
            b"\x04\x05\x06\x07\x08\x09\x0a\x0b",
            b"",
        )
    )
    empty_reads = 0

    def read_chunk(_file_descriptor: int, _size: int) -> bytes:
        nonlocal empty_reads
        chunk = next(reads)
        if not chunk:
            empty_reads += 1
            if empty_reads == 2:
                engine._stop.set()
        return chunk

    monkeypatch.setattr("radai_agent.radio_engine.os.open", lambda *_args: 1)
    monkeypatch.setattr("radai_agent.radio_engine.os.read", read_chunk)
    monkeypatch.setattr("radai_agent.radio_engine.os.close", lambda _descriptor: None)
    monkeypatch.setattr("radai_agent.radio_engine.time.sleep", lambda _seconds: None)

    engine._drain_spotifyd_audio()

    assert writes == [b"\x04\x05\x06\x07\x08\x09\x0a\x0b"]
    assert engine._spotify_audio_ready.is_set()

def test_music_break_accepts_spotify_pcm_before_starting_playback() -> None:
    engine = object.__new__(RadioEngine)
    engine._stop = threading.Event()
    engine._spotify_audio_enabled = threading.Event()
    engine._spotify_audio_ready = threading.Event()
    engine._pcm_source_active = threading.Event()
    engine._playback_paused = threading.Event()
    engine._lock = threading.RLock()
    engine._status = RadioStatus()
    engine._spotifyd = SimpleNamespace(poll=lambda: None)
    engine.spotify_device_name = "Radai Radio"
    engine.store = SimpleNamespace(
        settings=SimpleNamespace(
            songs_per_break=3,
            seed_track_uri="spotify:track:test",
            seed_track_name="Test track",
            active_music_source_uri=None,
            active_music_source_name=None,
        ),
        save=lambda: None,
    )

    def play_track_radio(*_args, **_kwargs) -> None:
        assert engine._spotify_audio_enabled.is_set()
        engine._spotify_audio_ready.set()
        engine._stop.set()

    engine.spotify_desktop = SimpleNamespace(play_track_radio=play_track_radio)
    engine._wait_for_device = lambda: None  # type: ignore[method-assign]
    engine._pause_spotify = lambda: None  # type: ignore[method-assign]

    engine._play_music_break()

    assert engine._status.mode == "music"


def test_song_radio_opens_track_menu_and_starts_generated_playlist(monkeypatch) -> None:
    controller = object.__new__(SpotifyDesktopController)
    played: list[tuple[object, ...]] = []
    activated: list[str] = []
    browser_results = iter((True, "radio", True))
    browser_steps: list[str] = []
    controller.play_track = (  # type: ignore[method-assign]
        lambda *args, **kwargs: played.append((*args, kwargs))
    )
    controller._resume_selected_song_radio = lambda *_args: False  # type: ignore[method-assign]
    controller.activate_device = activated.append  # type: ignore[method-assign]
    controller.current_playback = (  # type: ignore[method-assign]
        lambda: SimpleNamespace(is_playing=True)
    )
    monkeypatch.setattr("radai_agent.spotify_desktop.time.sleep", lambda _seconds: None)

    def evaluate(expression: str) -> object:
        if "closest('[role=\"row\"]')" in expression:
            browser_steps.append("open-options")
        elif "Go to song radio" in expression:
            browser_steps.append("open-radio")
        elif "label.includes('Radio')" in expression:
            browser_steps.append("play-radio")
        return next(browser_results)

    controller._evaluate = evaluate  # type: ignore[method-assign]

    controller.play_track_radio(
        "Radai Radio",
        "spotify:track:test",
        search_query="Test track",
    )

    assert played == [
        ("Radai Radio", "spotify:track:test", {"search_query": "Test track"})
    ]
    assert browser_steps == ["open-options", "open-radio", "play-radio"]
    assert activated == ["Radai Radio"]

def test_selected_song_radio_keeps_playing_without_restart() -> None:
    controller = object.__new__(SpotifyDesktopController)
    restarted: list[str] = []
    controller._evaluate = lambda _expression: {  # type: ignore[method-assign]
        "selected": True,
        "playing": True,
        "device_active": True,
    }
    controller.play_track = lambda *_args, **_kwargs: restarted.append("restart")  # type: ignore[method-assign]

    controller.play_track_radio(
        "Radai Radio",
        "spotify:track:test",
        search_query="Pretty Girls — Odeal",
    )

    assert restarted == []

def test_selected_playing_song_radio_switches_back_and_resumes(monkeypatch) -> None:
    controller = object.__new__(SpotifyDesktopController)
    restarted: list[str] = []
    activated: list[str] = []
    resumed: list[str] = []
    playback = iter((False, True))
    controller._evaluate = lambda _expression: {  # type: ignore[method-assign]
        "selected": True,
        "playing": True,
        "device_active": False,
    }
    controller.play_track = lambda *_args, **_kwargs: restarted.append("restart")  # type: ignore[method-assign]
    controller.activate_device = activated.append  # type: ignore[method-assign]
    controller.resume = lambda: resumed.append("resume")  # type: ignore[method-assign]
    controller.current_playback = (  # type: ignore[method-assign]
        lambda: SimpleNamespace(is_playing=next(playback))
    )
    monkeypatch.setattr("radai_agent.spotify_desktop.time.sleep", lambda _seconds: None)

    controller.play_track_radio(
        "Radai Radio",
        "spotify:track:test",
        search_query="Pretty Girls — Odeal",
    )

    assert restarted == []
    assert activated == ["Radai Radio"]
    assert resumed == ["resume"]


def test_selected_paused_song_radio_resumes_without_restart(monkeypatch) -> None:
    controller = object.__new__(SpotifyDesktopController)
    restarted: list[str] = []
    activated: list[str] = []
    resumed: list[str] = []
    playback = iter((False, True))
    controller._evaluate = lambda _expression: {  # type: ignore[method-assign]
        "selected": True,
        "playing": False,
        "device_active": False,
    }
    controller.play_track = lambda *_args, **_kwargs: restarted.append("restart")  # type: ignore[method-assign]
    controller.activate_device = activated.append  # type: ignore[method-assign]
    controller.resume = lambda: resumed.append("resume")  # type: ignore[method-assign]
    controller.current_playback = (  # type: ignore[method-assign]
        lambda: SimpleNamespace(is_playing=next(playback))
    )
    monkeypatch.setattr("radai_agent.spotify_desktop.time.sleep", lambda _seconds: None)

    controller.play_track_radio(
        "Radai Radio",
        "spotify:track:test",
        search_query="Pretty Girls — Odeal",
    )

    assert restarted == []
    assert activated == ["Radai Radio"]
    assert resumed == ["resume"]

def test_listener_stream_starts_on_a_complete_mp3_frame() -> None:
    frame = b"\xff\xfb\x90\x00" + bytes(413)
    stream = b"partial frame data" + frame + frame

    offset = _mp3_frame_start(stream, 0)

    assert offset == len(b"partial frame data")
    assert stream[offset : offset + 4] == b"\xff\xfb\x90\x00"


def test_pause_completes_partial_mp3_frame_before_holding_stream() -> None:
    frame = b"\xff\xfb\x90\x00" + bytes(413)
    stream = object.__new__(BufferedAudioStream)
    stream._data = bytearray(frame + frame + frame[:100])

    assert stream._mp3_frame_completion_bytes_locked() == len(frame) - 100


def test_playback_status_follows_the_lagged_audio_cursor() -> None:
    podcast = RadioStatus(state="running", mode="podcast", now_playing="Podcast")
    music = RadioStatus(state="running", mode="music", now_playing="Song")
    stream = object.__new__(BufferedAudioStream)
    stream.status_source = lambda: music
    stream.lag_bytes = 100
    stream._condition = threading.Condition()
    stream._data = bytearray(200)
    stream._base = 0
    stream._status_history = deque(((0, podcast), (150, music)))

    assert stream.playback_status().now_playing == "Podcast"

    stream._data.extend(bytes(100))

    assert stream.playback_status().now_playing == "Song"



def test_playlist_and_song_radio_swap_the_active_music_source() -> None:
    calls: list[tuple[object, ...]] = []
    playlist = SimpleNamespace(uri="spotify:playlist:new", name="New playlist")
    spotify = SimpleNamespace(
        playlists=lambda: (playlist,),
        play_context=lambda *args, **kwargs: calls.append(("playlist", *args, kwargs)),
        play_track_radio=lambda *args, **kwargs: calls.append(("track-radio", *args, kwargs)),
    )
    settings = RadioSettings()
    store = SimpleNamespace(settings=settings, save=lambda: calls.append(("save",)))
    engine = object.__new__(RadioEngine)
    engine._lock = threading.RLock()
    engine._status = RadioStatus(state="running", mode="music")
    engine.spotify_desktop = spotify
    engine.spotify_device_name = "radio"
    engine.store = store

    engine.set_playlist(playlist.uri, playlist.name)
    engine.set_radio_track("spotify:track:new", "New song")

    assert calls[1] == ("playlist", "radio", playlist.uri, {"shuffle": True})
    assert calls[4] == (
        "track-radio",
        "radio",
        "spotify:track:new",
        {"search_query": "New song"},
    )
    assert settings.active_music_source_uri == "spotify:track:new"
    assert settings.active_music_source_name == "New song"


def test_radio_selects_prepared_episode_before_opening_stream() -> None:
    engine = object.__new__(RadioEngine)
    engine._lock = threading.RLock()
    engine._current_episode_id = None
    engine._stop = threading.Event()
    engine._spotify_audio_enabled = threading.Event()
    engine._active_decoder = None
    engine._spotifyd = None
    engine._encoder = None
    events: list[str] = []
    engine._choose_prepared_episode = lambda: events.append("choose") or (None, None, None)  # type: ignore[method-assign,return-value]
    engine._start_pipeline = lambda: (events.append("stream"), engine._stop.set())  # type: ignore[method-assign]
    engine._pause_spotify = lambda: None  # type: ignore[method-assign]
    engine._terminate = lambda process: None  # type: ignore[method-assign]

    engine._run()

    assert events == ["choose", "stream"]


def test_prepared_episode_is_consumed_only_after_playback_finishes() -> None:
    engine = object.__new__(RadioEngine)
    engine._lock = threading.RLock()
    engine._current_episode_id = None
    engine._stop = threading.Event()
    engine._spotify_audio_enabled = threading.Event()
    engine._active_decoder = None
    engine._spotifyd = None
    engine._encoder = None
    episode = SimpleNamespace(episode=SimpleNamespace(id="prepared-episode"))
    events: list[str] = []

    def choose_episode() -> tuple[object, None, None]:
        events.append("choose")
        if events.count("choose") == 2:
            engine._stop.set()
        return episode, None, None

    engine._choose_prepared_episode = choose_episode  # type: ignore[method-assign]
    engine._start_pipeline = lambda: events.append("stream")  # type: ignore[method-assign]
    engine._play_episode = lambda *_args: events.append("play") or True  # type: ignore[method-assign]
    engine._consume_queued_episode = lambda _id: events.append("consume")  # type: ignore[method-assign]
    engine._mark_played = lambda _id: events.append("mark")  # type: ignore[method-assign]
    engine.prepare_in_background = lambda: events.append("prepare")  # type: ignore[method-assign]
    engine._pause_spotify = lambda: None  # type: ignore[method-assign]
    engine._terminate = lambda _process: None  # type: ignore[method-assign]

    engine._run()

    assert events == ["choose", "stream", "play", "consume", "mark", "prepare", "choose"]


def test_restart_podcast_is_disabled_by_default() -> None:
    assert RadioSettings().restart_current_podcast_enabled is False


def test_prepared_video_waits_for_queue_or_play_now_choice() -> None:
    settings = RadioSettings(
        pending_video_id="video-1",
        pending_video_title="Prepared video",
        played_episode_ids=["video-1"],
    )
    saved: list[bool] = []
    engine = object.__new__(RadioEngine)
    engine.store = SimpleNamespace(settings=settings, save=lambda: saved.append(True))
    engine._thread = SimpleNamespace(is_alive=lambda: True)
    engine._lock = threading.RLock()
    engine._playback_paused = threading.Event()
    engine._play_now_requested = threading.Event()
    engine._pause_generation = 0
    engine._status = RadioStatus(state="running", mode="podcast")
    engine._active_decoder = object()
    interrupted: list[object] = []
    engine._terminate = interrupted.append  # type: ignore[method-assign]
    engine._pause_spotify = lambda: interrupted.append("spotify")  # type: ignore[method-assign]

    engine.resolve_prepared_video("play_now")

    assert settings.queued_video_id == "video-1"
    assert settings.pending_video_id is None
    assert settings.played_episode_ids == []
    assert engine._play_now_requested.is_set()
    assert interrupted == [engine._active_decoder, "spotify"]
    assert saved == [True]


def test_llm_preparation_failure_is_exposed_in_radio_status() -> None:
    engine = object.__new__(RadioEngine)
    engine._lock = threading.RLock()
    engine._status = RadioStatus()
    engine.store = SimpleNamespace(settings=SimpleNamespace(channels=["channel-a"]))

    def fail_preparation(_channel: str) -> None:
        raise DeepSeekError("DeepSeek API credits are exhausted")

    engine._prepare_channel = fail_preparation  # type: ignore[method-assign]

    engine._prepare_missing_channels()

    assert engine.status().preparation_error == (
        "Podcast preparation failed: LLM API error: DeepSeek API credits are exhausted"
    )


def test_podcast_checkpoint_tracks_pcm_written_to_stream(monkeypatch) -> None:
    settings = RadioSettings()
    saved_positions: list[float] = []
    engine = object.__new__(RadioEngine)
    engine.sample_rate = 10
    engine.channels = 2
    engine.sample_width = 2
    engine._lock = threading.RLock()
    engine._stop = threading.Event()
    engine._restart_podcast = threading.Event()
    engine._play_now_requested = threading.Event()
    engine._status = RadioStatus()
    engine._pcm_source_active = threading.Event()
    engine._active_decoder = None
    engine._persisted_podcast_checkpoint_sec = 0.0
    engine.store = SimpleNamespace(
        settings=settings,
        save=lambda: saved_positions.append(settings.podcast_checkpoint_position_sec),
    )
    engine._pause_spotify = lambda: None  # type: ignore[method-assign]
    engine._write_pcm = lambda _chunk: None  # type: ignore[method-assign]
    decoder = SimpleNamespace(
        stdout=BytesIO(bytes(400)),
        wait=lambda timeout: 0,
    )
    monkeypatch.setattr("radai_agent.radio_engine.subprocess.Popen", lambda *_args, **_kwargs: decoder)

    engine._play_podcast_segment(
        Path("episode.mp3"),
        20.0,
        30.0,
        "Episode",
        "episode-1",
    )

    assert settings.podcast_checkpoint_episode_id == "episode-1"
    assert settings.podcast_checkpoint_position_sec == 30.0
    assert saved_positions[-1] == 30.0


def test_stream_restart_selects_checkpoint_episode_and_resumes_position(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    original = StateStore(state_path)
    original.settings.channels = ["channel-a", "channel-b"]
    original.settings.last_channel_url = "channel-a"
    original.settings.prepared_episodes = {
        "channel-a": ["episode-a"],
        "channel-b": ["episode-b"],
    }
    original.settings.podcast_checkpoint_episode_id = "episode-a"
    original.settings.podcast_checkpoint_position_sec = 37.5
    original.save()
    restored = StateStore(state_path)
    episodes = {
        episode_id: (
            SimpleNamespace(
                episode=SimpleNamespace(id=episode_id, title=episode_id),
            ),
            Path(f"{episode_id}.mp3"),
            SimpleNamespace(ad_cuts=(), music_insertions=()),
        )
        for episode_ids in restored.settings.prepared_episodes.values()
        for episode_id in episode_ids
    }
    engine = object.__new__(RadioEngine)
    engine.store = restored
    engine._lock = threading.RLock()
    engine._status = RadioStatus()
    engine._stop = threading.Event()
    engine._play_now_requested = threading.Event()
    engine._restart_podcast = threading.Event()
    engine._prepared_by_id = lambda episode_id, _channel: episodes.get(episode_id)  # type: ignore[method-assign]
    engine._duration = lambda _path: 100.0  # type: ignore[method-assign]
    engine._wait_while_paused = lambda: None  # type: ignore[method-assign]
    segments: list[tuple[float, float, str]] = []
    engine._play_podcast_segment = (  # type: ignore[method-assign]
        lambda _path, start, end, _title, episode_id: segments.append(
            (start, end, episode_id)
        )
    )

    selected = engine._choose_prepared_episode()
    completed = engine._play_episode(*selected)

    assert selected[0].episode.id == "episode-a"
    assert completed
    assert segments == [(37.5, 100.0, "episode-a")]


def test_state_store_starts_without_personal_channels_and_persists_selection(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.settings.selected_playlist_uri = "spotify:playlist:abc"
    store.settings.selected_playlist_name = "Saved"
    store.settings.prepared_episodes["https://youtube.com/@saved"] = "episode-1"
    store.save()

    restored = StateStore(path)

    assert restored.settings.channels == []
    assert restored.settings.selected_playlist_uri == "spotify:playlist:abc"
    assert restored.settings.selected_playlist_name == "Saved"

    assert restored.settings.prepared_episodes == {
        "https://youtube.com/@saved": ["episode-1"]
    }

def test_html_branding_is_configurable_and_escaped() -> None:
    rendered = _render_html_template(
        "<title>{{SITE_TITLE}}</title><h1>{{SITE_NAME}}</h1>",
        {"SITE_TITLE": "Station & Friends", "SITE_NAME": "<Radio>"},
    )

    assert rendered == (
        b"<title>Station &amp; Friends</title><h1>&lt;Radio&gt;</h1>"
    )


def test_prepared_episode_selection_cycles_after_last_channel() -> None:
    settings = RadioSettings(
        channels=["channel-a", "channel-b", "channel-c"],
        prepared_episodes={
            "channel-a": ["episode-a"],
            "channel-b": ["episode-b"],
            "channel-c": ["episode-c"],
        },
        last_channel_url="channel-a",
    )
    episodes = {
        episode_id: (
            SimpleNamespace(
                episode=SimpleNamespace(
                    id=episode_id,
                    title=episode_id,
                    channel=channel,
                )
            ),
            Path(f"{episode_id}.mp3"),
            SimpleNamespace(),
        )
        for channel, episode_ids in settings.prepared_episodes.items()
        for episode_id in episode_ids
    }
    engine = object.__new__(RadioEngine)
    engine.store = SimpleNamespace(settings=settings)
    engine._prepared_by_id = lambda episode_id, _channel: episodes.get(episode_id)  # type: ignore[method-assign]

    selected = engine._choose_prepared_episode()

    assert selected[0].episode.id == "episode-b"
    settings.last_channel_url = "channel-c"
    assert engine._choose_prepared_episode()[0].episode.id == "episode-a"


def test_channel_preparation_keeps_configured_latest_unplayed_episodes(monkeypatch, tmp_path: Path) -> None:
    latest = SimpleNamespace(id="latest", title="Latest")
    second_latest = SimpleNamespace(id="second", title="Second latest")
    third_latest = SimpleNamespace(id="third", title="Third latest")
    settings = RadioSettings(
        channels=["channel-a"],
        played_episode_ids=["latest"],
        unplayed_episodes_per_source=2,
    )
    selected: list[object] = []
    engine = object.__new__(RadioEngine)
    engine.store = SimpleNamespace(settings=settings, save=lambda: None)
    engine._lock = threading.RLock()
    engine._status = RadioStatus(state="running")
    engine.media_dir = tmp_path / "media"
    engine.transcript_dir = tmp_path / "transcripts"
    engine.processed_dir = tmp_path / "processed"
    engine._yt_dlp = lambda: "yt-dlp"  # type: ignore[method-assign]
    engine._yt_dlp_options = lambda: ()  # type: ignore[method-assign]
    engine._plan_episode = lambda downloaded: SimpleNamespace(ad_cuts=())  # type: ignore[method-assign]
    engine._remove_ads = lambda downloaded, cuts: tmp_path / "second.mp3"  # type: ignore[method-assign]
    engine._prepared_by_id = lambda *_args: None  # type: ignore[method-assign]
    engine._remember_episode = lambda *_args: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        "radai_agent.radio_engine.list_channel_episodes",
        lambda *args, **kwargs: (latest, second_latest, third_latest),
    )

    def download(episode, *args, **kwargs):
        selected.append(episode)
        return SimpleNamespace()

    monkeypatch.setattr("radai_agent.radio_engine.download_episode", download)

    engine._prepare_channel("channel-a")

    assert selected == [second_latest, third_latest]
    assert settings.prepared_episodes == {"channel-a": ["second", "third"]}


def test_podcast_chooser_queues_prepared_episode_and_rotation_continues_after_it() -> None:
    settings = RadioSettings(
        channels=["channel-a", "channel-b"],
        prepared_episodes={
            "channel-a": ["episode-a"],
            "channel-b": ["episode-b"],
        },
        episode_history={
            "episode-b": {"source_url": "channel-b"},
        },
    )
    episodes = {
        episode_id: (
            SimpleNamespace(
                episode=SimpleNamespace(
                    id=episode_id,
                    title=f"Title {episode_id}",
                    channel=channel,
                )
            ),
            Path(f"{episode_id}.mp3"),
            SimpleNamespace(),
        )
        for channel, episode_ids in settings.prepared_episodes.items()
        for episode_id in episode_ids
    }
    engine = object.__new__(RadioEngine)
    engine._lock = threading.RLock()
    engine._current_episode_id = None
    engine.store = SimpleNamespace(settings=settings, save=lambda: None)
    engine._prepared_by_id = lambda episode_id, _channel: episodes.get(episode_id)  # type: ignore[method-assign]
    engine._enforce_storage_retention = lambda: None  # type: ignore[method-assign]

    queued = engine.queue_prepared_episode("episode-b")

    assert queued["title"] == "Title episode-b"
    assert settings.queued_video_id == "episode-b"
    engine._consume_queued_episode("episode-b")
    assert settings.queued_video_id is None
    engine._mark_played("episode-b")
    assert settings.last_channel_url == "channel-b"
    assert settings.queued_video_id is None
    assert engine._channels_in_playback_order() == ["channel-a", "channel-b"]


def test_podcast_retention_defaults_to_one_per_source() -> None:
    settings = RadioSettings()

    assert settings.unplayed_episodes_per_source == 1
    assert settings.played_episodes_per_source == 1


def test_podcast_chooser_can_interrupt_with_play_now() -> None:
    settings = RadioSettings(
        channels=["channel-a"],
        prepared_episodes={"channel-a": ["episode-a"]},
    )
    prepared = (
        SimpleNamespace(
            episode=SimpleNamespace(
                id="episode-a",
                title="Episode A",
                channel="Channel A",
            )
        ),
        Path("episode-a.mp3"),
        SimpleNamespace(),
    )
    requested: list[str] = []
    engine = object.__new__(RadioEngine)
    engine._lock = threading.RLock()
    engine._current_episode_id = None
    engine.store = SimpleNamespace(settings=settings, save=lambda: None)
    engine._prepared_by_id = lambda *_args: prepared  # type: ignore[method-assign]
    engine._request_play_now = lambda: requested.append("play-now")  # type: ignore[method-assign]

    selected = engine.queue_prepared_episode("episode-a", "play_now")

    assert selected["title"] == "Episode A"
    assert settings.queued_video_id == "episode-a"
    assert requested == ["play-now"]


def test_history_replays_prepared_episode_without_removing_history() -> None:
    settings = RadioSettings(
        played_episode_ids=["missing", "prepared"],
        episode_history={
            "missing": {
                "title": "Missing episode",
                "channel": "Channel A",
                "source_url": "channel-a",
                "url": "https://youtube.com/watch?v=missing",
            },
            "prepared": {
                "title": "Prepared episode",
                "channel": "Channel A",
                "source_url": "channel-a",
                "url": "https://youtube.com/watch?v=prepared",
            },
        },
    )
    requested: list[str] = []
    engine = object.__new__(RadioEngine)
    engine.store = SimpleNamespace(settings=settings, save=lambda: None)
    engine._prepared_by_id = (  # type: ignore[method-assign]
        lambda episode_id, _channel: (object(), Path("prepared.mp3"), object())
        if episode_id == "prepared"
        else None
    )
    engine._request_play_now = lambda: requested.append("play-now")  # type: ignore[method-assign]

    history = engine.podcast_history()
    replayed = engine.replay_history_episode("prepared")

    assert [(item["id"], item["prepared"]) for item in history] == [
        ("prepared", True),
        ("missing", False),
    ]
    assert replayed["title"] == "Prepared episode"
    assert settings.played_episode_ids == ["missing", "prepared"]
    assert settings.queued_video_id == "prepared"
    assert requested == ["play-now"]


def test_storage_retention_keeps_configured_unplayed_and_played_counts(tmp_path: Path) -> None:
    settings = RadioSettings(
        prepared_episodes={"channel-a": ["unplayed-a", "unplayed-b"]},
        played_episode_ids=["played-a1", "played-b1", "played-a2", "played-b2"],
        episode_history={
            "unplayed-a": {"source_url": "channel-a"},
            "unplayed-b": {"source_url": "channel-a"},
            "played-a1": {"source_url": "channel-a"},
            "played-a2": {"source_url": "channel-a"},
            "played-b1": {"source_url": "channel-b"},
            "played-b2": {"source_url": "channel-b"},
        },
        unplayed_episodes_per_source=1,
        played_episodes_per_source=1,
    )
    engine = object.__new__(RadioEngine)
    engine.store = SimpleNamespace(settings=settings, save=lambda: None)
    engine.media_dir = tmp_path / "media"
    engine.transcript_dir = tmp_path / "transcripts"
    engine.processed_dir = tmp_path / "processed"
    engine._current_episode_id = None
    for directory in (engine.media_dir, engine.transcript_dir, engine.processed_dir):
        directory.mkdir()
        for episode_id in settings.episode_history:
            (directory / f"{episode_id}.cache").write_text("cached")

    engine._enforce_storage_retention()

    assert settings.prepared_episodes == {"channel-a": ["unplayed-a"]}
    for retained_id in ("unplayed-a", "played-a2", "played-b2"):
        assert (engine.processed_dir / f"{retained_id}.cache").exists()
    for deleted_id in ("unplayed-b", "played-a1", "played-b1"):
        assert not (engine.processed_dir / f"{deleted_id}.cache").exists()