from __future__ import annotations

import json
import base64
import os
import time
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from websocket import WebSocketTimeoutException, create_connection

from .spotify import SpotifyPlayback, SpotifyPlaylist, SpotifyTrack


class SpotifyDesktopError(RuntimeError):
    pass


class SpotifyDesktopTokenProvider:
    def __init__(self, token_path: Path, *, cdp_url: str = "http://127.0.0.1:9223") -> None:
        self.token_path = token_path
        self.cdp_url = cdp_url.rstrip("/")


    @staticmethod
    def _valid(token: str) -> bool:
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            expires_at = float(json.loads(base64.urlsafe_b64decode(payload))[ "exp"])
            return expires_at > time.time() + 60
        except (IndexError, KeyError, ValueError, TypeError, json.JSONDecodeError):
            return False
    def __call__(self) -> str:
        if self.token_path.exists():
            token = self.token_path.read_text(encoding="utf-8").strip()
            if token and self._valid(token):
                return token
        return self.capture()

    def capture(self, *, timeout: float = 20.0) -> str:
        target = self._spotify_target()
        websocket_url = target.get("webSocketDebuggerUrl")
        if not isinstance(websocket_url, str):
            raise SpotifyDesktopError("Spotify desktop CDP target has no debugger websocket")

        ws = create_connection(websocket_url, timeout=1, origin=self.cdp_url)
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
            ws.send(json.dumps({"id": 2, "method": "Page.reload", "params": {"ignoreCache": False}}))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    event = json.loads(ws.recv())
                except WebSocketTimeoutException:
                    continue
                if event.get("method") != "Network.requestWillBeSentExtraInfo":
                    continue
                headers = event.get("params", {}).get("headers", {})
                authorization = headers.get("Authorization") or headers.get("authorization")
                if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
                    continue
                token = authorization.removeprefix("Bearer ").strip()
                if token:
                    self._store(token)
                    return token
        finally:
            ws.close()
        raise SpotifyDesktopError("Spotify desktop did not emit an API bearer token")

    def _spotify_target(self) -> dict[str, object]:
        with urllib.request.urlopen(self.cdp_url + "/json", timeout=5) as response:
            targets = json.loads(response.read().decode("utf-8"))
        for target in targets:
            if "xpui.app.spotify.com" in str(target.get("url", "")):
                return target
        raise SpotifyDesktopError("Spotify desktop debugger target is unavailable")

    def _store(self, token: str) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_path.with_suffix(".tmp")
        temporary.write_text(token, encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.token_path)


class SpotifyDesktopController:
    def __init__(self, *, cdp_url: str = "http://127.0.0.1:9223") -> None:
        self.cdp_url = cdp_url.rstrip("/")
        self._lock = threading.RLock()

    def playlists(self) -> tuple[SpotifyPlaylist, ...]:
        items = self._evaluate(
            """
            (async () => {
              const library = document.querySelector('[aria-label="Your Library"]');
              if (!library) throw new Error('Spotify saved library is unavailable');
              const candidates = [library, ...library.querySelectorAll('*')]
                .filter((item) => item.scrollHeight > item.clientHeight + 20);
              const scroller = candidates.sort((a, b) => b.scrollHeight - a.scrollHeight)[0] || library;
              const original = scroller.scrollTop;
              const found = new Map();
              let stableAtBottom = 0;
              for (let attempt = 0; attempt < 200; attempt += 1) {
                for (const title of library.querySelectorAll(
                  '[id^="listrow-title-spotify:playlist:"]'
                )) {
                  const uri = title.id.slice('listrow-title-'.length);
                  const id = uri.slice('spotify:playlist:'.length);
                  const name = (title.textContent || '').trim();
                  if (id && name && !found.has(id)) found.set(id, name);
                }
                const previous = scroller.scrollTop;
                scroller.scrollTop = Math.min(
                  scroller.scrollHeight,
                  previous + Math.max(scroller.clientHeight * 0.8, 240)
                );
                await new Promise((resolve) => setTimeout(resolve, 40));
                const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
                stableAtBottom = atBottom && scroller.scrollTop === previous ? stableAtBottom + 1 : 0;
                if (stableAtBottom >= 2) break;
              }
              scroller.scrollTop = original;
              return Array.from(found, ([id, name]) => ({id, name}));
            })()
            """
        )
        return tuple(
            SpotifyPlaylist(
                id=str(item["id"]),
                name=str(item["name"]),
                uri=f"spotify:playlist:{item['id']}",
                owner="",
                tracks_total=0,
            )
            for item in items
        )

    def search_tracks(self, query: str, *, limit: int = 10) -> tuple[SpotifyTrack, ...]:
        encoded_query = json.dumps(query)
        self._evaluate(
            f"""
            (() => {{
              const input = document.querySelector('[data-testid="search-input"]');
              if (!input) throw new Error('Spotify search input is unavailable');
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
              setter.call(input, {encoded_query});
              input.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: {encoded_query}}}));
              input.focus();
              input.dispatchEvent(new KeyboardEvent('keydown', {{
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
              }}));
              return true;
            }})()
            """
        )
        time.sleep(2)
        items = self._evaluate(
            f"""
            (() => {{
              const found = new Map();
              for (const link of document.querySelectorAll('a[href^="/track/"]')) {{
                const id = link.getAttribute('href').split('/')[2];
                const name = (link.textContent || '').trim();
                const row = link.closest('[role="row"]') || link.parentElement?.parentElement;
                const artists = row ? Array.from(row.querySelectorAll('a[href^="/artist/"]'), a => (a.textContent || '').trim()).filter(Boolean) : [];
                const album = row?.querySelector('a[href^="/album/"]')?.textContent?.trim() || '';
                if (id && name && !found.has(id)) found.set(id, {{id, name, artists, album}});
              }}
              return Array.from(found.values()).slice(0, {int(limit)});
            }})()
            """
        )
        return tuple(
            SpotifyTrack(
                id=str(item["id"]),
                name=str(item["name"]),
                uri=f"spotify:track:{item['id']}",
                artists=tuple(str(value) for value in item.get("artists", [])),
                album=str(item.get("album", "")),
                duration_ms=0,
            )
            for item in items
        )

    def play_context(
        self,
        device_name: str,
        spotify_uri: str,
        *,
        shuffle: bool = False,
        search_query: str | None = None,
    ) -> None:
        self.activate_device(device_name)
        kind, item_id = _spotify_uri_parts(spotify_uri)
        opened = False
        if kind == "playlist":
            opened = bool(
                self._evaluate(
                    f"""
                    (() => {{
                      const title = document.getElementById(
                        {json.dumps(f"listrow-title-{spotify_uri}")}
                      );
                      const control = title?.closest('[role="row"]')?.querySelector('[role="button"]');
                      if (!control) return false;
                      control.click();
                      return true;
                    }})()
                    """
                )
            )
        if not opened:
            self._navigate(f"/{kind}/{item_id}", search_query=search_query)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            played = self._evaluate(
                f"""
                (() => {{
                  const button = document.querySelector('main [data-testid="play-button"]');
                  if (!button) return false;
                  if ({str(shuffle).lower()}) {{
                    const shuffle = Array.from(document.querySelectorAll('main button')).find(
                      value => (value.getAttribute('aria-label') || '').startsWith('Enable Shuffle')
                    );
                    shuffle?.click();
                  }}
                  button.click();
                  return true;
                }})()
                """
            )
            if played:
                return
            time.sleep(1)
        raise SpotifyDesktopError("Spotify play button is unavailable")

    def play_track(self, device_name: str, spotify_uri: str, *, search_query: str | None = None) -> None:
        if not search_query:
            self.play_context(device_name, spotify_uri)
            return
        self.activate_device(device_name)
        kind, item_id = _spotify_uri_parts(spotify_uri)
        target_path = f"/{kind}/{item_id}"
        self.search_tracks(search_query, limit=10)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            clicked = self._evaluate(
                f"""
                (() => {{
                  const wanted = {json.dumps(target_path)};
                  const target = Array.from(document.querySelectorAll('main a[href]')).find(
                    value => value.getAttribute('href') === wanted
                      && value.closest('[role="row"]')
                  );
                  const row = target?.closest('[role="row"]');
                  const button = Array.from(row?.querySelectorAll('button') || []).find(
                    value => (value.getAttribute('aria-label') || '').startsWith('Play')
                  );
                  if (!button) return false;
                  button.click();
                  return true;
                }})()
                """
            )
            if clicked:
                return
            time.sleep(0.5)
        raise SpotifyDesktopError(f"Spotify could not play {target_path}")

    def _resume_selected_song_radio(self, device_name: str, radio_title: str) -> bool:
        state = self._evaluate(
            f"""
            (() => {{
              const wantedTitle = {json.dumps(radio_title)};
              const wantedDevice = {json.dumps(device_name)};
              const titles = [
                ...Array.from(document.querySelectorAll('main h1')),
                ...Array.from(document.querySelectorAll('a[href*="/playlist/"]')),
              ].map(element => (element.textContent || '').trim());
              const control = document.querySelector('[data-testid="control-button-playpause"]');
              const playing = (control?.getAttribute('aria-label') || '') === 'Pause';
              const deviceActive = Array.from(document.querySelectorAll('button')).some(
                button => {{
                  const label = button.getAttribute('aria-label') || '';
                  const text = (button.textContent || '').trim();
                  return label.includes(`Playing on ${{wantedDevice}}`)
                    || text.includes(`Playing on ${{wantedDevice}}`);
                }}
              );
              return {{
                selected: titles.includes(wantedTitle),
                playing,
                device_active: deviceActive,
              }};
            }})()
            """
        )
        if not isinstance(state, dict) or not state.get("selected"):
            return False
        if state.get("playing") and state.get("device_active"):
            return True
        if not state.get("device_active"):
            self.activate_device(device_name)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.current_playback().is_playing:
                return True
            self.resume()
            time.sleep(0.5)
        raise SpotifyDesktopError(f"Spotify could not resume {radio_title}")


    def play_track_radio(
        self,
        device_name: str,
        spotify_uri: str,
        *,
        search_query: str | None = None,
    ) -> None:
        radio_title = _song_radio_title(search_query)
        if radio_title and self._resume_selected_song_radio(device_name, radio_title):
            return
        self.play_track(device_name, spotify_uri, search_query=search_query)
        kind, item_id = _spotify_uri_parts(spotify_uri)
        if kind != "track":
            raise SpotifyDesktopError("Spotify song radio requires a track URI")
        target_path = f"/track/{item_id}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            opened = self._evaluate(
                f"""
                (() => {{
                  const wanted = {json.dumps(target_path)};
                  const target = Array.from(document.querySelectorAll('main a[href]')).find(
                    value => value.getAttribute('href') === wanted
                      && value.closest('[role="row"]')
                  );
                  const row = target?.closest('[role="row"]');
                  const button = row?.querySelector('[data-testid="more-button"]')
                    || Array.from(row?.querySelectorAll('button') || []).find(
                      value => (value.getAttribute('aria-label') || '').startsWith('More options')
                    );
                  if (!button) return false;
                  button.click();
                  return true;
                }})()
                """
            )
            if opened:
                break
            time.sleep(0.5)
        else:
            raise SpotifyDesktopError(f"Spotify song options are unavailable for {target_path}")

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            action = self._evaluate(
                """
                (() => {
                  const items = Array.from(document.querySelectorAll('[role="menuitem"]'));
                  const radio = items.find(
                    item => (item.textContent || '').trim() === 'Go to song radio'
                  );
                  if (radio) {
                    radio.click();
                    return 'radio';
                  }
                  const more = items.find(
                    item => (item.textContent || '').trim() === 'More'
                  );
                  if (more) {
                    more.click();
                    return 'more';
                  }
                  return '';
                })()
                """
            )
            if action == "radio":
                break
            time.sleep(0.5)
        else:
            raise SpotifyDesktopError("Spotify song radio option is unavailable")

        self.activate_device(device_name)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            started = self._evaluate(
                """
                (() => {
                  const button = document.querySelector('main [data-testid="play-button"]');
                  const label = button?.getAttribute('aria-label') || '';
                  if (!button || !label.includes('Radio')) return false;
                  if (label.startsWith('Play')) button.click();
                  return true;
                })()
                """
            )
            if started:
                time.sleep(2)
                if self.current_playback().is_playing:
                    return
            time.sleep(0.5)
        raise SpotifyDesktopError("Spotify song radio did not start playing")

    def activate_device(self, device_name: str) -> None:
        opened = self._evaluate(
            """
            (() => {
              const button = Array.from(document.querySelectorAll('button')).find(
                value => (value.getAttribute('aria-label') || '') === 'Connect to a device'
              );
              if (!button) throw new Error('Spotify device picker is unavailable');
              setTimeout(() => button.click(), 200);
              return true;
            })()
            """
        )
        if not opened:
            raise SpotifyDesktopError("Spotify device picker did not open")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            clicked = self._evaluate(
                f"""
                (() => {{
                  const wanted = {json.dumps(device_name)};
                  const candidate = Array.from(document.querySelectorAll(
                    '[data-testid="device-picker-row-sidepanel"], button'
                  )).find(element =>
                    (element.textContent || '').includes(wanted)
                    || (element.getAttribute('aria-label') || '').includes(wanted)
                  );
                  if (!candidate) return false;
                  setTimeout(() => candidate.click(), 200);
                  return true;
                }})()
                """
            )
            if clicked:
                time.sleep(1)
                return
            time.sleep(1)
        raise SpotifyDesktopError(f"Spotify device {device_name!r} did not appear")

    def current_playback(self) -> SpotifyPlayback:
        item = self._evaluate(
            """
            (() => {
              const link = document.querySelector('[data-testid="context-item-link"]');
              const artist = document.querySelector('[data-testid="context-item-info-artist"]');
              const control = document.querySelector('[data-testid="control-button-playpause"]');
              if (!link) return {is_playing: false, track: null};
              const href = link.getAttribute('href') || '';
              const id = href.startsWith('/track/') ? href.split('/')[2] : href;
              return {
                is_playing: (control?.getAttribute('aria-label') || '') === 'Pause',
                track: {
                  id,
                  name: (link.textContent || '').trim(),
                  artists: artist ? [(artist.textContent || '').trim()] : []
                }
              };
            })()
            """
        )
        track_data = item.get("track") if isinstance(item, dict) else None
        track = None
        if isinstance(track_data, dict) and track_data.get("id"):
            track = SpotifyTrack(
                id=str(track_data["id"]),
                name=str(track_data.get("name", "")),
                uri=f"spotify:track:{track_data['id']}",
                artists=tuple(str(value) for value in track_data.get("artists", []) if value),
                album="",
                duration_ms=0,
            )
        return SpotifyPlayback(bool(item.get("is_playing")), 0, track)

    def pause(self) -> None:
        self._evaluate(
            """
            (() => {
              const control = document.querySelector('[data-testid="control-button-playpause"]');
              if (control?.getAttribute('aria-label') === 'Pause') control.click();
              return true;
            })()
            """
        )
    def resume(self) -> None:
        self._evaluate(
            """
            (() => {
              const control = document.querySelector('[data-testid="control-button-playpause"]');
              if (control?.getAttribute('aria-label') === 'Play') control.click();
              return true;
            })()
            """
        )

    def _navigate(self, path: str, *, search_query: str | None = None) -> None:
        target_path = f"/{path.lstrip('/')}"
        encoded_path = json.dumps(target_path)
        with self._lock:
            clicked = self._evaluate(
                f"(() => {{ const wanted = {encoded_path}; "
                "const link = Array.from(document.querySelectorAll('a[href]')).find("
                "value => value.getAttribute('href') === wanted); "
                "if (!link) return false; link.click(); return true; })()"
            )
            if not clicked:
                item_id = target_path.rsplit("/", 1)[-1]
                self.search_tracks(search_query or item_id, limit=10)
                clicked = self._evaluate(
                    f"(() => {{ const wanted = {encoded_path}; "
                    "const link = Array.from(document.querySelectorAll('a[href]')).find("
                    "value => value.getAttribute('href') === wanted); "
                    "if (!link) return false; link.click(); return true; })()"
                )
            if not clicked:
                raise SpotifyDesktopError(f"Spotify could not navigate to {target_path}")
            time.sleep(1)

    def _evaluate(self, expression: str) -> object:
        with self._lock:
            target = _spotify_target(self.cdp_url)
            websocket_url = target.get("webSocketDebuggerUrl")
            if not isinstance(websocket_url, str):
                raise SpotifyDesktopError("Spotify desktop CDP target has no debugger websocket")
            ws = create_connection(websocket_url, timeout=5, origin=self.cdp_url)
            try:
                ws.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": expression,
                                "awaitPromise": True,
                                "returnByValue": True,
                            },
                        }
                    )
                )
                while True:
                    response = json.loads(ws.recv())
                    if response.get("id") != 1:
                        continue
                    if "error" in response:
                        raise SpotifyDesktopError(str(response["error"]))
                    result = response.get("result", {}).get("result", {})
                    if result.get("subtype") == "error":
                        raise SpotifyDesktopError(str(result.get("description") or result.get("value")))
                    return result.get("value")
            finally:
                ws.close()


def _spotify_target(cdp_url: str) -> dict[str, object]:
    with urllib.request.urlopen(cdp_url.rstrip("/") + "/json", timeout=5) as response:
        targets = json.loads(response.read().decode("utf-8"))
    for target in targets:
        if "xpui.app.spotify.com" in str(target.get("url", "")):
            return target
    raise SpotifyDesktopError("Spotify desktop debugger target is unavailable")


def _song_radio_title(search_query: str | None) -> str | None:
    if not search_query:
        return None
    track_name = search_query.split(" — ", 1)[0].strip()
    return f"{track_name} Radio" if track_name else None


def _spotify_uri_parts(uri: str) -> tuple[str, str]:
    parts = uri.split(":")
    if len(parts) != 3 or parts[0] != "spotify" or parts[1] not in {"playlist", "track", "album"}:
        raise SpotifyDesktopError(f"Unsupported Spotify URI: {uri}")
    return parts[1], parts[2]
