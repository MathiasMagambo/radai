from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import AppConfig
from .db import connect, list_new_episodes, migrate, transaction, upsert_episode, upsert_feed
from .rss import fetch_feed, parse_feed_xml
from .scheduler import PodcastSegment, build_time_window_schedule
from .spotify import SpotifyClient
from .spotify_device import ensure_device_ready
from .spotify_playlists import list_music_sources, sync_playlists
from .stream import IcecastClient
from .service import AgentService
from .telegram_bot import LongPollingBot, TelegramBotClient, build_router


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radai-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sync = sub.add_parser("sync")
    sync.add_argument("feed_url", nargs="+")
    sub.add_parser("episodes")
    schedule = sub.add_parser("schedule")
    schedule.add_argument("duration", nargs="+", type=float, help="Podcast segment durations in seconds")
    sub.add_parser("playlists")
    sub.add_parser("check-spotify-device")
    sub.add_parser("check-stream")
    sub.add_parser("bot")
    args = parser.parse_args(argv)

    config = AppConfig.from_env()

    if args.command == "init-db":
        con = _open_db(config)
        print(f"initialized {config.db_path}")
        return 0
    if args.command == "sync":
        con = _open_db(config)
        return _sync(con, args.feed_url)
    if args.command == "episodes":
        con = _open_db(config)
        for row in list_new_episodes(con):
            print(f"{row['id']}: {row['feed_title']} — {row['title']}")
        return 0
    if args.command == "schedule":
        segments = tuple(PodcastSegment(index + 1, Path(f"episode-{index + 1}.mp3"), duration) for index, duration in enumerate(args.duration))
        for window in build_time_window_schedule(segments, podcast_window_sec=config.podcast_window_sec, music_window_sec=config.music_window_sec):
            print(f"{window.kind.value}\t{window.planned_start_sec:.0f}\t{window.planned_end_sec:.0f}\t{window.duration_sec:.0f}")
        return 0
    if args.command == "playlists":
        con = _open_db(config)
        client = _spotify_client(config)
        with transaction(con):
            sync_playlists(con, client)
        for row in list_music_sources(con):
            print(f"{row['id']}: {row['name']} ({row['spotify_uri']})")
        return 0
    if args.command == "check-spotify-device":
        client = _spotify_client(config)
        selection = ensure_device_ready(client, config.spotify_device_name, activate=False)
        print(f"found Spotify device: {selection.device.name} [{selection.device.id}]")
        return 0
    if args.command == "check-stream":
        status = IcecastClient(config.icecast_url, config.icecast_status_url).check_stream()
        if status.reachable:
            print(f"stream reachable: {status.url} {status.content_type or ''}".strip())
            return 0
        print(f"stream unreachable: {status.error}", file=sys.stderr)
        return 1
    if args.command == "bot":
        con = _open_db(config)
        token = config.require_telegram()
        service = AgentService(config, con)
        bot = LongPollingBot(TelegramBotClient(token), build_router(service), config.telegram_allowed_user_ids)
        offset = None
        while True:
            offset = bot.poll_once(offset=offset)
    return 2



def _open_db(config: AppConfig):
    con = connect(config.db_path)
    migrate(con)
    return con


def _sync(con, feed_urls: list[str]) -> int:
    new_count = 0
    with transaction(con):
        for url in feed_urls:
            fetched = fetch_feed(url)
            parsed = parse_feed_xml(fetched.body, url)
            feed_id = upsert_feed(con, parsed.url, parsed.title, fetched.etag, fetched.last_modified)
            for episode in parsed.episodes:
                before = con.execute(
                    "SELECT id FROM podcast_episodes WHERE feed_id=? AND guid=?",
                    (feed_id, episode.stable_key),
                ).fetchone()
                upsert_episode(con, feed_id, episode)
                if before is None:
                    new_count += 1
    print(f"synced {len(feed_urls)} feed(s), {new_count} new episode(s)")
    return 0


def _spotify_client(config: AppConfig) -> SpotifyClient:
    client_id, client_secret, refresh_token = config.require_spotify()
    return SpotifyClient(client_id, client_secret, refresh_token)


if __name__ == "__main__":
    raise SystemExit(main())
