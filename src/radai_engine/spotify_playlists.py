from __future__ import annotations

import sqlite3

from .spotify import SpotifyClient, SpotifyPlaylist


def sync_playlists(con: sqlite3.Connection, client: SpotifyClient) -> tuple[SpotifyPlaylist, ...]:
    playlists = client.playlists()
    for playlist in playlists:
        con.execute(
            """
            INSERT INTO music_sources(source, name, spotify_uri, spotify_type, enabled)
            VALUES ('spotify', ?, ?, 'playlist', 1)
            ON CONFLICT(spotify_uri) DO UPDATE SET
                name=excluded.name,
                enabled=1
            """,
            (playlist.name, playlist.uri),
        )
    return playlists


def list_music_sources(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        con.execute(
            "SELECT id, source, name, spotify_uri, spotify_type, enabled FROM music_sources WHERE enabled=1 ORDER BY lower(name)"
        )
    )


def find_playlist_source(con: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    lowered = name.lower().strip()
    rows = list_music_sources(con)
    for row in rows:
        if row["name"].lower() == lowered:
            return row
    for row in rows:
        if lowered in row["name"].lower():
            return row
    return None
