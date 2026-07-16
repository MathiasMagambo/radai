from __future__ import annotations

import email.utils
import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin

from .models import ParsedFeed, PodcastEpisode, TranscriptRef

PODCAST_NS = "https://podcastindex.org/namespace/1.0"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
TRANSCRIPT_EXTENSIONS = (".vtt", ".srt", ".txt", ".json", ".html", ".htm")
TRANSCRIPT_RE = re.compile(r"\btranscript\b", re.IGNORECASE)


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href_stack: list[str | None] = []
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attrs_map = {k.lower(): v for k, v in attrs if v is not None}
            self._href_stack.append(attrs_map.get("href"))
            self._text_parts.append("")

    def handle_data(self, data: str) -> None:
        if self._href_stack:
            self._text_parts[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href_stack:
            href = self._href_stack.pop()
            text = self._text_parts.pop().strip()
            if href:
                self.links.append((href, text))


def fetch_feed(url: str, etag: str | None = None, last_modified: str | None = None, timeout: float = 20.0) -> FetchResult:
    request = urllib.request.Request(url, headers={"User-Agent": "radai-agent/0.1"})
    if etag:
        request.add_header("If-None-Match", etag)
    if last_modified:
        request.add_header("If-Modified-Since", last_modified)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return FetchResult(
                body=response.read(),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult(b"", not_modified=True)
        raise


def parse_feed_xml(xml_bytes: bytes | str, feed_url: str) -> ParsedFeed:
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel") if root.tag.lower().endswith("rss") else root
    if channel is None:
        raise ValueError("RSS channel not found")
    feed_title = _text(channel.find("title")) or feed_url
    episodes: list[PodcastEpisode] = []
    for item in channel.findall("item"):
        enclosure = _first_enclosure(item)
        if enclosure is None:
            continue
        audio_url = enclosure.get("url", "").strip()
        if not audio_url:
            continue
        title = _text(item.find("title")) or audio_url.rsplit("/", 1)[-1]
        description = _description(item)
        link = _text(item.find("link"))
        guid = _text(item.find("guid")) or audio_url or f"{title}|{_text(item.find('pubDate'))}"
        transcripts = tuple(_transcripts(item, feed_url, description, link))
        episodes.append(
            PodcastEpisode(
                feed_url=feed_url,
                guid=guid,
                title=html.unescape(title.strip()),
                description=description,
                published_at=_normalize_pubdate(_text(item.find("pubDate"))),
                audio_url=urljoin(feed_url, audio_url),
                audio_mime=enclosure.get("type"),
                audio_bytes=_parse_int(enclosure.get("length")),
                link=link,
                transcripts=transcripts,
            )
        )
    return ParsedFeed(url=feed_url, title=html.unescape(feed_title.strip()), episodes=tuple(episodes))


def _first_enclosure(item: ET.Element) -> ET.Element | None:
    for enclosure in item.findall("enclosure"):
        mime = enclosure.get("type", "").lower()
        if mime.startswith("audio/") or enclosure.get("url", "").lower().endswith((".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")):
            return enclosure
    return item.find("enclosure")


def _description(item: ET.Element) -> str:
    for path in (f"{{{CONTENT_NS}}}encoded", "description", "summary"):
        value = _text(item.find(path))
        if value:
            return value.strip()
    return ""


def _transcripts(item: ET.Element, feed_url: str, description: str, link: str | None) -> Iterable[TranscriptRef]:
    seen: set[str] = set()
    for element in item.iter():
        if _local_name(element.tag) != "transcript":
            continue
        url = (element.get("url") or element.get("href") or "").strip()
        if not url:
            continue
        absolute = urljoin(feed_url, url)
        if absolute in seen:
            continue
        seen.add(absolute)
        yield TranscriptRef(
            url=absolute,
            mime=(element.get("type") or _mime_from_url(absolute) or "text/html").strip(),
            source="podcast:transcript",
            language=element.get("language"),
            rel=element.get("rel"),
        )
    for href, text in _html_links(description):
        absolute = urljoin(link or feed_url, href)
        label = f"{href} {text}"
        if absolute in seen or not _looks_like_transcript(label):
            continue
        seen.add(absolute)
        yield TranscriptRef(
            url=absolute,
            mime=_mime_from_url(absolute) or "text/html",
            source="description-link",
        )


def _html_links(fragment: str) -> list[tuple[str, str]]:
    if not fragment:
        return []
    parser = _LinkCollector()
    parser.feed(fragment)
    return parser.links


def _looks_like_transcript(label: str) -> bool:
    lowered = label.lower().split("?", 1)[0]
    return bool(TRANSCRIPT_RE.search(label)) or lowered.endswith(TRANSCRIPT_EXTENSIONS)


def _mime_from_url(url: str) -> str | None:
    lowered = url.lower().split("?", 1)[0]
    if lowered.endswith(".vtt"):
        return "text/vtt"
    if lowered.endswith(".srt"):
        return "application/x-subrip"
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".txt"):
        return "text/plain"
    if lowered.endswith((".html", ".htm")):
        return "text/html"
    return None


def _normalize_pubdate(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value.strip()
    return parsed.isoformat()


def _parse_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _text(element: ET.Element | None) -> str:
    return "" if element is None or element.text is None else element.text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()
