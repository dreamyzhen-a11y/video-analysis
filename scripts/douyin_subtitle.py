#!/usr/bin/env python3
"""Low-intrusion Douyin/Bilibili subtitle and visual extraction helper.

The script follows the workflow in SKILL.md:
metadata/native subtitles/public images first, legal media acquisition only as a
fallback, then optional ASR/OCR for acquired or user-provided media.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

ALLOWED_SOURCE_LABELS = {
    "native_subtitle",
    "public_page_metadata",
    "public_image_or_gallery",
    "asr_audio",
    "ocr_burned_subtitle",
    "visual_slide_summary",
    "visual_slide_diff",
    "legal_media_acquisition",
    "user_uploaded_media",
    "unavailable",
}

SUBTITLE_SOURCE_LABELS = {
    "native_subtitle",
    "asr_audio",
    "ocr_burned_subtitle",
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "downloaders": {
        "lux": {
            "enabled": True,
            "priority": 1,
            "type": "cli",
            "command": "lux",
            "supports": ["douyin", "bilibili"],
        },
        "video_analyse": {
            "enabled": True,
            "priority": 2,
            "type": "api",
            "endpoint": "https://proxy.layzz.cn/lyz/platAnalyse/",
            "token_env": "DOUYIN_SUBTITLE_VIDEO_ANALYSE_TOKEN",
            "supports": ["douyin", "bilibili"],
        },
        "galaxy_downloader": {
            "enabled": False,
            "priority": 3,
            "type": "api",
            "endpoint": "http://localhost:8788/api/parse",
            "supports": ["douyin", "bilibili"],
        },
    },
    "subtitle": {
        "asr": {
            "enabled": True,
            "engine": "faster-whisper",
            "model": "medium",
            "language": "zh",
            "device": "auto",
            "compute_type": "auto",
        },
        "ocr_subtitle": {
            "enabled": True,
            "engine": "auto",
            "sample_interval_seconds": 1.0,
            "crop_area": "bottom_40_percent",
        },
    },
    "visual": {
        "slide_summary": {"enabled": True, "sample_interval_seconds": 1.0},
        "slide_diff_summary": {"enabled": True, "stable_frame_seconds": 0.8},
    },
}


@dataclass
class NormalizedInput:
    raw: str
    kind: str
    platform: str
    normalized_url: Optional[str] = None
    local_path: Optional[Path] = None


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self.in_title = False
        self.meta: Dict[str, str] = {}
        self.links: List[Dict[str, str]] = []
        self.images: List[str] = []
        self.scripts: List[Dict[str, str]] = []
        self._script_attrs: Optional[Dict[str, str]] = None
        self._script_parts: List[str] = []
        self.visible_text_parts: List[str] = []
        self._suppress_text = 0

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self.in_title = True
        elif tag in {"script", "style", "noscript"}:
            self._suppress_text += 1
            if tag == "script":
                self._script_attrs = attr
                self._script_parts = []
        elif tag == "meta":
            key = attr.get("property") or attr.get("name") or attr.get("itemprop")
            content = attr.get("content")
            if key and content:
                self.meta[key.strip().lower()] = html.unescape(content.strip())
        elif tag == "link":
            self.links.append(attr)
        elif tag == "img":
            src = attr.get("src") or attr.get("data-src") or attr.get("data-original")
            if src:
                self.images.append(src)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        elif tag in {"script", "style", "noscript"}:
            self._suppress_text = max(0, self._suppress_text - 1)
            if tag == "script" and self._script_attrs is not None:
                data = "".join(self._script_parts).strip()
                if data:
                    item = dict(self._script_attrs)
                    item["text"] = data
                    self.scripts.append(item)
                self._script_attrs = None
                self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self._script_attrs is not None:
            self._script_parts.append(data)
        elif not self._suppress_text:
            stripped = " ".join(data.split())
            if stripped:
                self.visible_text_parts.append(stripped)

    @property
    def title(self) -> str:
        return " ".join(" ".join(self.title_parts).split())

    @property
    def visible_text(self) -> str:
        text = " ".join(self.visible_text_parts)
        return text[:4000]


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def add_status(timeline: Dict[str, Any], status: str, **fields: Any) -> None:
    entry = {"status": status, "at": now_iso()}
    entry.update({k: v for k, v in fields.items() if v is not None})
    timeline.setdefault("states", []).append(entry)
    timeline["status"] = status


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(s in lowered for s in ("token", "secret", "password", "cookie", "authorization", "api_key")):
                out[key] = "<redacted>"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def deep_update(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        return {}
    return data


def expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), "")

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)
    return value


def load_config(config_path: Path, local_config_path: Optional[Path]) -> Dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    deep_update(config, load_yaml(config_path))
    if local_config_path and local_config_path.exists():
        deep_update(config, load_yaml(local_config_path))
    return expand_env(config)


def is_url(text: str) -> bool:
    return bool(re.match(r"https?://", text.strip(), re.I))


def extract_first_url(text: str) -> Optional[str]:
    match = re.search(r"https?://[^\s<>\"'`，。；;、)）\]]+", text)
    return match.group(0) if match else None


def detect_platform(url_or_text: str) -> str:
    lowered = url_or_text.lower()
    if "douyin.com" in lowered or "iesdouyin.com" in lowered:
        return "douyin"
    if "bilibili.com" in lowered or "b23.tv" in lowered:
        return "bilibili"
    return "unknown"


def request_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    if extra:
        headers.update(extra)
    return headers


def http_open(url: str, timeout: int = 20, headers: Optional[Dict[str, str]] = None, method: str = "GET"):
    req = urllib.request.Request(url, headers=request_headers(headers), method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def resolve_url(url: str, timeout: int = 15) -> str:
    for method in ("HEAD", "GET"):
        try:
            headers = {"Range": "bytes=0-2048"} if method == "GET" else None
            with http_open(url, timeout=timeout, headers=headers, method=method) as resp:
                if method == "GET":
                    resp.read(256)
                return resp.geturl()
        except Exception:
            continue
    return url


def fetch_bytes(url: str, timeout: int = 30, max_bytes: Optional[int] = None) -> Tuple[bytes, Dict[str, str], str]:
    headers: Dict[str, str] = {}
    if max_bytes:
        headers["Range"] = f"bytes=0-{max_bytes - 1}"
    with http_open(url, timeout=timeout, headers=headers) as resp:
        data = resp.read(max_bytes + 1 if max_bytes else -1)
        if max_bytes and len(data) > max_bytes:
            data = data[:max_bytes]
        info = {k.lower(): v for k, v in resp.headers.items()}
        return data, info, resp.geturl()


def fetch_text(url: str, timeout: int = 30, max_bytes: int = 3_000_000) -> Tuple[str, str, Dict[str, str]]:
    data, headers, final_url = fetch_bytes(url, timeout=timeout, max_bytes=max_bytes)
    content_type = headers.get("content-type", "")
    charset = "utf-8"
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type)
    if match:
        charset = match.group(1)
    try:
        text = data.decode(charset, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    return text, final_url, headers


def fetch_json(url: str, timeout: int = 30) -> Any:
    text, _, _ = fetch_text(url, timeout=timeout)
    return json.loads(text)


def normalize_input(raw: str, offline: bool = False) -> NormalizedInput:
    raw = raw.strip()
    local_candidate = Path(raw.strip('"'))
    if local_candidate.exists():
        return NormalizedInput(raw=raw, kind="local", platform="local", local_path=local_candidate.resolve())

    url = extract_first_url(raw) or (raw if is_url(raw) else None)
    if url:
        normalized = url
        platform = detect_platform(url)
        if not offline and ("b23.tv" in url.lower() or "v.douyin.com" in url.lower()):
            normalized = resolve_url(url)
            platform = detect_platform(normalized)
        return NormalizedInput(raw=raw, kind="url", platform=platform, normalized_url=normalized)

    return NormalizedInput(raw=raw, kind="unknown", platform="unknown")


def parse_page(html_text: str) -> PageParser:
    parser = PageParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    return parser


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def page_metadata_from_parser(parser: PageParser, base_url: str) -> Dict[str, Any]:
    meta = parser.meta
    title = clean_text(meta.get("og:title") or meta.get("twitter:title") or parser.title)
    description = clean_text(
        meta.get("description")
        or meta.get("og:description")
        or meta.get("twitter:description")
    )
    author = clean_text(meta.get("author") or meta.get("article:author"))
    duration = clean_text(meta.get("video:duration") or meta.get("duration"))
    upload_time = clean_text(meta.get("article:published_time") or meta.get("publishdate"))
    images = collect_page_images(parser, base_url)
    result: Dict[str, Any] = {
        "title": title,
        "description": description,
        "author": author,
        "upload_time": upload_time,
        "duration": duration,
        "cover_image": images[0] if images else "",
        "images": images,
        "visible_text_sample": parser.visible_text,
        "source": "public_page_metadata",
    }
    return {k: v for k, v in result.items() if v not in ("", [], None)}


def collect_page_images(parser: PageParser, base_url: str) -> List[str]:
    candidates: List[str] = []
    for key in ("og:image", "twitter:image", "image"):
        if parser.meta.get(key):
            candidates.append(parser.meta[key])
    for link in parser.links:
        rel = link.get("rel", "").lower()
        if any(item in rel for item in ("image_src", "apple-touch-icon", "icon")):
            href = link.get("href")
            if href:
                candidates.append(href)
    candidates.extend(parser.images[:50])
    normalized: List[str] = []
    seen = set()
    for item in candidates:
        if not item or item.startswith("data:"):
            continue
        full = urllib.parse.urljoin(base_url, html.unescape(item))
        if full not in seen and is_url(full):
            seen.add(full)
            normalized.append(full)
    return normalized[:30]


def balanced_json_after(text: str, start_pos: int) -> Optional[str]:
    open_pos = -1
    opener = ""
    for index in range(start_pos, len(text)):
        if text[index] in "[{":
            open_pos = index
            opener = text[index]
            break
    if open_pos < 0:
        return None
    closer = "]" if opener == "[" else "}"
    depth = 0
    in_string = False
    quote = ""
    escape = False
    for index in range(open_pos, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in ('"', "'"):
            in_string = True
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[open_pos : index + 1]
    return None


def extract_json_blobs(parser: PageParser) -> List[Any]:
    blobs: List[Any] = []
    markers = [
        "__INITIAL_STATE__",
        "__NEXT_DATA__",
        "__ROUTER_DATA__",
        "RENDER_DATA",
        "_ROUTER_DATA",
        "__playinfo__",
        "__INITIAL_DATA__",
    ]
    for script in parser.scripts:
        text = script.get("text", "")
        script_id = script.get("id", "")
        script_type = script.get("type", "")
        candidates = []
        if script_id == "RENDER_DATA":
            candidates.append(urllib.parse.unquote(text))
        if "json" in script_type.lower() or text.lstrip().startswith(("{", "[")):
            candidates.append(text)
        for marker in markers:
            pos = text.find(marker)
            if pos >= 0:
                found = balanced_json_after(text, pos + len(marker))
                if found:
                    candidates.append(found)
        for candidate in candidates:
            candidate = candidate.strip().rstrip(";")
            if not candidate:
                continue
            try:
                blobs.append(json.loads(candidate))
            except Exception:
                continue
    return blobs


def iter_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_dicts(item)


def recursive_values_for_keys(value: Any, key_patterns: Sequence[str]) -> List[Any]:
    found: List[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(pattern in lowered for pattern in key_patterns):
                found.append(item)
            found.extend(recursive_values_for_keys(item, key_patterns))
    elif isinstance(value, list):
        for item in value:
            found.extend(recursive_values_for_keys(item, key_patterns))
    return found


def seconds_from_value(value: Any, key_hint: str = "") -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.strip()
        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?(\.\d+)?$", value):
            parts = [float(part) for part in value.split(":")]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
    try:
        number = float(value)
    except Exception:
        return None
    lowered = key_hint.lower()
    if number > 10000 or "ms" in lowered or "millisecond" in lowered:
        return number / 1000.0
    return number


def text_from_segment_dict(item: Dict[str, Any]) -> str:
    for key in ("content", "text", "utterance", "sentence", "line", "subtitle", "caption"):
        value = item.get(key)
        if isinstance(value, str) and clean_text(value):
            return clean_text(value)
    return ""


def segment_from_dict(item: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    text = text_from_segment_dict(item)
    if not text:
        return None
    start: Optional[float] = None
    end: Optional[float] = None
    for key in ("from", "start", "start_time", "startTime", "begin", "begin_time", "beginTime"):
        if key in item:
            start = seconds_from_value(item.get(key), key)
            break
    for key in ("to", "end", "end_time", "endTime", "stop", "stop_time", "stopTime"):
        if key in item:
            end = seconds_from_value(item.get(key), key)
            break
    if end is None:
        for key in ("duration", "dur"):
            if key in item and start is not None:
                duration = seconds_from_value(item.get(key), key)
                if duration is not None:
                    if duration > 1000:
                        duration = duration / 1000.0
                    end = start + duration
                    break
    if start is None or end is None or end <= start:
        return None
    return {
        "start": timeline_time(start),
        "end": timeline_time(end),
        "text": text,
        "source": source,
    }


def find_subtitle_segments(value: Any, source: str = "native_subtitle") -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                segment = segment_from_dict(item, source)
                if segment:
                    segments.append(segment)
                else:
                    segments.extend(find_subtitle_segments(item, source))
            elif isinstance(item, list):
                segments.extend(find_subtitle_segments(item, source))
        return dedupe_segments(segments)
    if isinstance(value, dict):
        direct = segment_from_dict(value, source)
        if direct:
            return [direct]
        likely = recursive_values_for_keys(value, ["subtitle", "caption", "sub_title", "subtitles", "sentences"])
        for item in likely:
            if item is value:
                continue
            segments.extend(find_subtitle_segments(item, source))
    return dedupe_segments(segments)


def dedupe_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for item in sorted(segments, key=lambda seg: seg.get("start", "")):
        key = (item.get("start"), item.get("end"), item.get("text"), item.get("source"))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def timeline_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        secs += 1
        millis = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def seconds_from_timeline(value: str) -> float:
    match = re.match(r"^(\d+):(\d{2}):(\d{2})[,.](\d{3})$", value)
    if not match:
        return 0.0
    hours, minutes, seconds, millis = [int(part) for part in match.groups()]
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def srt_time(value: str) -> str:
    return value.replace(".", ",")


def extract_bilibili_ids(url: str) -> Dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    ids: Dict[str, Any] = {}
    match = re.search(r"/(BV[0-9A-Za-z]+)", path)
    if not match:
        match = re.search(r"(BV[0-9A-Za-z]+)", url)
    if match:
        ids["bvid"] = match.group(1)
    av_match = re.search(r"/av(\d+)", path, re.I)
    if av_match:
        ids["aid"] = av_match.group(1)
    if query.get("p"):
        try:
            ids["p"] = max(1, int(query["p"][0]))
        except Exception:
            pass
    return ids


def bilibili_view_api(ids: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if ids.get("bvid"):
        url = "https://api.bilibili.com/x/web-interface/view?bvid=" + urllib.parse.quote(str(ids["bvid"]))
    elif ids.get("aid"):
        url = "https://api.bilibili.com/x/web-interface/view?aid=" + urllib.parse.quote(str(ids["aid"]))
    else:
        return None
    data = fetch_json(url)
    if isinstance(data, dict) and data.get("code") == 0 and isinstance(data.get("data"), dict):
        return data["data"]
    return None


def select_bilibili_page(view: Dict[str, Any], ids: Dict[str, Any]) -> Dict[str, Any]:
    pages = view.get("pages") or []
    if not pages:
        return {}
    index = int(ids.get("p", 1)) - 1
    index = min(max(0, index), len(pages) - 1)
    return pages[index] if isinstance(pages[index], dict) else {}


def normalize_subtitle_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.bilibili.com" + url
    return url


def parse_bilibili_subtitle_json(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    body = data.get("body") or data.get("data") or data.get("list")
    return find_subtitle_segments(body, "native_subtitle")


def extract_bilibili_native_subtitles(url: str, timeline: Dict[str, Any]) -> List[Dict[str, Any]]:
    ids = extract_bilibili_ids(url)
    if not ids:
        return []
    try:
        view = bilibili_view_api(ids)
    except Exception as exc:
        add_status(timeline, "native_subtitle_not_found", reason=f"bilibili_view_api_failed: {exc}")
        return []
    if not view:
        return []
    page = select_bilibili_page(view, ids)
    metadata = timeline.setdefault("metadata", {})
    metadata.update(
        redact(
            {
                "title": view.get("title"),
                "description": view.get("desc"),
                "author": (view.get("owner") or {}).get("name"),
                "upload_time": view.get("pubdate"),
                "duration": view.get("duration"),
                "cover_image": view.get("pic"),
                "bvid": view.get("bvid"),
                "aid": view.get("aid"),
                "cid": page.get("cid"),
                "source": "public_page_metadata",
            }
        )
    )
    if metadata:
        add_status(timeline, "public_metadata_found", source="bilibili_api")

    subtitle_items: List[Dict[str, Any]] = []
    raw_subtitles = ((view.get("subtitle") or {}).get("list") or [])
    if not raw_subtitles and page.get("cid"):
        player_url = (
            "https://api.bilibili.com/x/player/v2?cid="
            + urllib.parse.quote(str(page.get("cid")))
        )
        if view.get("bvid"):
            player_url += "&bvid=" + urllib.parse.quote(str(view.get("bvid")))
        try:
            player = fetch_json(player_url)
            raw_subtitles = (((player or {}).get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        except Exception:
            raw_subtitles = []

    for item in raw_subtitles:
        if not isinstance(item, dict):
            continue
        sub_url = item.get("subtitle_url") or item.get("url")
        if not sub_url:
            continue
        try:
            sub_json = fetch_json(normalize_subtitle_url(str(sub_url)))
            subtitle_items.extend(parse_bilibili_subtitle_json(sub_json))
        except Exception:
            continue

    return dedupe_segments(subtitle_items)


def fetch_public_page(url: str, timeline: Dict[str, Any]) -> Tuple[Optional[str], Optional[PageParser], str]:
    try:
        html_text, final_url, headers = fetch_text(url)
        content_type = headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type and "json" not in content_type:
            add_status(timeline, "public_visual_info_insufficient", reason="public_page_not_textual")
            return html_text, None, final_url
        parser = parse_page(html_text)
        metadata = page_metadata_from_parser(parser, final_url)
        if metadata:
            timeline.setdefault("metadata", {}).update(redact(metadata))
            if metadata.get("images"):
                timeline.setdefault("public_images", metadata.get("images", []))
            add_status(timeline, "public_metadata_found", source="html_meta")
        return html_text, parser, final_url
    except urllib.error.HTTPError as exc:
        add_status(timeline, "public_visual_info_insufficient", reason=f"http_{exc.code}")
    except Exception as exc:
        add_status(timeline, "public_visual_info_insufficient", reason=str(exc))
    return None, None, url


def extract_native_from_page(parser: Optional[PageParser]) -> List[Dict[str, Any]]:
    if not parser:
        return []
    segments: List[Dict[str, Any]] = []
    for blob in extract_json_blobs(parser):
        segments.extend(find_subtitle_segments(blob, "native_subtitle"))
    return dedupe_segments(segments)


def safe_filename(text: str, default: str = "file") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")
    return text[:80] or default


def download_public_images(urls: Sequence[str], output_dir: Path, timeline: Dict[str, Any], limit: int = 12) -> List[Path]:
    image_dir = output_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for index, url in enumerate(urls[:limit], start=1):
        try:
            data, headers, final_url = fetch_bytes(url, timeout=30, max_bytes=15_000_000)
            content_type = headers.get("content-type", "")
            if "image" not in content_type and not re.search(r"\.(png|jpe?g|webp|gif|bmp)(\?|$)", final_url, re.I):
                continue
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or Path(urllib.parse.urlparse(final_url).path).suffix
            if not ext or len(ext) > 6:
                ext = ".jpg"
            path = image_dir / f"public_image_{index:02d}{ext}"
            path.write_bytes(data)
            paths.append(path)
        except Exception as exc:
            timeline.setdefault("warnings", []).append({"image_url": url, "warning": str(exc), "source": "public_image_or_gallery"})
    if paths:
        add_status(timeline, "public_visual_info_found", count=len(paths), source="public_image_or_gallery")
    return paths


def pil_image_info(path: Path) -> Dict[str, Any]:
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return {"path": str(path), "pil_available": False}
    try:
        with Image.open(path) as image:
            return {"path": str(path), "width": image.width, "height": image.height, "mode": image.mode}
    except Exception as exc:
        return {"path": str(path), "error": str(exc)}


def run_ocr_on_image(path: Path, crop_bottom: bool = False) -> Dict[str, Any]:
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return {"text": "", "engine": "unavailable", "warning": "Pillow is not installed"}

    try:
        image = Image.open(path)
        if crop_bottom:
            width, height = image.size
            image = image.crop((0, int(height * 0.60), width, height))
    except Exception as exc:
        return {"text": "", "engine": "unavailable", "warning": f"image_open_failed: {exc}"}

    try:
        import pytesseract  # type: ignore

        text = pytesseract.image_to_string(image, lang="chi_sim+eng")
        return {"text": clean_text(text), "engine": "pytesseract"}
    except Exception:
        pass

    try:
        from paddleocr import PaddleOCR  # type: ignore

        ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        result = ocr.ocr(str(path), cls=True)
        lines: List[str] = []
        for page in result or []:
            for line in page or []:
                if isinstance(line, (list, tuple)) and len(line) >= 2:
                    text_part = line[1][0] if isinstance(line[1], (list, tuple)) else ""
                    if text_part:
                        lines.append(str(text_part))
        return {"text": clean_text("\n".join(lines)), "engine": "paddleocr"}
    except Exception:
        return {"text": "", "engine": "unavailable", "warning": "No OCR engine available; install pytesseract or paddleocr"}


def visual_segments_from_images(image_paths: Sequence[Path], mode: str, timeline: Dict[str, Any]) -> List[Dict[str, Any]]:
    visual_segments: List[Dict[str, Any]] = []
    for index, path in enumerate(image_paths, start=1):
        info = pil_image_info(path)
        ocr = run_ocr_on_image(path, crop_bottom=(mode == "subtitle_ocr")) if mode in {"auto", "subtitle_ocr", "slide_summary", "slide_diff_summary"} else {"text": "", "engine": "off"}
        summary_parts = []
        if info.get("width") and info.get("height"):
            summary_parts.append(f"Image size: {info['width']}x{info['height']}.")
        if ocr.get("text"):
            summary_parts.append("Visible text: " + str(ocr["text"]))
        if not summary_parts:
            summary_parts.append("Public image collected; semantic visual summary requires model-side inspection.")
        segment = {
            "start": timeline_time(float(index - 1)),
            "end": timeline_time(float(index)),
            "title": path.name,
            "summary": " ".join(summary_parts),
            "source": "public_image_or_gallery" if "public_image" in path.name else "visual_slide_summary",
            "image_path": str(path),
            "ocr_engine": ocr.get("engine"),
        }
        if ocr.get("warning"):
            segment["warning"] = ocr["warning"]
        visual_segments.append(segment)
    if visual_segments:
        add_status(timeline, "public_visual_info_found", source="public_image_or_gallery", count=len(visual_segments))
    return visual_segments


def extract_frames(video_path: Path, output_dir: Path, interval: float = 1.0) -> List[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    frame_dir = output_dir / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    fps = 1.0 / max(interval, 0.1)
    pattern = str(frame_dir / "frame_%06d.jpg")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video_path), "-vf", f"fps={fps}", "-q:v", "3", pattern]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=900)
    except Exception:
        return []
    return sorted(frame_dir.glob("frame_*.jpg"))


def merge_ocr_subtitle_segments(frame_paths: Sequence[Path], interval: float = 1.0) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    current_text = ""
    current_start = 0.0
    last_time = 0.0
    for index, path in enumerate(frame_paths):
        t = index * interval
        ocr = run_ocr_on_image(path, crop_bottom=True)
        text = clean_text(ocr.get("text", ""))
        if not text:
            if current_text:
                segments.append({"start": timeline_time(current_start), "end": timeline_time(t), "text": current_text, "source": "ocr_burned_subtitle"})
                current_text = ""
            continue
        if text == current_text:
            last_time = t
            continue
        if current_text:
            segments.append({"start": timeline_time(current_start), "end": timeline_time(t), "text": current_text, "source": "ocr_burned_subtitle"})
        current_text = text
        current_start = t
        last_time = t
    if current_text:
        segments.append({"start": timeline_time(current_start), "end": timeline_time(last_time + interval), "text": current_text, "source": "ocr_burned_subtitle"})
    return dedupe_segments(segments)


def run_asr(
    media_path: Path,
    model_name: str,
    language: str,
    timeline: Dict[str, Any],
    device: str = "auto",
    compute_type: str = "auto",
) -> List[Dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        timeline.setdefault("warnings", []).append({"warning": "faster-whisper is not installed", "source": "asr_audio"})
        return []
    attempts: List[Tuple[str, str]] = [(device, compute_type)]
    fallback = ("cpu", "int8")
    if fallback not in attempts:
        attempts.append(fallback)
    errors: List[str] = []
    for attempt_device, attempt_compute_type in attempts:
        try:
            model = WhisperModel(model_name, device=attempt_device, compute_type=attempt_compute_type)
            segments_iter, info = model.transcribe(str(media_path), language=language, vad_filter=True)
            segments: List[Dict[str, Any]] = []
            for segment in segments_iter:
                text = clean_text(segment.text)
                if not text:
                    continue
                segments.append({"start": timeline_time(segment.start), "end": timeline_time(segment.end), "text": text, "source": "asr_audio"})
            timeline.setdefault("asr", {}).update(redact({
                "engine": "faster-whisper",
                "model": model_name,
                "language": getattr(info, "language", language),
                "device": attempt_device,
                "compute_type": attempt_compute_type,
            }))
            if segments:
                add_status(timeline, "processing_completed", source="asr_audio", count=len(segments))
            return segments
        except Exception as exc:
            errors.append(f"{attempt_device}/{attempt_compute_type}: {exc}")
    if errors:
        timeline.setdefault("warnings", []).append({"warning": "asr_failed: " + " | ".join(errors), "source": "asr_audio"})
    return []


def run_lux_json(url: str, config: Dict[str, Any], timeline: Dict[str, Any]) -> Optional[Any]:
    lux_cfg = (((config.get("downloaders") or {}).get("lux")) or {})
    if not lux_cfg.get("enabled", True):
        return None
    command = str(lux_cfg.get("command") or "lux")
    exe = shutil.which(command)
    if not exe:
        timeline.setdefault("warnings", []).append({"warning": "lux command not found", "source": "legal_media_acquisition"})
        return None
    try:
        result = subprocess.run([exe, "-j", url], capture_output=True, text=True, timeout=120, check=False)
    except Exception as exc:
        timeline.setdefault("warnings", []).append({"warning": f"lux_json_failed: {exc}", "source": "legal_media_acquisition"})
        return None
    if result.returncode != 0:
        timeline.setdefault("warnings", []).append({"warning": clean_text(result.stderr)[:500], "source": "legal_media_acquisition"})
        return None
    text = result.stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(1))
        except Exception:
            return None
    add_status(timeline, "media_acquisition_needed", source="lux_json")
    return data


def summarize_lux_metadata(data: Any) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"source": "legal_media_acquisition"}
    for item in iter_dicts(data):
        for source_key, target_key in (
            ("title", "title"),
            ("site", "site"),
            ("type", "media_type"),
            ("duration", "duration"),
            ("description", "description"),
        ):
            if target_key not in summary and item.get(source_key):
                summary[target_key] = item.get(source_key)
    return redact(summary)


def run_lux_download(url: str, output_dir: Path, config: Dict[str, Any], timeline: Dict[str, Any]) -> List[Path]:
    lux_cfg = (((config.get("downloaders") or {}).get("lux")) or {})
    if not lux_cfg.get("enabled", True):
        return []
    command = str(lux_cfg.get("command") or "lux")
    exe = shutil.which(command)
    if not exe:
        return []
    media_dir = output_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    before = {path.resolve() for path in media_dir.rglob("*") if path.is_file()}
    try:
        result = subprocess.run([exe, "-o", str(media_dir), url], capture_output=True, text=True, timeout=3600, check=False)
    except Exception as exc:
        timeline.setdefault("warnings", []).append({"warning": f"lux_download_failed: {exc}", "source": "legal_media_acquisition"})
        return []
    if result.returncode != 0:
        timeline.setdefault("warnings", []).append({"warning": clean_text(result.stderr)[:500], "source": "legal_media_acquisition"})
        return []
    after = [path.resolve() for path in media_dir.rglob("*") if path.is_file()]
    new_files = [Path(path) for path in after if path not in before]
    if new_files:
        add_status(timeline, "media_acquired", source="lux", count=len(new_files))
    return new_files


def call_json_api(endpoint: str, payload: Dict[str, Any], token: str = "", timeout: int = 60) -> Optional[Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = request_headers({"Content-Type": "application/json"})
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        query = urllib.parse.urlencode({"url": payload.get("url", "")})
        sep = "&" if "?" in endpoint else "?"
        req2 = urllib.request.Request(endpoint + sep + query, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req2, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None


def run_configured_apis(url: str, config: Dict[str, Any], timeline: Dict[str, Any], allow_external_api: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {"segments": [], "images": [], "metadata": {}}
    downloaders = config.get("downloaders") or {}
    for name, cfg in sorted(downloaders.items(), key=lambda pair: (pair[1] or {}).get("priority", 99)):
        if name == "lux" or not isinstance(cfg, dict) or not cfg.get("enabled", False):
            continue
        endpoint = str(cfg.get("endpoint") or "")
        if not endpoint:
            continue
        is_local = endpoint.startswith("http://localhost") or endpoint.startswith("http://127.0.0.1")
        if not is_local and not allow_external_api:
            timeline.setdefault("warnings", []).append({"warning": f"external api {name} skipped; pass --allow-external-api to use it", "source": "legal_media_acquisition"})
            continue
        token_env = str(cfg.get("token_env") or "")
        token = os.environ.get(token_env, "") if token_env else str(cfg.get("token") or "")
        data = call_json_api(endpoint, {"url": url}, token=token)
        if not data:
            continue
        add_status(timeline, "media_acquisition_needed", source=name)
        result["segments"].extend(find_subtitle_segments(data, "native_subtitle"))
        for item in iter_dicts(data):
            for key in ("desc", "description", "title"):
                if item.get(key) and key not in result["metadata"]:
                    result["metadata"][key] = item.get(key)
            for key in ("pics", "images", "image", "cover", "coverUrl"):
                value = item.get(key)
                if isinstance(value, str):
                    result["images"].append(value)
                elif isinstance(value, list):
                    result["images"].extend([str(v) for v in value if isinstance(v, str)])
    result["segments"] = dedupe_segments(result["segments"])
    result["images"] = list(dict.fromkeys(result["images"]))
    result["metadata"] = redact(result["metadata"])
    return result


def media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv"}:
        return "video"
    if ext in {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus"}:
        return "audio"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}:
        return "image"
    return "unknown"


def process_local_media(
    paths: Sequence[Path],
    output_dir: Path,
    visual_mode: str,
    config: Dict[str, Any],
    timeline: Dict[str, Any],
    run_asr_flag: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    segments: List[Dict[str, Any]] = []
    visual_segments: List[Dict[str, Any]] = []
    asr_cfg = (((config.get("subtitle") or {}).get("asr")) or {})
    model = str(asr_cfg.get("model") or "medium")
    language = str(asr_cfg.get("language") or "zh")
    device = str(asr_cfg.get("device") or "auto")
    compute_type = str(asr_cfg.get("compute_type") or "auto")
    interval = float((((config.get("visual") or {}).get("slide_summary") or {}).get("sample_interval_seconds") or 1.0))
    for path in paths:
        kind = media_kind(path)
        if kind in {"video", "audio"} and run_asr_flag:
            segments.extend(run_asr(path, model, language, timeline, device=device, compute_type=compute_type))
        if kind == "image":
            visual_segments.extend(visual_segments_from_images([path], visual_mode, timeline))
        if kind == "video" and visual_mode != "off":
            frames = extract_frames(path, output_dir, interval=interval)
            if visual_mode in {"subtitle_ocr", "auto"}:
                segments.extend(merge_ocr_subtitle_segments(frames, interval=interval))
            if visual_mode in {"slide_summary", "slide_diff_summary", "auto"}:
                visual_segments.extend(visual_segments_from_images(frames[:60], visual_mode, timeline))
    return dedupe_segments(segments), visual_segments


def transcript_text(segments: Sequence[Dict[str, Any]]) -> str:
    return "\n".join(clean_text(item.get("text", "")) for item in segments if clean_text(item.get("text", "")))


def write_transcript(path: Path, segments: Sequence[Dict[str, Any]]) -> None:
    text = transcript_text(segments)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def write_srt(path: Path, segments: Sequence[Dict[str, Any]]) -> None:
    lines: List[str] = []
    index = 1
    for item in segments:
        if item.get("source") not in SUBTITLE_SOURCE_LABELS:
            continue
        text = clean_text(item.get("text", ""))
        start = item.get("start")
        end = item.get("end")
        if not text or not start or not end:
            continue
        lines.extend([str(index), f"{srt_time(str(start))} --> {srt_time(str(end))}", text, ""])
        index += 1
    path.write_text("\n".join(lines), encoding="utf-8")


def write_visual_report(path: Path, timeline: Dict[str, Any]) -> None:
    lines: List[str] = ["# Visual Report", ""]
    metadata = timeline.get("metadata") or {}
    if metadata:
        lines.extend(["## Public Metadata", ""])
        for key in ("title", "description", "author", "upload_time", "duration", "cover_image"):
            if metadata.get(key):
                lines.append(f"- {key}: {metadata[key]}")
        lines.append("")
    visual_segments = timeline.get("visual_segments") or []
    if visual_segments:
        lines.extend(["## Visual Segments", ""])
        for item in visual_segments:
            lines.append(f"### {item.get('start', '')} - {item.get('end', '')} {item.get('title', '')}".strip())
            lines.append("")
            lines.append(str(item.get("summary", "")))
            if item.get("image_path"):
                lines.append(f"\nImage: `{item['image_path']}`")
            if item.get("warning"):
                lines.append(f"\nWarning: {item['warning']}")
            lines.append("")
    else:
        lines.extend(["No visual segments were produced.", ""])
    warnings = timeline.get("warnings") or []
    if warnings:
        lines.extend(["## Warnings", ""])
        for warning in warnings:
            lines.append(f"- {warning.get('source', 'unknown')}: {warning.get('warning', warning)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def export_outputs(output_dir: Path, timeline: Dict[str, Any]) -> Dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = output_dir / "transcript.txt"
    subtitle_path = output_dir / "subtitle.srt"
    visual_path = output_dir / "visual_report.md"
    timeline_path = output_dir / "timeline.json"
    files = {
        "transcript_txt": str(transcript_path),
        "subtitle_srt": str(subtitle_path),
        "visual_report_md": str(visual_path),
        "timeline_json": str(timeline_path),
    }
    timeline["files"] = files
    segments = timeline.get("segments") or []
    write_transcript(transcript_path, segments)
    write_srt(subtitle_path, segments)
    write_visual_report(visual_path, timeline)
    timeline_path.write_text(json.dumps(redact(timeline), ensure_ascii=False, indent=2), encoding="utf-8")
    return files


def choose_media_files(paths: Sequence[Path]) -> List[Path]:
    return [path for path in paths if media_kind(path) in {"video", "audio"}]


def process_url(args: argparse.Namespace, config: Dict[str, Any], normalized: NormalizedInput, timeline: Dict[str, Any]) -> None:
    assert normalized.normalized_url
    url = normalized.normalized_url
    add_status(timeline, "url_normalized", platform=normalized.platform, normalized_url=url)

    parser: Optional[PageParser] = None
    if not args.offline:
        _, parser, final_url = fetch_public_page(url, timeline)
        if final_url != url:
            timeline["url"] = final_url
            url = final_url

    segments: List[Dict[str, Any]] = []
    if not args.offline and normalized.platform == "bilibili":
        segments.extend(extract_bilibili_native_subtitles(url, timeline))
    if not args.offline:
        segments.extend(extract_native_from_page(parser))

    if segments:
        timeline["segments"] = dedupe_segments(segments)
        add_status(timeline, "native_subtitle_found", count=len(timeline["segments"]))
    else:
        add_status(timeline, "native_subtitle_not_found")

    public_images = list(timeline.get("public_images") or [])
    if not args.offline and args.visual_mode != "off" and public_images and not timeline.get("segments"):
        image_paths = download_public_images(public_images, args.output, timeline)
        timeline.setdefault("visual_segments", []).extend(visual_segments_from_images(image_paths, args.visual_mode, timeline))

    media_needed = not timeline.get("segments") and not timeline.get("visual_segments")
    if media_needed:
        add_status(timeline, "media_acquisition_needed", reason="no_native_subtitle_and_public_info_insufficient")

    acquired_files: List[Path] = []
    if media_needed and not args.offline:
        lux_json = run_lux_json(url, config, timeline)
        if lux_json is not None:
            timeline.setdefault("metadata", {}).update(summarize_lux_metadata(lux_json))
            lux_segments = find_subtitle_segments(lux_json, "native_subtitle")
            if lux_segments:
                timeline["segments"] = dedupe_segments(list(timeline.get("segments") or []) + lux_segments)
                add_status(timeline, "native_subtitle_found", source="lux_json", count=len(lux_segments))
        api_result = run_configured_apis(url, config, timeline, args.allow_external_api)
        if api_result.get("metadata"):
            timeline.setdefault("metadata", {}).update(api_result["metadata"])
        if api_result.get("segments"):
            timeline["segments"] = dedupe_segments(list(timeline.get("segments") or []) + api_result["segments"])
            add_status(timeline, "native_subtitle_found", source="configured_api", count=len(api_result["segments"]))
        if api_result.get("images") and args.visual_mode != "off" and not timeline.get("segments"):
            image_paths = download_public_images(api_result["images"], args.output, timeline)
            timeline.setdefault("visual_segments", []).extend(visual_segments_from_images(image_paths, args.visual_mode, timeline))
        if args.allow_media and not timeline.get("segments"):
            acquired_files = run_lux_download(url, args.output, config, timeline)
    if acquired_files:
        media_files = choose_media_files(acquired_files)
        local_segments, local_visual = process_local_media(media_files, args.output, args.visual_mode, config, timeline, run_asr_flag=True)
        timeline["segments"] = dedupe_segments(list(timeline.get("segments") or []) + local_segments)
        timeline.setdefault("visual_segments", []).extend(local_visual)

    if not timeline.get("segments") and not timeline.get("visual_segments"):
        add_status(
            timeline,
            "user_upload_required",
            reason="no_subtitle_no_public_media_no_download_permission",
            next_action="ask_user_upload",
        )
    else:
        add_status(timeline, "processing_completed")


def process_local(args: argparse.Namespace, config: Dict[str, Any], normalized: NormalizedInput, timeline: Dict[str, Any]) -> None:
    assert normalized.local_path
    add_status(timeline, "user_media_received", path=str(normalized.local_path), source="user_uploaded_media")
    paths: List[Path] = []
    if normalized.local_path.is_dir():
        paths = sorted(path for path in normalized.local_path.iterdir() if path.is_file())
    else:
        paths = [normalized.local_path]
    local_segments, visual_segments = process_local_media(paths, args.output, args.visual_mode, config, timeline, run_asr_flag=not args.no_asr)
    timeline["segments"] = dedupe_segments(local_segments)
    timeline["visual_segments"] = visual_segments
    add_status(timeline, "processing_completed")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract Douyin/Bilibili subtitles, public metadata, visual notes, and timeline JSON.")
    parser.add_argument("input", help="Douyin/Bilibili URL, share text, local media file, or image/screenshot directory")
    parser.add_argument("-o", "--output", type=Path, default=Path("outputs"), help="Output directory")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.yaml", help="Config YAML path")
    parser.add_argument("--local-config", type=Path, default=Path(__file__).resolve().parents[1] / "config.local.yaml", help="Ignored local override YAML path")
    parser.add_argument("--visual-mode", choices=["off", "subtitle_ocr", "slide_summary", "slide_diff_summary", "auto"], default="auto")
    parser.add_argument("--allow-media", action="store_true", help="Allow legal video/audio acquisition fallback after public sources fail")
    parser.add_argument("--allow-external-api", action="store_true", help="Allow configured non-localhost parsing APIs after public sources fail")
    parser.add_argument("--offline", action="store_true", help="Do not fetch network resources; useful for dry runs")
    parser.add_argument("--no-asr", action="store_true", help="Skip ASR for local or acquired audio/video")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config, args.local_config)
    normalized = normalize_input(args.input, offline=args.offline)
    timeline: Dict[str, Any] = {
        "input": normalized.raw,
        "url": normalized.normalized_url,
        "platform": normalized.platform,
        "status": "input_received",
        "segments": [],
        "visual_segments": [],
        "metadata": {},
        "source_policy": {
            "order": [
                "native_subtitle",
                "public_page_metadata",
                "public_image_or_gallery",
                "visual_analysis_when_available",
                "legal_media_acquisition_fallback",
                "user_uploaded_media_fallback",
                "asr_ocr_visual_analysis",
            ],
            "allow_media": bool(args.allow_media),
            "allow_external_api": bool(args.allow_external_api),
            "offline": bool(args.offline),
        },
        "states": [],
    }
    add_status(timeline, "input_received")

    if normalized.kind == "url" and normalized.normalized_url:
        process_url(args, config, normalized, timeline)
    elif normalized.kind == "local" and normalized.local_path:
        process_local(args, config, normalized, timeline)
    else:
        add_status(timeline, "unavailable", reason="input_is_not_url_or_existing_local_file")

    timeline["segments"] = dedupe_segments(timeline.get("segments") or [])
    timeline["files"] = export_outputs(args.output, timeline)
    print(json.dumps({"status": timeline.get("status"), "files": timeline["files"]}, ensure_ascii=False, indent=2))
    return 0 if timeline.get("status") == "processing_completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
