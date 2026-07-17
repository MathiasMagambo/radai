from __future__ import annotations

import base64
from collections import deque
import json
import hashlib
import http.cookies
import html
import mimetypes
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from dotenv import load_dotenv

from .deepseek import DeepSeekClient
from .radio_engine import RadioEngine, RadioStatus, StateStore
from .spotify_desktop import SpotifyDesktopController


def _mp3_frame_length(data: bytes | bytearray, offset: int) -> int | None:
    if offset + 4 > len(data) or data[offset] != 0xFF or data[offset + 1] & 0xE0 != 0xE0:
        return None
    version = (data[offset + 1] >> 3) & 0x03
    layer = (data[offset + 1] >> 1) & 0x03
    bitrate_index = data[offset + 2] >> 4
    sample_index = (data[offset + 2] >> 2) & 0x03
    if version == 1 or layer != 1 or bitrate_index in {0, 15} or sample_index == 3:
        return None
    if version == 3:
        bitrates = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        sample_rates = (44_100, 48_000, 32_000)
        scale = 144
    else:
        bitrates = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
        sample_rates = (22_050, 24_000, 16_000) if version == 2 else (11_025, 12_000, 8_000)
        scale = 72
    padding = (data[offset + 2] >> 1) & 0x01
    return scale * bitrates[bitrate_index] * 1_000 // sample_rates[sample_index] + padding


def _mp3_frame_start(data: bytes | bytearray, offset: int) -> int:
    for candidate in range(max(offset, 0), len(data) - 4):
        frame_length = _mp3_frame_length(data, candidate)
        if frame_length is None:
            continue
        next_frame = candidate + frame_length
        if next_frame + 4 <= len(data) and _mp3_frame_length(data, next_frame) is not None:
            return candidate
    return offset


class BufferedAudioStream:
    def __init__(
        self,
        upstream_url: str,
        status_source: Callable[[], RadioStatus],
        source_paused: Callable[[], bool] = lambda: False,
        *,
        lag_bytes: int = 240_000,
        on_idle: Callable[[], None] = lambda: None,
        idle_timeout: float = 2.0,
    ) -> None:
        self.upstream_url = upstream_url
        self.status_source = status_source
        self.source_paused = source_paused
        self.lag_bytes = lag_bytes
        self.capacity = max(lag_bytes * 4, 33_554_432)
        self._condition = threading.Condition()
        self._data = bytearray()
        self._base = 0
        self._generation = 0
        self._status_history: deque[tuple[int, RadioStatus]] = deque()
        self._on_idle = on_idle
        self._idle_timeout = idle_timeout
        self._listeners = 0
        self._idle_generation = 0
        threading.Thread(target=self._run, name="radai-stream-buffer", daemon=True).start()
    @property
    def delay_seconds(self) -> float:
        return self.lag_bytes / (128_000 / 8)

    def _run(self) -> None:
        while True:
            try:
                with urllib.request.urlopen(self.upstream_url, timeout=30) as source:
                    source_was_paused = False
                    pause_completion_bytes = 0
                    with self._condition:
                        self._record_status_locked(self._base + len(self._data), self.status_source())
                    while chunk := source.read(4_096):
                        if self.source_paused():
                            if not source_was_paused:
                                with self._condition:
                                    pause_completion_bytes = self._mp3_frame_completion_bytes_locked()
                                source_was_paused = True
                            if pause_completion_bytes:
                                keep = min(pause_completion_bytes, len(chunk))
                                with self._condition:
                                    self._data.extend(chunk[:keep])
                                    self._condition.notify_all()
                                pause_completion_bytes -= keep
                            continue
                        if source_was_paused:
                            frame_start = _mp3_frame_start(chunk, 0)
                            frame_length = _mp3_frame_length(chunk, frame_start)
                            if frame_length is None or _mp3_frame_length(chunk, frame_start + frame_length) is None:
                                continue
                            chunk = chunk[frame_start:]
                            source_was_paused = False
                        status = self.status_source()
                        with self._condition:
                            self._data.extend(chunk)
                            self._record_status_locked(self._base + len(self._data), status)
                            if len(self._data) > self.capacity:
                                trim = len(self._data) - self.capacity
                                del self._data[:trim]
                                self._base += trim
                                while (
                                    len(self._status_history) > 1
                                    and self._status_history[1][0] <= self._base
                                ):
                                    self._status_history.popleft()
                            self._condition.notify_all()
            except Exception:
                pass
            with self._condition:
                self._data.clear()
                self._status_history.clear()
                self._base = 0
                self._generation += 1
                self._condition.notify_all()
            time.sleep(1)

    def _mp3_frame_completion_bytes_locked(self) -> int:
        if len(self._data) < 8:
            return 0
        cursor = _mp3_frame_start(self._data, max(0, len(self._data) - 8_192))
        while (frame_length := _mp3_frame_length(self._data, cursor)) is not None:
            frame_end = cursor + frame_length
            if frame_end >= len(self._data):
                return frame_end - len(self._data)
            cursor = frame_end
        return 0

    def _record_status_locked(self, position: int, status: RadioStatus) -> None:
        if not self._status_history or self._status_history[-1][1] != status:
            self._status_history.append((position, status))

    def playback_status(self) -> RadioStatus:
        current = self.status_source()
        if current.state not in {"running", "paused"}:
            return current
        with self._condition:
            target = self._base + max(0, len(self._data) - self.lag_bytes)
            delayed = next(
                (
                    status
                    for position, status in reversed(self._status_history)
                    if position <= target
                ),
                current,
            )
        return RadioStatus(
            state=current.state,
            detail=delayed.detail,
            mode=delayed.mode,
            now_playing=delayed.now_playing,
            podcast=delayed.podcast,
            started_at=current.started_at,
            error=current.error,
            preparation_error=current.preparation_error,
        )

    def _listener_started(self) -> None:
        with self._condition:
            self._listeners += 1
            self._idle_generation += 1

    def _listener_stopped(self) -> None:
        with self._condition:
            self._listeners = max(0, self._listeners - 1)
            if self._listeners:
                return
            self._idle_generation += 1
            generation = self._idle_generation
        threading.Thread(
            target=self._notify_idle_after_delay,
            args=(generation,),
            name="radai-stream-idle",
            daemon=True,
        ).start()

    def _notify_idle_after_delay(self, generation: int) -> None:
        time.sleep(self._idle_timeout)
        with self._condition:
            if self._listeners or generation != self._idle_generation:
                return
        self._on_idle()

    def serve(self, handler: BaseHTTPRequestHandler) -> None:
        self._listener_started()
        try:
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "audio/mpeg")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Accel-Buffering", "no")
            handler.end_headers()
            cursor: int | None = None
            generation = -1
            while True:
                with self._condition:
                    while len(self._data) < self.lag_bytes:
                        self._condition.wait(timeout=1)
                    if cursor is None or generation != self._generation:
                        generation = self._generation
                        cursor = self._aligned_cursor_locked(
                            self._base + len(self._data) - self.lag_bytes
                        )
                    while cursor >= self._base + len(self._data):
                        self._condition.wait(timeout=1)
                        if generation != self._generation:
                            cursor = None
                            break
                    if cursor is None:
                        continue
                    if cursor < self._base:
                        cursor = self._aligned_cursor_locked(self._base)
                    offset = cursor - self._base
                    chunk = bytes(self._data[offset : offset + 65_536])
                    cursor += len(chunk)
                handler.wfile.write(chunk)
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self._listener_stopped()

    def _aligned_cursor_locked(self, absolute_offset: int) -> int:
        relative_offset = max(0, absolute_offset - self._base)
        return self._base + _mp3_frame_start(self._data, relative_offset)


def _render_html_template(content: str, branding: dict[str, str]) -> bytes:
    for name, value in branding.items():
        content = content.replace(f"{{{{{name}}}}}", html.escape(value, quote=True))
    return content.encode("utf-8")


def _path_from_env(root: Path, name: str, default: str) -> Path:
    path = Path(os.environ.get(name, default)).expanduser()
    return path if path.is_absolute() else root / path


class RadioApplication:
    def __init__(self, root: Path) -> None:
        self.root = root
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not deepseek_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required")
        icecast_password = os.environ.get("ICECAST_SOURCE_PASSWORD", "").strip()
        if not icecast_password:
            raise RuntimeError("ICECAST_SOURCE_PASSWORD is required")
        self.username = os.environ.get("RADIO_USERNAME", "radio")
        self.password = os.environ.get("RADIO_PASSWORD", "")
        if not self.password:
            raise RuntimeError("RADIO_PASSWORD is required")
        self.site_name = os.environ.get("SITE_NAME", "RADAI")
        self.site_title = os.environ.get("SITE_TITLE", "Radai Radio")
        self.branding = {
            "SITE_NAME": self.site_name,
            "SITE_TITLE": self.site_title,
            "SITE_TAGLINE": os.environ.get("SITE_TAGLINE", "PRIVATE INTERNET RADIO"),
            "SITE_FOOTER": os.environ.get("SITE_FOOTER", "RADIO.EXAMPLE.COM"),
            "SITE_MARK": os.environ.get("SITE_MARK", self.site_name[:1] or "R"),
            "SITE_LOGIN_INTRO": os.environ.get(
                "SITE_LOGIN_INTRO",
                "Sign in to manage the station and listen to the private stream.",
            ),
        }
        cdp_url = os.environ.get("SPOTIFY_CDP_URL", "http://127.0.0.1:9223")
        self.spotify_desktop = SpotifyDesktopController(cdp_url=cdp_url)
        self.store = StateStore(root / "data/state/radio.json")
        icecast_port = int(os.environ.get("ICECAST_PORT", "8001"))
        self.engine = RadioEngine(
            root=root,
            spotify_desktop=self.spotify_desktop,
            deepseek=DeepSeekClient(
                deepseek_key,
                model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            ),
            state_store=self.store,
            icecast_password=icecast_password,
            icecast_port=icecast_port,
            spotifyd_path=_path_from_env(
                root,
                "SPOTIFYD_PATH",
                str(Path.home() / ".local/bin/spotifyd"),
            ),
            spotify_device_name=os.environ.get("SPOTIFY_DEVICE_NAME", "Radai Radio"),
        )
        self.session_token = hashlib.sha256(
            f"{self.username}\0{self.password}".encode("utf-8")
        ).hexdigest()
        self.static_dir = _path_from_env(root, "RADIO_WEB_DIR", "web").resolve()
        self.engine.prepare_in_background()
        self.stream_buffer = BufferedAudioStream(
            f"http://127.0.0.1:{icecast_port}/spotify.mp3",
            self.engine.status,
            self.engine.source_paused,
            on_idle=lambda: self.engine.pause(delay_sec=0),
        )
    def settings_payload(self) -> dict[str, object]:
        settings = self.store.settings
        return {
            "playback_mode": settings.playback_mode,
            "music_placement": settings.music_placement,
            "songs_per_break": settings.songs_per_break,
            "podcast_selector_enabled": settings.podcast_selector_enabled,
            "restart_current_podcast_enabled": settings.restart_current_podcast_enabled,
            "unplayed_episodes_per_source": settings.unplayed_episodes_per_source,
            "played_episodes_per_source": settings.played_episodes_per_source,
            "seed_track_uri": settings.seed_track_uri,
            "seed_track_name": settings.seed_track_name,
            "selected_playlist_uri": settings.selected_playlist_uri,
            "selected_playlist_name": settings.selected_playlist_name,
            "active_music_source_uri": settings.active_music_source_uri,
            "active_music_source_name": settings.active_music_source_name,
            "queued_video_id": settings.queued_video_id,
            "queued_video_title": settings.queued_video_title,
            "pending_video_id": settings.pending_video_id,
            "pending_video_title": settings.pending_video_title,
        }


class RadioHandler(BaseHTTPRequestHandler):
    server_version = "RadaiRadio/1.0"

    @property
    def app(self) -> RadioApplication:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/login", "/login.js", "/style.css"}:
            if parsed.path == "/login" and self._authorized(challenge=False):
                self._redirect("/")
                return
            self._static("/login.html" if parsed.path == "/login" else parsed.path)
            return
        if not self._authorized(challenge=False):
            if parsed.path in {"", "/"}:
                self._redirect("/login")
            else:
                self._unauthorized()
            return
        if parsed.path == "/buffered-stream.mp3":
            self.app.stream_buffer.serve(self)
            return
        if parsed.path == "/api/status":
            self._json(HTTPStatus.OK, {"status": asdict(self.app.stream_buffer.playback_status())})
            return
        if parsed.path == "/api/channels":
            self._json(HTTPStatus.OK, {"channels": self.app.engine.channel_sources()})
            return
        if parsed.path == "/api/playlists":
            try:
                playlists = [asdict(item) for item in self.app.spotify_desktop.playlists()]
                self._json(HTTPStatus.OK, {"playlists": playlists})
            except Exception as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        if parsed.path == "/api/settings":
            self._json(HTTPStatus.OK, {"settings": self.app.settings_payload()})
            return
        if parsed.path == "/api/podcasts":
            self._json(HTTPStatus.OK, {"podcasts": self.app.engine.prepared_podcasts()})
            return
        if parsed.path == "/api/history":
            self._json(HTTPStatus.OK, {"history": self.app.engine.podcast_history()})
            return
        if parsed.path == "/api/search":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            if len(query) < 2:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Search query is too short"})
                return
            try:
                tracks = [asdict(item) for item in self.app.spotify_desktop.search_tracks(query)]
                self._json(HTTPStatus.OK, {"tracks": tracks})
            except Exception as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/login":
            body = self._body()
            username = str(body.get("username", ""))
            password = str(body.get("password", ""))
            if not self._valid_credentials(username, password):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "Incorrect username or password"})
                return
            self._json(
                HTTPStatus.OK,
                {"authenticated": True},
                headers={
                    "Set-Cookie": (
                        f"radai_session={self.app.session_token}; Path=/; Max-Age=2592000; "
                        "HttpOnly; Secure; SameSite=Strict"
                    )
                },
            )
            return
        if not self._authorized():
            return
        try:
            body = self._body()
            if parsed.path == "/api/start":
                self._json(HTTPStatus.ACCEPTED, {"status": asdict(self.app.engine.start())})
                return
            if parsed.path == "/api/stop":
                self._json(HTTPStatus.OK, {"status": asdict(self.app.engine.stop())})
                return
            if parsed.path == "/api/pause":
                self._json(
                    HTTPStatus.OK,
                    {"status": asdict(self.app.engine.pause(delay_sec=self.app.stream_buffer.delay_seconds))},
                )
                return
            if parsed.path == "/api/resume":
                self._json(HTTPStatus.OK, {"status": asdict(self.app.engine.resume())})
                return
            if parsed.path == "/api/restart-podcast":
                self._json(
                    HTTPStatus.OK,
                    {"status": asdict(self.app.engine.restart_current_podcast())},
                )
                return
            if parsed.path == "/api/settings":
                settings = self.app.engine.update_settings(
                    playback_mode=str(body.get("playback_mode", "")),
                    music_placement=str(body.get("music_placement", "")),
                    songs_per_break=int(body.get("songs_per_break", 0)),
                    podcast_selector_enabled=body.get("podcast_selector_enabled") is True,
                    restart_current_podcast_enabled=body.get("restart_current_podcast_enabled") is True,
                    unplayed_episodes_per_source=int(body.get("unplayed_episodes_per_source", 0)),
                    played_episodes_per_source=int(body.get("played_episodes_per_source", 0)),
                )
                self._json(HTTPStatus.OK, {"settings": self.app.settings_payload()})
                return
            if parsed.path == "/api/podcast":
                selected = self.app.engine.queue_prepared_episode(
                    str(body.get("id", "")),
                    str(body.get("action", "queue")),
                )
                self._json(
                    HTTPStatus.OK,
                    {
                        "podcast": selected,
                        "settings": self.app.settings_payload(),
                        "status": asdict(self.app.engine.status()),
                    },
                )
                return
            if parsed.path == "/api/history":
                action = str(body.get("action", ""))
                episode_id = str(body.get("id", ""))
                if action == "play_again":
                    selected = self.app.engine.replay_history_episode(episode_id)
                    self._json(
                        HTTPStatus.OK,
                        {
                            "podcast": selected,
                            "settings": self.app.settings_payload(),
                            "status": asdict(self.app.engine.status()),
                        },
                    )
                    return
                if action == "prepare":
                    selected = self.app.engine.prepare_history_episode(episode_id)
                    self._json(HTTPStatus.ACCEPTED, {"podcast": selected})
                    return
                raise ValueError("History action must be play_again or prepare")
            if parsed.path == "/api/video":
                self.app.engine.queue_video(str(body.get("url", "")))
                self._json(HTTPStatus.ACCEPTED, {"preparing": True})
                return
            if parsed.path == "/api/video/action":
                settings = self.app.engine.resolve_prepared_video(str(body.get("action", "")))
                self._json(HTTPStatus.OK, {"settings": self.app.settings_payload()})
                return
            if parsed.path == "/api/channels":
                channel = self.app.store.add_channel(str(body.get("url", "")))
                self.app.engine.prepare_in_background()
                self._json(HTTPStatus.CREATED, {"channel": channel})
                return
            if parsed.path == "/api/channels/remove":
                self.app.store.remove_channel(str(body.get("url", "")))
                self._json(HTTPStatus.OK, {"removed": True})
                return
            if parsed.path == "/api/playlist":
                self.app.engine.set_playlist(
                    str(body.get("uri", "")) or None,
                    str(body.get("name", "")) or None,
                )
                self._json(HTTPStatus.OK, {"settings": self.app.settings_payload()})
                return
            if parsed.path == "/api/radio-track":
                uri = str(body.get("uri", ""))
                name = str(body.get("name", ""))
                if not uri.startswith("spotify:track:"):
                    raise ValueError("A Spotify track URI is required")
                self.app.engine.set_radio_track(uri, name)
                self.app.engine.start()
                self._json(HTTPStatus.OK, {"selected": name, "settings": self.app.settings_payload()})
                return
        except (ValueError, RuntimeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
    def _valid_credentials(self, username: str, password: str) -> bool:
        return secrets.compare_digest(username, self.app.username) and secrets.compare_digest(
            password, self.app.password
        )

    def _authorized(self, *, challenge: bool = True) -> bool:
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Basic "):
            try:
                decoded = base64.b64decode(supplied[6:], validate=True).decode("utf-8")
                username, password = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                pass
            else:
                if self._valid_credentials(username, password):
                    return True
        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            pass
        session = cookies.get("radai_session")
        if session and secrets.compare_digest(session.value, self.app.session_token):
            return True
        if challenge:
            self._unauthorized()
        return False

    def _unauthorized(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Radai Radio", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:
            raise ValueError("Request is too large")
        payload = self.rfile.read(length)
        if not payload:
            return {}
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def _json(
        self,
        status: HTTPStatus,
        payload: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        path = (self.app.static_dir / relative).resolve()
        if self.app.static_dir.resolve() not in path.parents or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if path.suffix == ".html":
            content = _render_html_template(path.read_text(encoding="utf-8"), self.app.branding)
        else:
            content = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


class RadioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: RadioApplication) -> None:
        self.app = app
        super().__init__(address, RadioHandler)


def main() -> None:
    working_root = Path.cwd().resolve()
    load_dotenv(working_root / ".env")
    root = Path(os.environ.get("RADAI_ROOT", working_root)).resolve()
    if root != working_root:
        load_dotenv(root / ".env", override=False)
    host = os.environ.get("RADIO_HOST", "127.0.0.1")
    port = int(os.environ.get("RADIO_PORT", "8090"))
    app = RadioApplication(root)
    server = RadioHTTPServer((host, port), app)
    print(f"{app.site_title} control site listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.engine.stop()
        server.server_close()


if __name__ == "__main__":
    main()
