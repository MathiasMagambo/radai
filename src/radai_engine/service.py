from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, ConfigError
from .db import create_stream_session, list_new_episodes, transaction, update_session_status, upsert_episode, upsert_feed
from .models import RecordingStatus, SessionStatus
from .rss import fetch_feed, parse_feed_xml
from .spotify import SpotifyClient
from .spotify_device import ensure_device_ready
from .spotify_playlists import find_playlist_source, list_music_sources, sync_playlists
from .stream import IcecastClient, discard_recording, keep_recording, next_recording_path


@dataclass
class EngineState:
    podcast_window_sec: int
    music_window_sec: int
    music_mode: str = "playlist"
    music_source_id: int | None = None
    music_source_name: str | None = None
    selected_episode_query: str | None = None
    latest_session_id: int | None = None
    latest_recording_path: Path | None = None


class EngineService:
    def __init__(self, config: AppConfig, con: sqlite3.Connection, icecast: IcecastClient | None = None) -> None:
        self.config = config
        self.con = con
        self.icecast = icecast or IcecastClient(config.icecast_url, config.icecast_status_url)
        self.state = EngineState(config.podcast_window_sec, config.music_window_sec)

    def sync(self, feed_urls: tuple[str, ...]) -> str:
        if not feed_urls:
            return "Usage: /sync <feed-url> [...]"
        new_count = 0
        with transaction(self.con):
            for url in feed_urls:
                fetched = fetch_feed(url)
                if fetched.not_modified:
                    continue
                parsed = parse_feed_xml(fetched.body, url)
                feed_id = upsert_feed(self.con, parsed.url, parsed.title, fetched.etag, fetched.last_modified)
                for episode in parsed.episodes:
                    before = self.con.execute(
                        "SELECT id FROM podcast_episodes WHERE feed_id=? AND guid=?",
                        (feed_id, episode.stable_key),
                    ).fetchone()
                    upsert_episode(self.con, feed_id, episode)
                    if before is None:
                        new_count += 1
        return f"Synced {len(feed_urls)} feed(s), {new_count} new episode(s)."

    def episodes(self) -> str:
        rows = list_new_episodes(self.con, limit=10)
        if not rows:
            return "No new podcast episodes."
        return "\n".join(f"{row['id']}: {row['feed_title']} — {row['title']}" for row in rows)

    def playlists(self) -> str:
        try:
            client_id, client_secret, refresh_token = self.config.require_spotify()
        except ConfigError:
            rows = list_music_sources(self.con)
        else:
            client = SpotifyClient(client_id, client_secret, refresh_token)
            with transaction(self.con):
                rows = sync_playlists(self.con, client)
            if rows:
                return "\n".join(f"{index + 1}: {playlist.name}" for index, playlist in enumerate(rows))
            rows = list_music_sources(self.con)
        if not rows:
            return "No Spotify playlists synced yet."
        return "\n".join(f"{row['id']}: {row['name']}" for row in rows)

    def set_ratio(self, podcast_minutes: int, music_minutes: int) -> str:
        self.state.podcast_window_sec = podcast_minutes * 60
        self.state.music_window_sec = music_minutes * 60
        return f"Set cadence to {podcast_minutes} minutes podcast / {music_minutes} minutes music."

    def set_music_playlist(self, name: str) -> str:
        row = find_playlist_source(self.con, name)
        if row is None:
            return f"Playlist not found: {name}. Run /playlists after syncing Spotify playlists."
        self.state.music_mode = "playlist"
        self.state.music_source_id = int(row["id"])
        self.state.music_source_name = row["name"]
        return f"Music windows will use playlist: {row['name']}."

    def set_music_radio(self, seed: str) -> str:
        self.state.music_mode = "radio"
        self.state.music_source_id = None
        self.state.music_source_name = seed
        return f"Music windows will use Spotify radio/recommendation seed: {seed}."

    def play(self, query: str) -> str:
        self.state.selected_episode_query = query or "latest"
        return self.start_stream()

    def start_stream(self) -> str:
        status = self.icecast.check_stream()
        if not status.reachable:
            return f"Stream not reachable yet at {self.config.icecast_url}: {status.error}"
        spotify_device = "not configured"
        try:
            client_id, client_secret, refresh_token = self.config.require_spotify()
        except ConfigError:
            pass
        else:
            client = SpotifyClient(client_id, client_secret, refresh_token)
            selection = ensure_device_ready(client, self.config.spotify_device_name)
            spotify_device = selection.device.name
        recording_path = next_recording_path(self.config.recordings_dir, _next_session_number(self.con))
        with transaction(self.con):
            session = create_stream_session(
                self.con,
                self.state.podcast_window_sec,
                self.state.music_window_sec,
                self.state.music_mode,
                self.config.icecast_url,
                recording_path,
            )
            update_session_status(self.con, session.id, SessionStatus.RUNNING, RecordingStatus.RECORDING)
        self.state.latest_session_id = session.id
        self.state.latest_recording_path = recording_path
        return (
            "Stream started.\n"
            f"VLC URL: {self.config.icecast_url}\n"
            f"Cadence: {self.state.podcast_window_sec // 60}:{self.state.music_window_sec // 60}\n"
            f"Episodes: {self.state.selected_episode_query or 'latest'}\n"
            f"Music: {self.state.music_mode} {self.state.music_source_name or ''}\n"
            f"Spotify device: {spotify_device}\n"
            f"Recording: {recording_path}"
        ).strip()

    def stop_stream(self) -> str:
        if self.state.latest_session_id is None:
            return "No active stream session."
        with transaction(self.con):
            update_session_status(self.con, self.state.latest_session_id, SessionStatus.STOPPED, RecordingStatus.PENDING_DECISION)
        return "Stream stopped. Reply /keep or /discard for the latest recording."

    def keep_recording(self) -> str:
        if self.state.latest_recording_path is None:
            return "No recording pending."
        decision = keep_recording(self.state.latest_recording_path)
        if self.state.latest_session_id is not None:
            with transaction(self.con):
                update_session_status(self.con, self.state.latest_session_id, SessionStatus.STOPPED, decision.status)
        return f"Kept recording: {decision.path}"

    def discard_recording(self) -> str:
        if self.state.latest_recording_path is None:
            return "No recording pending."
        decision = discard_recording(self.state.latest_recording_path)
        if self.state.latest_session_id is not None:
            with transaction(self.con):
                update_session_status(self.con, self.state.latest_session_id, SessionStatus.STOPPED, decision.status)
        return f"Discarded recording: {decision.path}"

    def status(self) -> str:
        source = self.state.music_source_name or "not selected"
        session = self.state.latest_session_id or "none"
        return (
            f"Stream URL: {self.config.icecast_url}\n"
            f"Latest session: {session}\n"
            f"Cadence: {self.state.podcast_window_sec // 60}:{self.state.music_window_sec // 60}\n"
            f"Music: {self.state.music_mode} {source}"
        )


def _next_session_number(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM stream_sessions").fetchone()
    return int(row["next_id"] if isinstance(row, sqlite3.Row) else row[0])
