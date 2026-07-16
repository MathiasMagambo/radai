from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import EpisodeStatus, PodcastEpisode, RecordingStatus, SessionStatus, StreamSession, TranscriptRef

SCHEMA_VERSION = 1


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS podcast_feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    etag TEXT,
    last_modified TEXT
);
CREATE TABLE IF NOT EXISTS podcast_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES podcast_feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    audio_url TEXT NOT NULL,
    audio_mime TEXT,
    audio_bytes INTEGER,
    local_audio_path TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    link TEXT,
    UNIQUE(feed_id, guid)
);
CREATE TABLE IF NOT EXISTS episode_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES podcast_episodes(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    mime TEXT NOT NULL,
    local_path TEXT,
    text_path TEXT,
    status TEXT NOT NULL,
    UNIQUE(episode_id, url)
);
CREATE TABLE IF NOT EXISTS deepseek_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES podcast_episodes(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    validated INTEGER NOT NULL DEFAULT 0,
    UNIQUE(episode_id, model, input_hash)
);
CREATE TABLE IF NOT EXISTS music_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    spotify_uri TEXT NOT NULL UNIQUE,
    spotify_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS stream_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    podcast_window_sec INTEGER NOT NULL,
    music_window_sec INTEGER NOT NULL,
    music_mode TEXT NOT NULL,
    icecast_url TEXT NOT NULL,
    recording_path TEXT,
    recording_status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS playback_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    source_id INTEGER REFERENCES music_sources(id),
    episode_id INTEGER REFERENCES podcast_episodes(id),
    planned_start_at REAL NOT NULL,
    planned_end_at REAL NOT NULL,
    actual_start_at REAL,
    actual_end_at REAL,
    status TEXT NOT NULL DEFAULT 'planned'
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise


def migrate(con: sqlite3.Connection) -> None:
    with transaction(con):
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
            (SCHEMA_VERSION,),
        )


def upsert_feed(con: sqlite3.Connection, url: str, title: str, etag: str | None = None, last_modified: str | None = None) -> int:
    con.execute(
        """
        INSERT INTO podcast_feeds(url, title, etag, last_modified, last_checked_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title,
            etag=COALESCE(excluded.etag, podcast_feeds.etag),
            last_modified=COALESCE(excluded.last_modified, podcast_feeds.last_modified),
            last_checked_at=CURRENT_TIMESTAMP
        """,
        (url, title, etag, last_modified),
    )
    row = con.execute("SELECT id FROM podcast_feeds WHERE url = ?", (url,)).fetchone()
    return int(row["id"])


def upsert_episode(con: sqlite3.Connection, feed_id: int, episode: PodcastEpisode) -> int:
    con.execute(
        """
        INSERT INTO podcast_episodes(
            feed_id, guid, title, description, published_at, audio_url, audio_mime,
            audio_bytes, local_audio_path, status, link
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(feed_id, guid) DO UPDATE SET
            title=excluded.title,
            description=excluded.description,
            published_at=excluded.published_at,
            audio_url=excluded.audio_url,
            audio_mime=excluded.audio_mime,
            audio_bytes=excluded.audio_bytes,
            link=excluded.link
        """,
        (
            feed_id,
            episode.stable_key,
            episode.title,
            episode.description,
            episode.published_at,
            episode.audio_url,
            episode.audio_mime,
            episode.audio_bytes,
            str(episode.local_audio_path) if episode.local_audio_path else None,
            episode.status.value,
            episode.link,
        ),
    )
    row = con.execute(
        "SELECT id FROM podcast_episodes WHERE feed_id = ? AND guid = ?",
        (feed_id, episode.stable_key),
    ).fetchone()
    episode_id = int(row["id"])
    for transcript in episode.transcripts:
        upsert_transcript_ref(con, episode_id, transcript)
    return episode_id


def upsert_transcript_ref(con: sqlite3.Connection, episode_id: int, transcript: TranscriptRef) -> int:
    con.execute(
        """
        INSERT INTO episode_transcripts(episode_id, source, url, mime, status)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(episode_id, url) DO UPDATE SET
            source=excluded.source,
            mime=excluded.mime
        """,
        (episode_id, transcript.source, transcript.url, transcript.mime, "found"),
    )
    row = con.execute(
        "SELECT id FROM episode_transcripts WHERE episode_id = ? AND url = ?",
        (episode_id, transcript.url),
    ).fetchone()
    return int(row["id"])


def store_deepseek_plan(con: sqlite3.Connection, episode_id: int, model: str, input_hash: str, plan_json: dict, validated: bool) -> int:
    payload = json.dumps(plan_json, sort_keys=True, separators=(",", ":"))
    con.execute(
        """
        INSERT INTO deepseek_plans(episode_id, model, input_hash, plan_json, validated)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(episode_id, model, input_hash) DO UPDATE SET
            plan_json=excluded.plan_json,
            validated=excluded.validated,
            created_at=CURRENT_TIMESTAMP
        """,
        (episode_id, model, input_hash, payload, 1 if validated else 0),
    )
    row = con.execute(
        "SELECT id FROM deepseek_plans WHERE episode_id=? AND model=? AND input_hash=?",
        (episode_id, model, input_hash),
    ).fetchone()
    return int(row["id"])


def list_new_episodes(con: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        con.execute(
            """
            SELECT e.*, f.title AS feed_title, f.url AS feed_url
            FROM podcast_episodes e
            JOIN podcast_feeds f ON f.id = e.feed_id
            WHERE e.status = ?
            ORDER BY COALESCE(e.published_at, '') DESC, e.id DESC
            LIMIT ?
            """,
            (EpisodeStatus.NEW.value, limit),
        )
    )


def create_stream_session(
    con: sqlite3.Connection,
    podcast_window_sec: int,
    music_window_sec: int,
    music_mode: str,
    icecast_url: str,
    recording_path: Path | None,
) -> StreamSession:
    con.execute(
        """
        INSERT INTO stream_sessions(status, podcast_window_sec, music_window_sec, music_mode, icecast_url, recording_path, recording_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SessionStatus.PLANNED.value,
            podcast_window_sec,
            music_window_sec,
            music_mode,
            icecast_url,
            str(recording_path) if recording_path else None,
            RecordingStatus.NONE.value if recording_path is None else RecordingStatus.RECORDING.value,
        ),
    )
    row = con.execute("SELECT * FROM stream_sessions WHERE id = last_insert_rowid()").fetchone()
    return _session_from_row(row)


def update_session_status(con: sqlite3.Connection, session_id: int, status: SessionStatus, recording_status: RecordingStatus | None = None) -> None:
    if recording_status is None:
        con.execute(
            "UPDATE stream_sessions SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status.value, session_id),
        )
    else:
        con.execute(
            "UPDATE stream_sessions SET status=?, recording_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status.value, recording_status.value, session_id),
        )


def _session_from_row(row: sqlite3.Row) -> StreamSession:
    return StreamSession(
        id=int(row["id"]),
        status=SessionStatus(row["status"]),
        podcast_window_sec=int(row["podcast_window_sec"]),
        music_window_sec=int(row["music_window_sec"]),
        music_mode=row["music_mode"],
        icecast_url=row["icecast_url"],
        recording_path=Path(row["recording_path"]) if row["recording_path"] else None,
        recording_status=RecordingStatus(row["recording_status"]),
    )
