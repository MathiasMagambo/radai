from __future__ import annotations

import html
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class YouTubeError(RuntimeError):
    pass


@dataclass(frozen=True)
class YouTubeEpisode:
    id: str
    title: str
    url: str
    channel: str
    duration_sec: float | None


@dataclass(frozen=True)
class DownloadedEpisode:
    episode: YouTubeEpisode
    audio_path: Path
    transcript_path: Path
    info_path: Path


def validate_channel_url(url: str) -> str:
    value = url.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
    }:
        raise YouTubeError("channel must be a youtube.com URL")
    if not (parsed.path.startswith("/@") or parsed.path.startswith("/channel/") or parsed.path.startswith("/c/")):
        raise YouTubeError("URL must identify a YouTube channel")
    return value
def inspect_video_url(
    url: str,
    *,
    yt_dlp: str = "yt-dlp",
    extra_args: tuple[str, ...] = (),
) -> YouTubeEpisode:
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }:
        raise YouTubeError("video must be a youtube.com or youtu.be URL")
    payload = _run_json(
        (
            yt_dlp,
            *extra_args,
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            value,
        ),
        timeout=120,
    )
    video_id = str(payload.get("id") or "")
    if not video_id:
        raise YouTubeError("YouTube did not return a video id")
    duration = payload.get("duration")
    return YouTubeEpisode(
        id=video_id,
        title=str(payload.get("title") or video_id),
        url=str(payload.get("webpage_url") or value),
        channel=str(payload.get("channel") or payload.get("uploader") or "YouTube"),
        duration_sec=float(duration) if duration is not None else None,
    )


def list_channel_episodes(
    channel_url: str,
    *,
    limit: int = 20,
    yt_dlp: str = "yt-dlp",
    extra_args: tuple[str, ...] = (),
) -> tuple[YouTubeEpisode, ...]:
    channel = validate_channel_url(channel_url)
    command = (
        yt_dlp,
        *extra_args,
        "--flat-playlist",
        "--playlist-end",
        str(limit),
        "--dump-single-json",
        channel + "/videos",
    )
    payload = _run_json(command, timeout=120)
    episodes: list[YouTubeEpisode] = []
    for item in payload.get("entries") or []:
        episode_id = str(item.get("id") or "")
        if not episode_id:
            continue
        duration = item.get("duration")
        episodes.append(
            YouTubeEpisode(
                id=episode_id,
                title=str(item.get("title") or episode_id),
                url=f"https://www.youtube.com/watch?v={episode_id}",
                channel=str(item.get("channel") or item.get("uploader") or payload.get("title") or ""),
                duration_sec=float(duration) if duration is not None else None,
            )
        )
    if not episodes:
        raise YouTubeError(f"no podcast episodes found for {channel}")
    return tuple(episodes)


def download_episode(
    episode: YouTubeEpisode,
    media_dir: Path,
    transcript_dir: Path,
    *,
    yt_dlp: str = "yt-dlp",
    extra_args: tuple[str, ...] = (),
    require_transcript: bool = True,
) -> DownloadedEpisode:
    media_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(media_dir / f"{episode.id}.%(ext)s")
    info_path = media_dir / f"{episode.id}.info.json"
    transcript_path = transcript_dir / f"{episode.id}.txt"
    cached_audio = [
        path
        for path in media_dir.glob(f"{episode.id}.*")
        if path.suffix not in {".json", ".vtt", ".part", ".ytdl"} and ".en" not in path.name
    ]
    if info_path.exists() and cached_audio and (transcript_path.exists() or not require_transcript):
        transcript_path.touch(exist_ok=True)
        return DownloadedEpisode(episode, max(cached_audio, key=lambda path: path.stat().st_size), transcript_path, info_path)
    command = (
        yt_dlp,
        *extra_args,
        "--no-playlist",
        "--format",
        "bestaudio/best",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "en.*,en",
        "--sub-format",
        "vtt",
        "--output",
        output_template,
        episode.url,
    )
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=1800)

    candidates = [
        path
        for path in media_dir.glob(f"{episode.id}.*")
        if path.suffix not in {".json", ".vtt", ".part", ".ytdl"} and ".en" not in path.name
    ]
    if not candidates:
        raise YouTubeError(f"yt-dlp did not create audio for {episode.url}")
    audio_path = max(candidates, key=lambda path: path.stat().st_size)
    subtitle_paths = sorted(media_dir.glob(f"{episode.id}*.vtt"))
    if subtitle_paths:
        transcript_path.write_text(
            vtt_to_timestamped_text(subtitle_paths[0].read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    elif require_transcript:
        raise YouTubeError(f"YouTube has no English transcript for {episode.title}")
    else:
        transcript_path.write_text("", encoding="utf-8")
    return DownloadedEpisode(episode, audio_path, transcript_path, info_path)


def vtt_to_timestamped_text(vtt: str) -> str:
    blocks = re.split(r"\n\s*\n", vtt.replace("\r\n", "\n"))
    lines: list[str] = []
    previous_text = ""
    for block in blocks:
        rows = [row.strip() for row in block.splitlines() if row.strip()]
        timing_index = next((index for index, row in enumerate(rows) if "-->" in row), None)
        if timing_index is None:
            continue
        start = rows[timing_index].split("-->", 1)[0].strip()
        text = " ".join(rows[timing_index + 1 :])
        text = re.sub(r"<[^>]+>", "", html.unescape(text)).strip()
        if not text or text == previous_text:
            continue
        lines.append(f"[{start}] {text}")
        previous_text = text
    if not lines:
        raise YouTubeError("downloaded transcript contains no timed speech")
    return "\n".join(lines) + "\n"


def _run_json(command: tuple[str, ...], *, timeout: float) -> dict[str, object]:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise YouTubeError(detail.strip()) from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise YouTubeError("yt-dlp returned invalid metadata") from exc
