from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
import urllib.error
from typing import Any
from collections.abc import Callable


class SpotifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpotifyDevice:
    id: str
    name: str
    type: str
    is_active: bool
    is_restricted: bool


@dataclass(frozen=True)
class SpotifyPlaylist:
    id: str
    name: str
    uri: str
    owner: str
    tracks_total: int


@dataclass(frozen=True)
class SpotifyTrack:
    id: str
    name: str
    uri: str
    artists: tuple[str, ...]
    album: str
    duration_ms: int


@dataclass(frozen=True)
class SpotifyPlayback:
    is_playing: bool
    progress_ms: int
    track: SpotifyTrack | None

class SpotifyClient:
    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
        *,
        api_base: str = "https://api.spotify.com/v1",
        token_url: str = "https://accounts.spotify.com/api/token",
        access_token_provider: Callable[[], str] | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.api_base = api_base.rstrip("/")
        self.token_url = token_url
        self.access_token_provider = access_token_provider
        self._access_token: str | None = None

    def access_token(self) -> str:
        if self.access_token_provider is not None:
            return self.access_token_provider()
        if self._access_token is None:
            self._access_token = self.refresh_access_token()
        return self._access_token

    def refresh_access_token(self) -> str:
        credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": self.refresh_token}).encode("utf-8")
        request = urllib.request.Request(
            self.token_url,
            data=data,
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise SpotifyError("Spotify token response did not include access_token")
        return token

    def devices(self) -> tuple[SpotifyDevice, ...]:
        payload = self._request_json("GET", "/me/player/devices")
        return tuple(
            SpotifyDevice(
                id=str(item["id"]),
                name=str(item.get("name", "")),
                type=str(item.get("type", "")),
                is_active=bool(item.get("is_active")),
                is_restricted=bool(item.get("is_restricted")),
            )
            for item in payload.get("devices", [])
            if item.get("id")
        )

    def playlists(self, *, limit: int = 50) -> tuple[SpotifyPlaylist, ...]:
        playlists: list[SpotifyPlaylist] = []
        offset = 0
        while True:
            payload = self._request_json("GET", f"/me/playlists?limit={limit}&offset={offset}")
            items = payload.get("items", [])
            for item in items:
                playlists.append(
                    SpotifyPlaylist(
                        id=str(item["id"]),
                        name=str(item.get("name", "")),
                        uri=str(item.get("uri", "")),
                        owner=str(item.get("owner", {}).get("display_name") or item.get("owner", {}).get("id") or ""),
                        tracks_total=int(item.get("tracks", {}).get("total") or 0),
                    )
                )
            if not payload.get("next") or not items:
                break
            offset += len(items)
        return tuple(playlists)

    def search_tracks(self, query: str, *, limit: int = 10) -> tuple[SpotifyTrack, ...]:
        encoded = urllib.parse.quote(query)
        payload = self._request_json("GET", f"/search?q={encoded}&type=track&limit={limit}")
        return tuple(
            SpotifyTrack(
                id=str(item["id"]),
                name=str(item.get("name", "")),
                uri=str(item.get("uri", "")),
                artists=tuple(str(artist.get("name", "")) for artist in item.get("artists", [])),
                album=str(item.get("album", {}).get("name", "")),
                duration_ms=int(item.get("duration_ms") or 0),
            )
            for item in payload.get("tracks", {}).get("items", [])
            if item.get("id")
        )

    def transfer_playback(self, device_id: str, *, play: bool = False) -> None:
        self._request_empty("PUT", "/me/player", {"device_ids": [device_id], "play": play})

    def play_context(self, device_id: str, spotify_uri: str, *, shuffle: bool = False) -> None:
        if shuffle:
            self._request_empty("PUT", f"/me/player/shuffle?state=true&device_id={urllib.parse.quote(device_id)}", None)
        self._request_empty("PUT", f"/me/player/play?device_id={urllib.parse.quote(device_id)}", {"context_uri": spotify_uri})

    def play_tracks(self, device_id: str, spotify_uris: tuple[str, ...]) -> None:
        if not spotify_uris:
            raise SpotifyError("at least one Spotify track URI is required")
        path = f"/me/player/play?device_id={urllib.parse.quote(device_id)}"
        self._request_empty("PUT", path, {"uris": list(spotify_uris)})

    def pause(self, device_id: str) -> None:
        self._request_empty("PUT", f"/me/player/pause?device_id={urllib.parse.quote(device_id)}", None)

    def current_playback(self) -> SpotifyPlayback:
        payload = self._request_json("GET", "/me/player")
        item = payload.get("item")
        track = None
        if isinstance(item, dict) and item.get("id"):
            track = SpotifyTrack(
                id=str(item["id"]),
                name=str(item.get("name", "")),
                uri=str(item.get("uri", "")),
                artists=tuple(str(artist.get("name", "")) for artist in item.get("artists", [])),
                album=str(item.get("album", {}).get("name", "")),
                duration_ms=int(item.get("duration_ms") or 0),
            )
        return SpotifyPlayback(
            is_playing=bool(payload.get("is_playing")),
            progress_ms=int(payload.get("progress_ms") or 0),
            track=track,
        )

    def recommendations_seed_uri(self, seed: str) -> str:
        return seed if seed.startswith("spotify:") else f"spotify:playlist:{seed}"

    def _request_json(self, method: str, path: str) -> dict[str, Any]:
        data = self._request(method, path, None)
        if not data:
            return {}
        return json.loads(data.decode("utf-8"))

    def _request_empty(self, method: str, path: str, body: dict[str, Any] | None) -> None:
        self._request(method, path, body)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> bytes:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.api_base + path,
            data=data,
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Content-Type": "application/json",
                "User-Agent": "radai-engine/0.1",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SpotifyError(f"Spotify API {method} {path} failed with {exc.code}: {detail}") from exc
