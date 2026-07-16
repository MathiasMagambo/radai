from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramCommand:
    name: str
    args: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class BotReply:
    text: str


Handler = Callable[[TelegramCommand], BotReply]


COMMAND_RE = re.compile(r"^/(\w+)(?:@\w+)?(?:\s+(.*))?$")
RATIO_RE = re.compile(r"^(\d+)\s*[:/]\s*(\d+)$")


def parse_command(text: str) -> TelegramCommand:
    stripped = text.strip()
    match = COMMAND_RE.match(stripped)
    if not match:
        if stripped.lower() in {"start stream", "startstream", "play"}:
            return TelegramCommand("startstream", raw=text)
        raise TelegramError("message is not a command")
    name = match.group(1).lower()
    arg_text = (match.group(2) or "").strip()
    args = tuple(arg_text.split()) if arg_text else ()
    return TelegramCommand(name=name, args=args, raw=text)


def parse_ratio(command: TelegramCommand) -> tuple[int, int]:
    if command.name != "ratio" or len(command.args) != 1:
        raise TelegramError("usage: /ratio 20:10")
    match = RATIO_RE.match(command.args[0])
    if not match:
        raise TelegramError("usage: /ratio 20:10")
    podcast_minutes = int(match.group(1))
    music_minutes = int(match.group(2))
    if podcast_minutes <= 0 or music_minutes <= 0:
        raise TelegramError("ratio minutes must be positive")
    return podcast_minutes, music_minutes


class AgentActions(Protocol):
    def sync(self, feed_urls: tuple[str, ...]) -> str: ...
    def episodes(self) -> str: ...
    def playlists(self) -> str: ...
    def set_ratio(self, podcast_minutes: int, music_minutes: int) -> str: ...
    def set_music_playlist(self, name: str) -> str: ...
    def set_music_radio(self, seed: str) -> str: ...
    def play(self, query: str) -> str: ...
    def start_stream(self) -> str: ...
    def stop_stream(self) -> str: ...
    def keep_recording(self) -> str: ...
    def discard_recording(self) -> str: ...
    def status(self) -> str: ...


HELP_TEXT = """Commands:
/sync <feed-url> [...]
/episodes
/play latest
/ratio 20:10
/music playlist <name>
/music radio <seed>
/playlists
/startstream
/stopstream
/keep
/discard
/status"""


class TelegramBotClient:
    def __init__(self, token: str, *, api_base: str = "https://api.telegram.org") -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")

    def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict]:
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params).get("result", [])

    def send_message(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text})

    def _call(self, method: str, params: dict) -> dict:
        data = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base}/bot{self.token}/{method}",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "radai-agent/0.1"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise TelegramError(f"Telegram API {method} failed: {payload}")
        return payload


class CommandRouter:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        self._handlers[name] = handler

    def handle_text(self, text: str) -> BotReply:
        command = parse_command(text)
        handler = self._handlers.get(command.name)
        if handler is None:
            return BotReply(f"Unknown command /{command.name}. Try /start.")
        return handler(command)



def build_router(actions: AgentActions) -> CommandRouter:
    router = CommandRouter()

    router.register("start", lambda command: BotReply(HELP_TEXT))
    router.register("help", lambda command: BotReply(HELP_TEXT))
    router.register("sync", lambda command: BotReply(actions.sync(command.args)))
    router.register("episodes", lambda command: BotReply(actions.episodes()))
    router.register("playlists", lambda command: BotReply(actions.playlists()))
    router.register("startstream", lambda command: BotReply(actions.start_stream()))
    router.register("stopstream", lambda command: BotReply(actions.stop_stream()))
    router.register("keep", lambda command: BotReply(actions.keep_recording()))
    router.register("discard", lambda command: BotReply(actions.discard_recording()))
    router.register("status", lambda command: BotReply(actions.status()))

    def ratio(command: TelegramCommand) -> BotReply:
        podcast_minutes, music_minutes = parse_ratio(command)
        return BotReply(actions.set_ratio(podcast_minutes, music_minutes))

    def music(command: TelegramCommand) -> BotReply:
        if len(command.args) < 2:
            raise TelegramError("usage: /music playlist <name> or /music radio <seed>")
        mode = command.args[0].lower()
        value = " ".join(command.args[1:]).strip()
        if mode == "playlist":
            return BotReply(actions.set_music_playlist(value))
        if mode == "radio":
            return BotReply(actions.set_music_radio(value))
        raise TelegramError("usage: /music playlist <name> or /music radio <seed>")

    def play(command: TelegramCommand) -> BotReply:
        query = " ".join(command.args).strip() or "latest"
        return BotReply(actions.play(query))

    router.register("ratio", ratio)
    router.register("music", music)
    router.register("play", play)
    return router

class LongPollingBot:
    def __init__(self, client: TelegramBotClient, router: CommandRouter, allowed_user_ids: Iterable[int]) -> None:
        self.client = client
        self.router = router
        self.allowed_user_ids = set(allowed_user_ids)

    def poll_once(self, *, offset: int | None = None) -> int | None:
        next_offset = offset
        for update in self.client.get_updates(offset=offset):
            update_id = int(update["update_id"])
            next_offset = update_id + 1
            message = update.get("message") or update.get("edited_message") or {}
            user = message.get("from") or {}
            user_id = int(user.get("id") or 0)
            chat = message.get("chat") or {}
            chat_id = int(chat.get("id") or 0)
            text = message.get("text")
            if not text or not chat_id:
                continue
            if user_id not in self.allowed_user_ids:
                self.client.send_message(chat_id, "Unauthorized user.")
                continue
            try:
                reply = self.router.handle_text(text)
            except Exception as exc:
                reply = BotReply(f"Error: {exc}")
            self.client.send_message(chat_id, reply.text)
        return next_offset
