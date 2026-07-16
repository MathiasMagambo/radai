from __future__ import annotations

import json
import os
import random
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable

from .deepseek import DeepSeekClient, DeepSeekError, EpisodePlanningInput, parse_deepseek_plan
from .audio import build_prepare_command
from .models import CutRange, DeepSeekPlan, MusicInsertion
from .spotify_desktop import SpotifyDesktopController
from .youtube import DownloadedEpisode, YouTubeEpisode, download_episode, inspect_video_url, list_channel_episodes, validate_channel_url



class RadioError(RuntimeError):
    pass


@dataclass
class RadioSettings:
    channels: list[str] = field(default_factory=list)
    channel_names: dict[str, str] = field(default_factory=dict)
    selected_playlist_uri: str | None = None
    selected_playlist_name: str | None = None
    seed_track_uri: str | None = None
    seed_track_name: str | None = None
    played_episode_ids: list[str] = field(default_factory=list)
    episode_history: dict[str, dict[str, object]] = field(default_factory=dict)
    prepared_episodes: dict[str, list[str]] = field(default_factory=dict)
    unplayed_episodes_per_source: int = 1
    played_episodes_per_source: int = 1
    playback_mode: str = "resumable"
    music_placement: str = "ads"
    songs_per_break: int = 3
    queued_video_id: str | None = None
    queued_video_title: str | None = None
    pending_video_id: str | None = None
    pending_video_title: str | None = None
    podcast_selector_enabled: bool = False
    restart_current_podcast_enabled: bool = False
    last_channel_url: str | None = None
    paused_playlist_uri: str | None = None
    paused_playlist_name: str | None = None
    paused_track_uri: str | None = None
    paused_track_name: str | None = None
    active_music_source_uri: str | None = None
    active_music_source_name: str | None = None
    podcast_checkpoint_episode_id: str | None = None
    podcast_checkpoint_position_sec: float = 0.0


@dataclass
class RadioStatus:
    state: str = "stopped"
    detail: str = "Radio is stopped"
    mode: str = "idle"
    now_playing: str = ""
    podcast: str = ""
    started_at: float | None = None
    error: str | None = None
    preparation_error: str | None = None


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.settings = self._load()

    def _load(self) -> RadioSettings:
        if not self.path.exists():
            settings = RadioSettings()
            self.save(settings)
            return settings
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        prepared = raw.get("prepared_episodes")
        if isinstance(prepared, dict):
            raw["prepared_episodes"] = {
                str(channel): [
                    str(episode_id)
                    for episode_id in (episode_ids if isinstance(episode_ids, list) else [episode_ids])
                    if episode_id
                ]
                for channel, episode_ids in prepared.items()
            }
        return RadioSettings(**{key: raw[key] for key in RadioSettings.__dataclass_fields__ if key in raw})

    def save(self, settings: RadioSettings | None = None) -> None:
        with self._lock:
            if settings is not None:
                self.settings = settings
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(self.settings), indent=2, sort_keys=True), encoding="utf-8")
            temporary.replace(self.path)

    def add_channel(self, url: str) -> str:
        channel = validate_channel_url(url)
        with self._lock:
            if channel not in self.settings.channels:
                self.settings.channels.append(channel)
                self.save()
        return channel

    def remove_channel(self, url: str) -> None:
        with self._lock:
            self.settings.channels = [channel for channel in self.settings.channels if channel != url]
            self.settings.prepared_episodes.pop(url, None)
            self.settings.channel_names.pop(url, None)
            if self.settings.last_channel_url == url:
                self.settings.last_channel_url = None
            self.save()


class RadioEngine:
    sample_rate = 44_100
    channels = 2
    sample_width = 2

    def __init__(
        self,
        *,
        root: Path,
        spotify_desktop: SpotifyDesktopController,
        deepseek: DeepSeekClient,
        state_store: StateStore,
        icecast_password: str,
        icecast_port: int = 8001,
        spotifyd_path: Path = Path.home() / ".local/bin/spotifyd",
        spotify_device_name: str = "Radai Radio",
    ) -> None:
        self.root = root
        self.spotify_desktop = spotify_desktop
        self.deepseek = deepseek
        self.store = state_store
        self.icecast_password = icecast_password
        self.icecast_port = icecast_port
        self.spotifyd_path = spotifyd_path
        self.spotify_device_name = spotify_device_name
        self.media_dir = root / "data/media"
        self.transcript_dir = root / "data/transcripts"
        self.processed_dir = root / "data/processed"
        self.spotifyd_config = root / "data/state/spotifyd-radio.conf"
        self.spotifyd_audio_pipe = root / "data/state/spotifyd-audio.pcm"
        self._lock = threading.RLock()
        self._pcm_lock = threading.Lock()
        self._status = RadioStatus()
        self._thread: threading.Thread | None = None
        self._preparation_lock = threading.Lock()
        self._preparation_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._spotify_audio_enabled = threading.Event()
        self._spotify_audio_ready = threading.Event()
        self._pcm_source_active = threading.Event()
        self._last_pcm_write = 0.0
        self._playback_paused = threading.Event()
        self._pause_generation = 0
        self._source_paused = False
        self._encoder: subprocess.Popen[bytes] | None = None
        self._spotifyd: subprocess.Popen[bytes] | None = None
        self._active_decoder: subprocess.Popen[bytes] | None = None
        self._prepare_now = threading.Event()
        self._video_thread: threading.Thread | None = None
        self._preparing_history_id: str | None = None
        self._current_episode_id: str | None = None
        self._persisted_podcast_checkpoint_sec = (
            self.store.settings.podcast_checkpoint_position_sec
        )
        self._restart_podcast = threading.Event()
        self._play_now_requested = threading.Event()
        self._backfill_episode_history()
        self._enforce_storage_retention()

    def status(self) -> RadioStatus:
        with self._lock:
            return RadioStatus(**asdict(self._status))

    def start(self) -> RadioStatus:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._stop.clear()
            self._playback_paused.clear()
            self._status = RadioStatus(
                state="starting",
                detail="Starting a prepared podcast",
                mode="preparing",
                started_at=time.time(),
            )
            self._thread = threading.Thread(target=self._run, name="radai-radio", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self) -> RadioStatus:
        self._stop.set()
        self._restart_podcast.clear()
        self._play_now_requested.clear()
        with self._lock:
            self._pause_generation += 1
            self._playback_paused.clear()
        self._spotify_audio_enabled.clear()
        self._terminate(self._active_decoder)
        checkpoint_id = self.store.settings.podcast_checkpoint_episode_id
        if checkpoint_id:
            self._checkpoint_podcast(
                checkpoint_id,
                self.store.settings.podcast_checkpoint_position_sec,
                force=True,
            )
        self._pause_spotify()
        self._terminate(self._spotifyd)
        if self._encoder and self._encoder.stdin:
            try:
                self._encoder.stdin.close()
            except OSError:
                pass
        self._terminate(self._encoder)
        with self._lock:
            self._status = RadioStatus(state="stopped", detail="Radio is stopped", mode="idle")
        return self.status()
    def pause(self, *, delay_sec: float = 0.0) -> RadioStatus:
        if self.store.settings.playback_mode != "resumable":
            return self.status()
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return self.status()
            self._pause_generation += 1
            generation = self._pause_generation
            self._playback_paused.set()
            self._status.state = "paused"
        threading.Thread(
            target=self._pause_source_after_delay,
            args=(generation, max(0.0, delay_sec)),
            name="radai-delayed-pause",
            daemon=True,
        ).start()
        return self.status()

    def resume(self) -> RadioStatus:
        with self._lock:
            self._pause_generation += 1
            self._playback_paused.clear()
            source_paused = self._source_paused
            mode = self._status.mode
            decoder = self._active_decoder
        if source_paused:
            self._pcm_source_active.set()
            if decoder is not None and decoder.poll() is None:
                decoder.send_signal(signal.SIGCONT)
            elif mode == "music":
                self.spotify_desktop.resume()
            with self._lock:
                self._source_paused = False
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._status.state = "running"
                self._status.detail = "Podcast segment" if mode == "podcast" else f"{self.store.settings.songs_per_break}-song music break"
        return self.status()

    def source_paused(self) -> bool:
        with self._lock:
            return self._source_paused

    def restart_current_podcast(self) -> RadioStatus:
        if not self.store.settings.restart_current_podcast_enabled:
            raise RadioError("Podcast restart is disabled in settings")
        with self._lock:
            if not self._current_episode_id or self._status.state not in {"running", "paused"}:
                raise RadioError("No podcast is currently playing")
            episode_id = self._current_episode_id
            self._pause_generation += 1
            self._playback_paused.clear()
            self._restart_podcast.set()
            self._status.state = "running"
            self._status.detail = "Restarting podcast"
        self._terminate(self._active_decoder)
        self._checkpoint_podcast(episode_id, 0.0, force=True)
        self._pause_spotify()
        return self.status()


    def update_settings(
        self,
        *,
        playback_mode: str,
        music_placement: str,
        songs_per_break: int,
        podcast_selector_enabled: bool,
        restart_current_podcast_enabled: bool,
        unplayed_episodes_per_source: int,
        played_episodes_per_source: int,
    ) -> RadioSettings:
        if playback_mode not in {"resumable", "radio"}:
            raise ValueError("playback mode must be resumable or radio")
        if music_placement not in {"ads", "sections"}:
            raise ValueError("music placement must be ads or sections")
        if not 1 <= songs_per_break <= 10:
            raise ValueError("songs per break must be between 1 and 10")
        if not isinstance(podcast_selector_enabled, bool):
            raise ValueError("podcast selector setting must be true or false")
        if not isinstance(restart_current_podcast_enabled, bool):
            raise ValueError("restart podcast setting must be true or false")
        if not 1 <= unplayed_episodes_per_source <= 20:
            raise ValueError("unplayed episodes per source must be between 1 and 20")
        if not 1 <= played_episodes_per_source <= 20:
            raise ValueError("played episodes per source must be between 1 and 20")
        was_paused = self._playback_paused.is_set()
        settings = self.store.settings
        settings.playback_mode = playback_mode
        settings.music_placement = music_placement
        settings.songs_per_break = songs_per_break
        settings.podcast_selector_enabled = podcast_selector_enabled
        settings.restart_current_podcast_enabled = restart_current_podcast_enabled
        settings.unplayed_episodes_per_source = unplayed_episodes_per_source
        settings.played_episodes_per_source = played_episodes_per_source
        self.store.save()
        self._enforce_storage_retention()
        self.prepare_in_background()
        if playback_mode == "radio" and was_paused:
            self.resume()
        return settings

    def _pause_source_after_delay(self, generation: int, delay_sec: float) -> None:
        deadline = time.monotonic() + delay_sec
        while time.monotonic() < deadline:
            if generation != self._pause_generation or not self._playback_paused.is_set():
                return
            time.sleep(min(0.1, deadline - time.monotonic()))
        with self._lock:
            if generation != self._pause_generation or not self._playback_paused.is_set():
                return
            mode = self._status.mode
            decoder = self._active_decoder
        if decoder is not None and decoder.poll() is None:
            decoder.send_signal(signal.SIGSTOP)
        elif mode == "music":
            try:
                playback = self.spotify_desktop.current_playback()
                settings = self.store.settings
                settings.paused_playlist_uri = settings.selected_playlist_uri
                settings.paused_playlist_name = settings.selected_playlist_name
                settings.paused_track_uri = playback.track.uri if playback.track else None
                settings.paused_track_name = playback.track.name if playback.track else None
                self.store.save()
            finally:
                self._pause_spotify()
        with self._lock:
            if generation == self._pause_generation and self._playback_paused.is_set():
                self._source_paused = True
                self._pcm_source_active.clear()

    def set_playlist(self, uri: str | None, name: str | None) -> None:
        settings = self.store.settings
        settings.selected_playlist_uri = uri or None
        settings.selected_playlist_name = name or None
        settings.seed_track_uri = None
        settings.seed_track_name = None
        self.store.save()
        with self._lock:
            switch_now = self._status.mode == "music" and self._status.state in {"running", "paused"}
        if switch_now:
            playlists = self.spotify_desktop.playlists()
            playlist = next((item for item in playlists if item.uri == uri), None)
            playlist = playlist or (random.choice(playlists) if playlists else None)
            if playlist is None:
                raise RadioError("Spotify account has no saved playlists")
            self.spotify_desktop.play_context(self.spotify_device_name, playlist.uri, shuffle=True)
            settings.active_music_source_uri = playlist.uri
            settings.active_music_source_name = playlist.name
            self.store.save()

    def set_radio_track(self, uri: str, name: str) -> None:
        settings = self.store.settings
        settings.seed_track_uri = uri
        settings.seed_track_name = name
        self.store.save()
        with self._lock:
            switch_now = self._status.mode == "music" and self._status.state in {"running", "paused"}
        if switch_now:
            self.spotify_desktop.play_track_radio(
                self.spotify_device_name,
                uri,
                search_query=name,
            )
            settings.active_music_source_uri = uri
            settings.active_music_source_name = name
            self.store.save()


    def _run(self) -> None:
        try:
            prepared = self._choose_prepared_episode()
            self._start_pipeline()
            while not self._stop.is_set():
                downloaded, processed, plan = prepared
                with self._lock:
                    self._current_episode_id = downloaded.episode.id
                self._consume_queued_episode(downloaded.episode.id)
                completed = self._play_episode(downloaded, processed, plan)
                if self._stop.is_set():
                    break
                if not completed:
                    self._play_now_requested.clear()
                    prepared = self._choose_prepared_episode()
                    continue
                self._mark_played(downloaded.episode.id)
                with self._lock:
                    self._current_episode_id = None
                self.prepare_in_background()
                prepared = self._choose_prepared_episode()
        except Exception as exc:
            if not self._stop.is_set():
                with self._lock:
                    self._status.state = "error"
                    self._status.mode = "idle"
                    self._status.error = str(exc)
                    self._status.detail = "Radio stopped after an error"
        finally:
            with self._lock:
                self._current_episode_id = None
            self._spotify_audio_enabled.clear()
            self._pause_spotify()
            self._terminate(self._active_decoder)
            self._terminate(self._spotifyd)
            if self._encoder and self._encoder.stdin:
                try:
                    self._encoder.stdin.close()
                except OSError:
                    pass
            self._terminate(self._encoder)

    def _start_pipeline(self) -> None:
        if not self.icecast_password:
            raise RadioError("Icecast source credential is empty")
        password = urllib.parse.quote(self.icecast_password, safe="")
        url = f"icecast://source:{password}@127.0.0.1:{self.icecast_port}/spotify.mp3"
        self._encoder = subprocess.Popen(
            (
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-re",
                "-f",
                "s16le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                str(self.channels),
                "-i",
                "pipe:0",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-content_type",
                "audio/mpeg",
                "-f",
                "mp3",
                url,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )
        threading.Thread(
            target=self._keep_stream_alive,
            name="radai-stream-keepalive",
            daemon=True,
        ).start()
        self.spotifyd_audio_pipe.unlink(missing_ok=True)
        os.mkfifo(self.spotifyd_audio_pipe, mode=0o600)
        self._write_spotifyd_config()
        self._spotifyd = subprocess.Popen(
            (str(self.spotifyd_path), "--no-daemon", "--config-path", str(self.spotifyd_config)),
            stdout=subprocess.DEVNULL,
        )
        threading.Thread(
            target=self._drain_spotifyd_audio,
            name="radai-spotify-audio",
            daemon=True,
        ).start()
        with self._lock:
            self._status.state = "running"
            self._status.detail = "Playing a prepared podcast"

    def prepare_in_background(self) -> None:
        with self._preparation_lock:
            if self._preparation_thread and self._preparation_thread.is_alive():
                self._prepare_now.set()
                return
            self._preparation_thread = threading.Thread(
                target=self._preparation_loop,
                name="radai-podcast-preparer",
                daemon=True,
            )
            self._preparation_thread.start()

    def _preparation_loop(self) -> None:
        while True:
            self._prepare_now.clear()
            self._prepare_missing_channels()
            self._prepare_now.wait(timeout=30 * 60)

    def _prepare_missing_channels(self) -> None:
        channels = list(self.store.settings.channels)
        errors: list[str] = []
        for channel in channels:
            try:
                self._prepare_channel(channel)
            except Exception as exc:
                errors.append(
                    f"LLM API error: {exc}" if isinstance(exc, DeepSeekError) else str(exc)
                )
                print(f"Podcast preparation failed for {channel}: {exc}", flush=True)
        with self._lock:
            self._status.preparation_error = (
                f"Podcast preparation failed: {errors[0]}" if errors else None
            )

    def _prepare_channel(self, channel: str) -> None:
        settings = self.store.settings
        played = set(settings.played_episode_ids)
        existing_ids = list(settings.prepared_episodes.get(channel, []))
        reserved = {
            episode_id
            for other_channel, episode_ids in settings.prepared_episodes.items()
            if other_channel != channel
            for episode_id in episode_ids
        }
        episodes = list_channel_episodes(
            channel,
            yt_dlp=self._yt_dlp(),
            extra_args=self._yt_dlp_options(),
        )
        episode_channel = str(getattr(episodes[0], "channel", "")) if episodes else ""
        if episode_channel:
            settings.channel_names[channel] = episode_channel
        candidates = [
            episode
            for episode in episodes
            if episode.id not in played and episode.id not in reserved
        ][: settings.unplayed_episodes_per_source]
        if not candidates:
            raise RadioError(f"No unplayed podcast is available for {channel}")
        prepared_ids: list[str] = []
        for episode in candidates:
            prepared = self._prepared_by_id(episode.id, channel)
            if prepared is None:
                with self._lock:
                    if self._status.state == "stopped":
                        self._status.detail = f"Preparing {episode.title}"
                downloaded = download_episode(
                    episode,
                    self.media_dir,
                    self.transcript_dir,
                    yt_dlp=self._yt_dlp(),
                    extra_args=self._yt_dlp_options(),
                )
                plan = self._plan_episode(downloaded)
                self._remove_ads(downloaded, plan.ad_cuts)
            else:
                downloaded = prepared[0]
            self._remember_episode(downloaded, channel)
            prepared_ids.append(episode.id)
        settings.prepared_episodes[channel] = prepared_ids
        self.store.save()
        for episode_id in set(existing_ids) - set(prepared_ids):
            self._delete_episode_assets_if_unretained(episode_id)
        with self._lock:
            if self._status.state == "stopped":
                self._status.detail = "Radio is stopped"

    def _prepared_episode(
        self,
        channel: str,
    ) -> tuple[DownloadedEpisode, Path, DeepSeekPlan] | None:
        for episode_id in self.store.settings.prepared_episodes.get(channel, []):
            prepared = self._prepared_by_id(episode_id, channel)
            if prepared is not None:
                return prepared
        return None

    def _prepared_by_id(
        self,
        episode_id: str,
        channel: str,
    ) -> tuple[DownloadedEpisode, Path, DeepSeekPlan] | None:
        info_path = self.media_dir / f"{episode_id}.info.json"
        transcript_path = self.transcript_dir / f"{episode_id}.txt"
        processed_path = self.processed_dir / f"{episode_id}.mp3"
        plan_path = self.processed_dir / f"{episode_id}.plan.json"
        if not all(path.exists() for path in (info_path, transcript_path, processed_path, plan_path)):
            return None
        info = json.loads(info_path.read_text(encoding="utf-8"))
        episode = YouTubeEpisode(
            id=episode_id,
            title=str(info.get("title") or episode_id),
            url=str(info.get("webpage_url") or f"https://www.youtube.com/watch?v={episode_id}"),
            channel=str(info.get("channel") or info.get("uploader") or channel),
            duration_sec=float(info["duration"]) if info.get("duration") is not None else None,
        )
        downloaded = DownloadedEpisode(episode, processed_path, transcript_path, info_path)
        plan = parse_deepseek_plan(
            json.loads(plan_path.read_text(encoding="utf-8")),
            duration_sec=None,
        )
        return downloaded, processed_path, plan

    def queue_video(self, url: str) -> None:
        with self._preparation_lock:
            if self._video_thread and self._video_thread.is_alive():
                raise RadioError("Another YouTube video is already being prepared")
            self._video_thread = threading.Thread(
                target=self._prepare_video,
                args=(url,),
                name="radai-video-preparer",
                daemon=True,
            )
            self._video_thread.start()

    def _prepare_video(self, url: str) -> None:
        try:
            episode = inspect_video_url(
                url,
                yt_dlp=self._yt_dlp(),
                extra_args=self._yt_dlp_options(),
            )
            self._prepare_on_demand_episode(episode)
            settings = self.store.settings
            settings.pending_video_id = episode.id
            settings.pending_video_title = episode.title
            self.store.save()
            with self._lock:
                self._status.preparation_error = None
                if self._status.state == "stopped":
                    self._status.detail = "Radio is stopped"
        except Exception as exc:
            with self._lock:
                self._status.preparation_error = f"YouTube preparation failed: {exc}"

    def _prepare_on_demand_episode(self, episode: YouTubeEpisode) -> DownloadedEpisode:
        with self._lock:
            if self._status.state == "stopped":
                self._status.detail = f"Preparing {episode.title}"
        downloaded = download_episode(
            episode,
            self.media_dir,
            self.transcript_dir,
            yt_dlp=self._yt_dlp(),
            extra_args=self._yt_dlp_options(),
            require_transcript=False,
        )
        if downloaded.transcript_path.stat().st_size:
            plan = self._plan_episode(downloaded)
        else:
            plan = DeepSeekPlan(episode_id=episode.id)
            self.processed_dir.mkdir(parents=True, exist_ok=True)
            (self.processed_dir / f"{episode.id}.plan.json").write_text(
                json.dumps(
                    {
                        "episode_id": episode.id,
                        "ad_cuts": [],
                        "music_insertions": [],
                        "confidence": 0.0,
                        "warnings": ["Video has no transcript; playing without analysis."],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        self._remove_ads(downloaded, plan.ad_cuts)
        self._remember_episode(downloaded)
        self.store.save()
        return downloaded

    def prepare_history_episode(self, episode_id: str) -> dict[str, object]:
        match = next(
            (item for item in self.podcast_history() if item["id"] == episode_id),
            None,
        )
        if match is None:
            raise RadioError("Podcast history entry is not available")
        if match["prepared"]:
            return match
        url = str(match["url"])
        if not url:
            raise RadioError("Podcast history entry has no source URL")
        with self._preparation_lock:
            if self._video_thread and self._video_thread.is_alive():
                raise RadioError("Another YouTube video is already being prepared")
            self._preparing_history_id = episode_id
            self._video_thread = threading.Thread(
                target=self._prepare_history_episode,
                args=(episode_id, url),
                name="radai-history-preparer",
                daemon=True,
            )
            self._video_thread.start()
        return match

    def _prepare_history_episode(self, episode_id: str, url: str) -> None:
        try:
            episode = inspect_video_url(
                url,
                yt_dlp=self._yt_dlp(),
                extra_args=self._yt_dlp_options(),
            )
            if episode.id != episode_id:
                raise RadioError("YouTube returned a different podcast episode")
            self._prepare_on_demand_episode(episode)
            with self._lock:
                self._status.preparation_error = None
                if self._status.state == "stopped":
                    self._status.detail = "Radio is stopped"
        except Exception as exc:
            with self._lock:
                self._status.preparation_error = f"YouTube preparation failed: {exc}"
        finally:
            self._preparing_history_id = None

    def resolve_prepared_video(self, action: str) -> RadioSettings:
        if action not in {"queue", "play_now"}:
            raise ValueError("Video action must be queue or play_now")
        settings = self.store.settings
        if not settings.pending_video_id or not settings.pending_video_title:
            raise RadioError("No prepared YouTube video is waiting")
        settings.played_episode_ids = [
            episode_id
            for episode_id in settings.played_episode_ids
            if episode_id != settings.pending_video_id
        ]
        settings.queued_video_id = settings.pending_video_id
        settings.queued_video_title = settings.pending_video_title
        settings.pending_video_id = None
        settings.pending_video_title = None
        self.store.save()
        if action == "play_now":
            self._request_play_now()
        return settings

    def _request_play_now(self) -> None:
        with self._lock:
            active = bool(self._thread and self._thread.is_alive())
            self._pause_generation += 1
            self._playback_paused.clear()
            if active:
                self._play_now_requested.set()
                self._status.state = "running"
                self._status.detail = "Switching podcast"
        if active:
            self._terminate(self._active_decoder)
            self._pause_spotify()
        else:
            self._play_now_requested.clear()
            self.start()



    def _choose_prepared_episode(self) -> tuple[DownloadedEpisode, Path, DeepSeekPlan]:
        settings = self.store.settings
        played = set(settings.played_episode_ids)
        if settings.queued_video_id:
            queued = self._prepared_by_id(settings.queued_video_id, "YouTube")
            if queued is not None:
                return queued
        checkpoint_id = settings.podcast_checkpoint_episode_id
        if checkpoint_id and checkpoint_id not in played:
            for channel, episode_ids in settings.prepared_episodes.items():
                if checkpoint_id not in episode_ids:
                    continue
                checkpoint = self._prepared_by_id(checkpoint_id, channel)
                if checkpoint is not None:
                    return checkpoint
        for channel in self._channels_in_playback_order():
            for episode_id in settings.prepared_episodes.get(channel, []):
                if episode_id in played:
                    continue
                prepared = self._prepared_by_id(episode_id, channel)
                if prepared is not None:
                    return prepared
        raise RadioError("No prepared podcast is ready yet")

    def _channels_in_playback_order(self) -> list[str]:
        channels = list(self.store.settings.channels)
        last_channel = self.store.settings.last_channel_url
        if not channels or last_channel not in channels:
            return channels
        next_index = (channels.index(last_channel) + 1) % len(channels)
        return channels[next_index:] + channels[:next_index]

    def channel_sources(self) -> list[dict[str, str]]:
        settings = self.store.settings
        sources: list[dict[str, str]] = []
        for url in settings.channels:
            name = settings.channel_names.get(url, "")
            episode_ids = settings.prepared_episodes.get(url, [])
            info_path = self.media_dir / f"{episode_ids[0]}.info.json" if episode_ids else None
            if not name and info_path and info_path.exists():
                info = json.loads(info_path.read_text(encoding="utf-8"))
                name = str(info.get("channel") or info.get("uploader") or "")
            if not name:
                name = url.rstrip("/").rsplit("/", 1)[-1].lstrip("@") or "YouTube channel"
            sources.append({"url": url, "name": name})
        return sources

    def prepared_podcasts(self) -> list[dict[str, object]]:
        settings = self.store.settings
        played = set(settings.played_episode_ids)
        with self._lock:
            current_episode_id = self._current_episode_id
        podcasts: list[dict[str, object]] = []
        for channel in self._channels_in_playback_order():
            for episode_id in settings.prepared_episodes.get(channel, []):
                if episode_id in played or episode_id == current_episode_id:
                    continue
                prepared = self._prepared_by_id(episode_id, channel)
                if prepared is None:
                    continue
                episode = prepared[0].episode
                podcasts.append(
                    {
                        "id": episode.id,
                        "title": episode.title,
                        "channel": episode.channel,
                        "channel_url": channel,
                        "queued": episode.id == settings.queued_video_id,
                    }
                )
        return podcasts

    def queue_prepared_episode(
        self,
        episode_id: str,
        action: str = "queue",
    ) -> dict[str, object]:
        match = next(
            (podcast for podcast in self.prepared_podcasts() if podcast["id"] == episode_id),
            None,
        )
        if match is None:
            raise RadioError("Prepared podcast is not available")
        self._queue_episode(episode_id, str(match["title"]), action)
        return match

    def podcast_history(self) -> list[dict[str, object]]:
        settings = self.store.settings
        history: list[dict[str, object]] = []
        for episode_id in reversed(settings.played_episode_ids):
            entry = settings.episode_history.get(episode_id)
            if not entry:
                continue
            source_url = str(entry.get("source_url") or "YouTube")
            history.append(
                {
                    "id": episode_id,
                    "title": str(entry.get("title") or episode_id),
                    "channel": str(entry.get("channel") or "YouTube"),
                    "source_url": source_url,
                    "url": str(entry.get("url") or ""),
                    "prepared": self._prepared_by_id(episode_id, source_url) is not None,
                    "preparing": episode_id == getattr(self, "_preparing_history_id", None),
                }
            )
        return history

    def replay_history_episode(self, episode_id: str) -> dict[str, object]:
        match = next(
            (item for item in self.podcast_history() if item["id"] == episode_id),
            None,
        )
        if match is None:
            raise RadioError("Podcast history entry is not available")
        if not match["prepared"]:
            raise RadioError("Podcast must be prepared before it can play again")
        self._queue_episode(episode_id, str(match["title"]), "play_now")
        return match

    def _queue_episode(self, episode_id: str, title: str, action: str) -> None:
        if action not in {"queue", "play_now"}:
            raise ValueError("Podcast action must be queue or play_now")
        settings = self.store.settings
        settings.queued_video_id = episode_id
        settings.queued_video_title = title
        self.store.save()
        if action == "play_now":
            self._request_play_now()

    def _plan_episode(self, downloaded: DownloadedEpisode) -> DeepSeekPlan:
        transcript = downloaded.transcript_path.read_text(encoding="utf-8")
        info = json.loads(downloaded.info_path.read_text(encoding="utf-8")) if downloaded.info_path.exists() else {}
        duration = self._duration(downloaded.audio_path)
        plan_path = self.processed_dir / f"{downloaded.episode.id}.plan.json"
        if plan_path.exists():
            return parse_deepseek_plan(json.loads(plan_path.read_text(encoding="utf-8")), duration_sec=duration)
        chunks = _transcript_chunks(transcript, 45_000)
        cuts: list[CutRange] = []
        insertions: list[MusicInsertion] = []
        warnings: list[str] = []
        confidence: list[float] = []
        with self._lock:
            if self._status.state == "stopped":
                self._status.detail = f"Scanning {downloaded.episode.title} for ads"
        for chunk in chunks:
            plan = self.deepseek.plan_episode(
                EpisodePlanningInput(
                    episode_id=downloaded.episode.id,
                    show=downloaded.episode.channel,
                    title=downloaded.episode.title,
                    description=str(info.get("description") or ""),
                    transcript=chunk,
                    duration_sec=duration,
                    podcast_window_sec=20 * 60,
                    music_window_sec=3 * 4 * 60,
                ),
                strict_duration=False,
            )
            cuts.extend(plan.ad_cuts)
            insertions.extend(plan.music_insertions)
            warnings.extend(plan.warnings)
            confidence.append(plan.confidence)
        valid_cuts = [cut for cut in cuts if cut.end_sec <= duration]
        valid_insertions = [insertion for insertion in insertions if insertion.after_sec <= duration]
        discarded = len(cuts) - len(valid_cuts) + len(insertions) - len(valid_insertions)
        if discarded:
            warnings.append(f"Discarded {discarded} out-of-range model suggestion(s)")
        final_plan = DeepSeekPlan(
            episode_id=downloaded.episode.id,
            ad_cuts=_merge_cuts(valid_cuts),
            music_insertions=_dedupe_insertions(valid_insertions),
            confidence=sum(confidence) / len(confidence),
            warnings=tuple(warnings),
            raw={"chunks": len(chunks)},
        )
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            json.dumps(
                {
                    "episode_id": final_plan.episode_id,
                    "ad_cuts": [asdict(cut) for cut in final_plan.ad_cuts],
                    "music_insertions": [asdict(insertion) for insertion in final_plan.music_insertions],
                    "confidence": final_plan.confidence,
                    "warnings": list(final_plan.warnings),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return final_plan

    def _remove_ads(self, downloaded: DownloadedEpisode, cuts: tuple[CutRange, ...]) -> Path:
        output = self.processed_dir / f"{downloaded.episode.id}.mp3"
        if output.exists():
            return output
        with self._lock:
            if self._status.state == "stopped":
                self._status.detail = f"Removing {len(cuts)} detected ad segment(s)"
        command = build_prepare_command(downloaded.audio_path, output, cuts=cuts, normalize=True, bitrate="192k")
        subprocess.run(command, check=True, capture_output=True, timeout=3600)
        return output

    def _play_episode(
        self,
        downloaded: DownloadedEpisode,
        processed: Path,
        plan: DeepSeekPlan,
    ) -> bool:
        episode_id = downloaded.episode.id
        duration = self._duration(processed)
        boundaries = [0.0]
        if self.store.settings.music_placement == "ads":
            placements = (cut.start_sec for cut in plan.ad_cuts)
        else:
            placements = (insertion.after_sec for insertion in plan.music_insertions)
        for placement in placements:
            cleaned = _clean_time(placement, plan.ad_cuts)
            if 60 < cleaned < duration - 60:
                boundaries.append(cleaned)
        boundaries.append(duration)
        boundaries = sorted(set(round(value, 3) for value in boundaries))
        with self._lock:
            self._status.podcast = downloaded.episode.title
        settings = self.store.settings
        if settings.podcast_checkpoint_episode_id == episode_id:
            resume_position = min(
                duration,
                max(0.0, settings.podcast_checkpoint_position_sec),
            )
        else:
            resume_position = 0.0
            self._checkpoint_podcast(episode_id, resume_position, force=True)
        while not self._stop.is_set():
            for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
                if end <= resume_position + 0.001:
                    continue
                if self._stop.is_set() or self._play_now_requested.is_set():
                    return False
                self._wait_while_paused()
                self._play_podcast_segment(
                    processed,
                    max(start, resume_position),
                    end,
                    downloaded.episode.title,
                    episode_id,
                )
                if self._stop.is_set():
                    return False
                if self._play_now_requested.is_set():
                    self._play_now_requested.clear()
                    return False
                if self._restart_podcast.is_set():
                    break
                resume_position = end
                if index < len(boundaries) - 2:
                    self._play_music_break()
                    if self._play_now_requested.is_set():
                        self._play_now_requested.clear()
                        return False
                    if self._restart_podcast.is_set():
                        break
            if self._restart_podcast.is_set():
                self._restart_podcast.clear()
                resume_position = 0.0
                continue
            return not self._stop.is_set()
        return False

    def _play_podcast_segment(
        self,
        path: Path,
        start: float,
        end: float,
        title: str,
        episode_id: str,
    ) -> None:
        with self._lock:
            self._status.mode = "podcast"
            self._status.now_playing = title
            self._status.detail = "Podcast segment"
        self._pause_spotify()
        decoder = subprocess.Popen(
            (
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(path),
                "-t",
                f"{end - start:.3f}",
                "-f",
                "s16le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                str(self.channels),
                "pipe:1",
            ),
            stdout=subprocess.PIPE,
        )
        self._active_decoder = decoder
        self._pcm_source_active.set()
        position = start

        def checkpoint(written_bytes: int) -> None:
            nonlocal position
            position = min(
                end,
                start
                + written_bytes
                / (self.sample_rate * self.channels * self.sample_width),
            )
            if not self._restart_podcast.is_set():
                self._checkpoint_podcast(episode_id, position)

        try:
            assert decoder.stdout is not None
            self._copy_pcm(decoder.stdout, checkpoint)
            if (
                decoder.wait(timeout=30) != 0
                and not self._stop.is_set()
                and not self._restart_podcast.is_set()
                and not self._play_now_requested.is_set()
            ):
                raise RadioError(f"podcast decoder failed for {path.name}")
            if (
                not self._stop.is_set()
                and not self._restart_podcast.is_set()
                and not self._play_now_requested.is_set()
            ):
                position = end
        finally:
            if not self._restart_podcast.is_set():
                self._checkpoint_podcast(episode_id, position, force=True)
            self._pcm_source_active.clear()
            self._active_decoder = None

    def _play_music_break(self) -> None:
        self._wait_while_paused()
        self._wait_for_device()
        settings = self.store.settings
        songs_per_break = settings.songs_per_break
        self._spotify_audio_ready.clear()
        self._spotify_audio_enabled.set()
        if settings.seed_track_uri:
            self.spotify_desktop.play_track_radio(
                self.spotify_device_name,
                settings.seed_track_uri,
                search_query=settings.seed_track_name,
            )
            source_uri = settings.seed_track_uri
            source_name = settings.seed_track_name or "Spotify radio"
        else:
            playlists = self.spotify_desktop.playlists()
            if not playlists:
                raise RadioError("Spotify account has no saved playlists")
            playlist = next((item for item in playlists if item.uri == settings.selected_playlist_uri), None)
            playlist = playlist or random.choice(playlists)
            self.spotify_desktop.play_context(self.spotify_device_name, playlist.uri, shuffle=True)
            source_uri = playlist.uri
            source_name = playlist.name
        settings.active_music_source_uri = source_uri
        settings.active_music_source_name = source_name
        self.store.save()
        if not self._spotify_audio_ready.wait(timeout=15):
            raise RadioError("Spotify started without producing audio")
        with self._lock:
            self._status.mode = "music"
            self._status.detail = f"{songs_per_break}-song music break"
            self._status.now_playing = source_name
        completed_songs = 0
        current_track_key: tuple[str, tuple[str, ...]] | None = None
        last_poll = 0.0
        deadline = time.monotonic() + 30 * 60
        spotifyd = self._spotifyd
        if spotifyd is None:
            raise RadioError("Spotify audio source is unavailable")
        try:
            while (
                not self._stop.is_set()
                and not self._restart_podcast.is_set()
                and not self._play_now_requested.is_set()
                and time.monotonic() < deadline
            ):
                if spotifyd.poll() is not None:
                    raise RadioError("Spotify audio source stopped")
                time.sleep(0.1)
                if self._playback_paused.is_set():
                    deadline += 0.1
                    continue
                now = time.monotonic()
                if now - last_poll < 1.0:
                    continue
                last_poll = now
                try:
                    playback = self.spotify_desktop.current_playback()
                except Exception:
                    continue
                if playback.track and playback.is_playing:
                    track_key = (
                        playback.track.name.strip().casefold(),
                        tuple(artist.strip().casefold() for artist in playback.track.artists),
                    )
                    with self._lock:
                        artists = ", ".join(playback.track.artists)
                        self._status.now_playing = f"{playback.track.name} — {artists}"
                    if track_key != current_track_key:
                        if current_track_key is not None:
                            completed_songs += 1
                            if completed_songs >= songs_per_break:
                                break
                        current_track_key = track_key
            if (
                completed_songs < songs_per_break
                and not self._stop.is_set()
                and not self._restart_podcast.is_set()
                and not self._play_now_requested.is_set()
            ):
                raise RadioError(
                    f"Spotify did not complete {songs_per_break} songs before the music-break timeout"
                )
        finally:
            self._spotify_audio_enabled.clear()
            self._pcm_source_active.clear()
            self._pause_spotify()

    def _wait_while_paused(self) -> None:
        while self._playback_paused.is_set() and not self._stop.is_set():
            time.sleep(0.1)

    def _wait_for_device(self) -> None:
        deadline = time.monotonic() + 45
        last_error: Exception | None = None
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                self.spotify_desktop.activate_device(self.spotify_device_name)
                return
            except Exception as exc:
                last_error = exc
                time.sleep(2)
        raise RadioError(f"Spotify device did not become available: {last_error}")

    def _copy_pcm(
        self,
        source: BinaryIO,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        pending = b""
        written_bytes = 0
        while not self._stop.is_set():
            chunk = source.read(16_384)
            if not chunk:
                break
            complete = pending + chunk
            pending = self._write_complete_frames(complete)
            written_bytes += len(complete) - len(pending)
            if on_progress is not None:
                on_progress(written_bytes)

    def _write_complete_frames(self, chunk: bytes) -> bytes:
        frame_size = self.channels * self.sample_width
        complete_bytes = len(chunk) - len(chunk) % frame_size
        if complete_bytes:
            self._write_pcm(chunk[:complete_bytes])
        return chunk[complete_bytes:]

    def _write_pcm(self, chunk: bytes) -> None:
        if len(chunk) % (self.channels * self.sample_width):
            raise RadioError("PCM write is not aligned to a complete audio frame")
        encoder = self._encoder
        if not encoder or not encoder.stdin:
            raise RadioError("stream encoder is unavailable")
        with self._pcm_lock:
            encoder.stdin.write(chunk)
            encoder.stdin.flush()
            self._last_pcm_write = time.monotonic()

    def _drain_spotifyd_audio(self) -> None:
        frame_size = self.channels * self.sample_width
        pending = b""
        descriptor = os.open(self.spotifyd_audio_pipe, os.O_RDONLY | os.O_NONBLOCK)
        try:
            while not self._stop.is_set():
                try:
                    chunk = os.read(descriptor, 16_384)
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                if not chunk:
                    pending = b""
                    time.sleep(0.01)
                    continue
                buffered = pending + chunk
                complete_bytes = len(buffered) - len(buffered) % frame_size
                if complete_bytes and self._spotify_audio_enabled.is_set():
                    self._pcm_source_active.set()
                    self._write_pcm(buffered[:complete_bytes])
                    self._spotify_audio_ready.set()
        finally:
            os.close(descriptor)

    def _keep_stream_alive(self) -> None:
        silence = bytes(self.sample_rate * self.channels * self.sample_width // 10)
        while not self._stop.is_set():
            encoder = self._encoder
            if encoder is None or encoder.poll() is not None:
                return
            with self._pcm_lock:
                if time.monotonic() - self._last_pcm_write < 0.1:
                    pass
                else:
                    try:
                        assert encoder.stdin is not None
                        encoder.stdin.write(silence)
                        encoder.stdin.flush()
                        self._last_pcm_write = time.monotonic()
                    except (OSError, RadioError):
                        return
            time.sleep(0.02)

    def _checkpoint_podcast(
        self,
        episode_id: str,
        position_sec: float,
        *,
        force: bool = False,
    ) -> None:
        position_sec = max(0.0, position_sec)
        with self._lock:
            settings = self.store.settings
            changed_episode = settings.podcast_checkpoint_episode_id != episode_id
            settings.podcast_checkpoint_episode_id = episode_id
            settings.podcast_checkpoint_position_sec = position_sec
            should_save = (
                force
                or changed_episode
                or abs(position_sec - self._persisted_podcast_checkpoint_sec) >= 5.0
            )
        if should_save:
            self.store.save()
            self._persisted_podcast_checkpoint_sec = position_sec


    def _pause_spotify(self) -> None:
        try:
            self.spotify_desktop.pause()
        except Exception:
            pass

    def _consume_queued_episode(self, episode_id: str) -> None:
        settings = self.store.settings
        if settings.queued_video_id != episode_id:
            return
        settings.queued_video_id = None
        settings.queued_video_title = None
        self.store.save()

    def _mark_played(self, episode_id: str) -> None:
        settings = self.store.settings
        if settings.podcast_checkpoint_episode_id == episode_id:
            settings.podcast_checkpoint_episode_id = None
            settings.podcast_checkpoint_position_sec = 0.0
            self._persisted_podcast_checkpoint_sec = 0.0
        if episode_id not in settings.episode_history:
            self._remember_episode_id(episode_id)
        played = settings.played_episode_ids
        if episode_id in played:
            played.remove(episode_id)
        played.append(episode_id)
        dropped = played[:-200]
        del played[:-200]
        for dropped_id in dropped:
            settings.episode_history.pop(dropped_id, None)
        completed_channel = str(
            settings.episode_history.get(episode_id, {}).get("source_url") or ""
        )
        if completed_channel in settings.channels:
            settings.last_channel_url = completed_channel
        settings.prepared_episodes = {
            channel: [
                prepared_id for prepared_id in episode_ids if prepared_id != episode_id
            ]
            for channel, episode_ids in settings.prepared_episodes.items()
        }
        if settings.queued_video_id == episode_id:
            settings.queued_video_id = None
            settings.queued_video_title = None
        self.store.save()
        self._enforce_storage_retention()

    def _backfill_episode_history(self) -> None:
        settings = self.store.settings
        changed = False
        for info_path in self.media_dir.glob("*.info.json"):
            episode_id = info_path.name.removesuffix(".info.json")
            if episode_id in settings.episode_history:
                continue
            try:
                settings.episode_history[episode_id] = self._history_entry_from_info(info_path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            changed = True
        if changed:
            self.store.save()

    def _history_entry_from_info(
        self,
        info_path: Path,
        source_url: str | None = None,
    ) -> dict[str, object]:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        episode_id = str(info.get("id") or info_path.name.removesuffix(".info.json"))
        metadata_sources = [
            str(info.get("uploader_url") or "").rstrip("/"),
            str(info.get("channel_url") or "").rstrip("/"),
        ]
        if not source_url:
            source_url = next(
                (
                    configured
                    for configured in self.store.settings.channels
                    if configured.rstrip("/") in metadata_sources
                ),
                None,
            )
        source_url = source_url or next((value for value in metadata_sources if value), "YouTube")
        return {
            "id": episode_id,
            "title": str(info.get("title") or episode_id),
            "url": str(
                info.get("webpage_url")
                or info.get("original_url")
                or f"https://www.youtube.com/watch?v={episode_id}"
            ),
            "channel": str(info.get("channel") or info.get("uploader") or "YouTube"),
            "source_url": source_url,
            "published_at": float(info.get("timestamp") or 0),
        }

    def _remember_episode(
        self,
        downloaded: DownloadedEpisode,
        source_url: str | None = None,
    ) -> None:
        try:
            entry = self._history_entry_from_info(downloaded.info_path, source_url)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            episode = downloaded.episode
            entry = {
                "id": episode.id,
                "title": episode.title,
                "url": episode.url,
                "channel": episode.channel,
                "source_url": source_url or "YouTube",
                "published_at": 0.0,
            }
        self.store.settings.episode_history[downloaded.episode.id] = entry

    def _remember_episode_id(self, episode_id: str) -> None:
        info_path = self.media_dir / f"{episode_id}.info.json"
        if not info_path.exists():
            return
        try:
            self.store.settings.episode_history[episode_id] = self._history_entry_from_info(
                info_path
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _retained_episode_ids(self) -> set[str]:
        settings = self.store.settings
        retained = {
            episode_id
            for episode_ids in settings.prepared_episodes.values()
            for episode_id in episode_ids
        }
        retained.update(
            episode_id
            for episode_id in (
                settings.queued_video_id,
                settings.pending_video_id,
                settings.podcast_checkpoint_episode_id,
                self._current_episode_id,
            )
            if episode_id
        )
        retained_played: dict[str, int] = {}
        for episode_id in reversed(settings.played_episode_ids):
            source_url = str(
                settings.episode_history.get(episode_id, {}).get("source_url") or "YouTube"
            )
            count = retained_played.get(source_url, 0)
            if count >= settings.played_episodes_per_source:
                continue
            retained.add(episode_id)
            retained_played[source_url] = count + 1
        return retained

    def _enforce_storage_retention(self) -> None:
        settings = self.store.settings
        limit = settings.unplayed_episodes_per_source
        checkpoint_id = settings.podcast_checkpoint_episode_id
        trimmed: dict[str, list[str]] = {}
        for channel, episode_ids in settings.prepared_episodes.items():
            retained_ids = episode_ids[:limit]
            if checkpoint_id in episode_ids and checkpoint_id not in retained_ids:
                retained_ids.append(checkpoint_id)
            trimmed[channel] = retained_ids
        if trimmed != settings.prepared_episodes:
            settings.prepared_episodes = trimmed
            self.store.save()
        retained = self._retained_episode_ids()
        for episode_id in set(settings.episode_history) - retained:
            self._delete_episode_assets(episode_id)

    def _delete_episode_assets_if_unretained(self, episode_id: str) -> None:
        if episode_id not in self._retained_episode_ids():
            self._delete_episode_assets(episode_id)

    def _delete_episode_assets(self, episode_id: str) -> None:
        for directory in (self.media_dir, self.transcript_dir, self.processed_dir):
            if not directory.exists():
                continue
            for path in directory.glob(f"{episode_id}*"):
                if path.is_file():
                    path.unlink(missing_ok=True)

    def _write_spotifyd_config(self) -> None:
        self.spotifyd_config.parent.mkdir(parents=True, exist_ok=True)
        self.spotifyd_config.write_text(
            "[global]\n"
            "backend = \"pipe\"\n"
            f"device = \"{self.spotifyd_audio_pipe}\"\n"
            f"device_name = \"{self.spotify_device_name}\"\n"
            "audio_format = \"S16\"\n"
            "bitrate = 160\n"
            "no_audio_cache = true\n"
            "autoplay = true\n",
            encoding="utf-8",
        )

    def _yt_dlp(self) -> str:
        local = self.root / ".venv/bin/yt-dlp"
        executable = str(local) if local.is_file() else shutil.which("yt-dlp")
        if executable is None:
            raise RadioError("yt-dlp is not installed")
        return executable

    def _yt_dlp_options(self) -> tuple[str, ...]:
        cookies = self.root / "data/state/youtube-cookies.txt"
        node = self.root / "data/state/node/node_modules/node/bin/node"
        options: list[str] = []
        if cookies.is_file():
            options.extend(("--cookies", str(cookies)))
        if node.is_file():
            options.extend(
                (
                    "--no-js-runtimes",
                    "--js-runtimes",
                    f"node:{node}",
                    "--remote-components",
                    "ejs:github",
                )
            )
        return tuple(options)

    @staticmethod
    def _duration(path: Path) -> float:
        result = subprocess.run(
            ("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(json.loads(result.stdout)["format"]["duration"])

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _transcript_chunks(transcript: str, limit: int) -> tuple[str, ...]:
    lines = transcript.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return tuple(chunks)


def _merge_cuts(cuts: list[CutRange]) -> tuple[CutRange, ...]:
    merged: list[CutRange] = []
    for cut in sorted(cuts, key=lambda value: value.start_sec):
        if merged and cut.start_sec <= merged[-1].end_sec + 1:
            previous = merged[-1]
            merged[-1] = CutRange(previous.start_sec, max(previous.end_sec, cut.end_sec), previous.reason + "; " + cut.reason)
        else:
            merged.append(cut)
    return tuple(merged)


def _dedupe_insertions(insertions: list[MusicInsertion]) -> tuple[MusicInsertion, ...]:
    result: list[MusicInsertion] = []
    for insertion in sorted(insertions, key=lambda value: value.after_sec):
        if not result or insertion.after_sec - result[-1].after_sec >= 10 * 60:
            result.append(insertion)
    return tuple(result)


def _clean_time(original: float, cuts: tuple[CutRange, ...]) -> float:
    removed = sum(max(0.0, min(original, cut.end_sec) - cut.start_sec) for cut in cuts if cut.start_sec < original)
    return max(0.0, original - removed)
