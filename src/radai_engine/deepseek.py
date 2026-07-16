from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .models import CutRange, DeepSeekPlan, MusicInsertion


class DeepSeekError(RuntimeError):
    pass


@dataclass(frozen=True)
class EpisodePlanningInput:
    episode_id: str
    title: str
    show: str
    description: str
    transcript: str
    duration_sec: float | None = None
    podcast_window_sec: int = 1200
    music_window_sec: int = 600

    def input_hash(self) -> str:
        hasher = hashlib.sha256()
        for value in (
            self.episode_id,
            self.title,
            self.show,
            self.description,
            self.transcript,
            str(self.duration_sec),
            str(self.podcast_window_sec),
            str(self.music_window_sec),
        ):
            hasher.update(value.encode("utf-8"))
            hasher.update(b"\0")
        return hasher.hexdigest()


class DeepSeekClient:
    def __init__(self, api_key: str, *, model: str = "deepseek-chat", base_url: str = "https://api.deepseek.com") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def plan_episode(
        self,
        planning_input: EpisodePlanningInput,
        *,
        timeout: float = 60.0,
        strict_duration: bool = True,
    ) -> DeepSeekPlan:
        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You analyze timestamped YouTube podcast transcripts for a private radio station. "
                        "Return only strict JSON with keys: episode_id, ad_cuts, music_insertions, confidence, warnings. "
                        "Identify host-read ads, sponsor segments, dynamic ad reads, and promotional interruptions; "
                        "ad_cuts items require precise start_sec, end_sec, and reason derived from transcript timestamps. "
                        "music_insertions identify natural chapter or topic breaks outside advertisements and require "
                        "after_sec, window_sec, mood, and reason. Keep breaks at least ten minutes apart. "
                        "Confidence must be a number from 0 to 1. "
                        "The scheduler will play exactly three Spotify songs at every music insertion."
                    ),
                },
                {"role": "user", "content": _prompt(planning_input)},
            ],
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "radai-engine/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").lower()
            if exc.code == 402 or "insufficient balance" in body or "credit" in body:
                message = "DeepSeek API credits are exhausted"
            elif exc.code == 429:
                message = "DeepSeek API rate limit was reached"
            elif exc.code >= 500:
                message = f"DeepSeek API is unavailable (HTTP {exc.code})"
            else:
                message = f"DeepSeek API request failed (HTTP {exc.code})"
            raise DeepSeekError(message) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DeepSeekError("DeepSeek API is not responding") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("unexpected DeepSeek response shape") from exc
        try:
            raw_plan = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DeepSeekError("DeepSeek did not return valid JSON") from exc
        duration = planning_input.duration_sec if strict_duration else None
        return parse_deepseek_plan(raw_plan, duration_sec=duration, validate=strict_duration)


def parse_deepseek_plan(
    raw: dict[str, Any],
    *,
    duration_sec: float | None = None,
    validate: bool = True,
) -> DeepSeekPlan:
    if not isinstance(raw, dict):
        raise DeepSeekError("plan must be a JSON object")
    episode_id = _required_str(raw, "episode_id")
    warnings = [str(item) for item in _list(raw.get("warnings", []), "warnings")]
    cuts: list[CutRange] = []
    for item in _list(raw.get("ad_cuts", []), "ad_cuts"):
        try:
            cuts.append(_parse_cut(item))
        except (KeyError, TypeError, ValueError) as exc:
            if validate:
                raise DeepSeekError("ad_cuts entry is invalid") from exc
            warnings.append("Discarded an invalid ad cut")
    insertions: list[MusicInsertion] = []
    for item in _list(raw.get("music_insertions", []), "music_insertions"):
        try:
            insertions.append(_parse_insertion(item))
        except (KeyError, TypeError, ValueError) as exc:
            if validate:
                raise DeepSeekError("music_insertions entry is invalid") from exc
            warnings.append("Discarded an invalid music insertion")
    confidence = _parse_confidence(raw.get("confidence", 0.0))
    parsed_cuts = tuple(cuts)
    parsed_insertions = tuple(insertions)
    if validate:
        _validate_non_overlapping_cuts(parsed_cuts, duration_sec)
        _validate_insertions(parsed_insertions, duration_sec)
    return DeepSeekPlan(
        episode_id=episode_id,
        ad_cuts=parsed_cuts,
        music_insertions=parsed_insertions,
        confidence=confidence,
        warnings=tuple(warnings),
        raw=raw,
    )


def _parse_cut(item: Any) -> CutRange:
    if not isinstance(item, dict):
        raise DeepSeekError("ad_cuts entries must be objects")
    return CutRange(start_sec=float(item["start_sec"]), end_sec=float(item["end_sec"]), reason=str(item.get("reason", "")))


def _parse_insertion(item: Any) -> MusicInsertion:
    if not isinstance(item, dict):
        raise DeepSeekError("music_insertions entries must be objects")
    return MusicInsertion(
        after_sec=float(item["after_sec"]),
        window_sec=float(item["window_sec"]),
        mood=str(item.get("mood", "")),
        reason=str(item.get("reason", "")),
    )


def _validate_non_overlapping_cuts(cuts: tuple[CutRange, ...], duration_sec: float | None) -> None:
    previous_end = -1.0
    for cut in sorted(cuts, key=lambda c: c.start_sec):
        if cut.start_sec < previous_end:
            raise DeepSeekError("ad cuts overlap")
        if duration_sec is not None and cut.end_sec > duration_sec:
            raise DeepSeekError("ad cut exceeds episode duration")
        previous_end = cut.end_sec


def _validate_insertions(insertions: tuple[MusicInsertion, ...], duration_sec: float | None) -> None:
    previous = -1.0
    for insertion in sorted(insertions, key=lambda i: i.after_sec):
        if insertion.after_sec < previous:
            raise DeepSeekError("music insertions are not monotonic")
        if duration_sec is not None and insertion.after_sec > duration_sec:
            raise DeepSeekError("music insertion exceeds episode duration")
        previous = insertion.after_sec


def _parse_confidence(value: Any) -> float:
    if isinstance(value, str):
        named = {"low": 0.25, "medium": 0.5, "high": 0.85}
        normalized = value.strip().lower()
        if normalized in named:
            return named[normalized]
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise DeepSeekError("confidence must be a number from 0 to 1") from exc
    return confidence / 100 if 1 < confidence <= 100 else confidence


def _required_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise DeepSeekError(f"{key} must be a non-empty string")
    return value


def _list(value: Any, key: str) -> list[Any]:
    if not isinstance(value, list):
        raise DeepSeekError(f"{key} must be a list")
    return value


def _prompt(planning_input: EpisodePlanningInput) -> str:
    duration = "unknown" if planning_input.duration_sec is None else f"{planning_input.duration_sec:.3f} seconds"
    return (
        f"Episode id: {planning_input.episode_id}\n"
        f"Show: {planning_input.show}\n"
        f"Title: {planning_input.title}\n"
        f"Duration: {duration}\n"
        "At every natural topic break, the scheduler inserts exactly three Spotify songs. "
        "Do not create a music insertion inside an advertisement.\n"
        f"Description:\n{planning_input.description}\n\n"
        f"Timestamped transcript:\n{planning_input.transcript}"
    )
