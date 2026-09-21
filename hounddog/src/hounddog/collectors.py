from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from .model import Observation, SourceConfig, iso_utc


class CollectionError(RuntimeError):
    pass


def fetch_bytes(url: str, *, user_agent: str, timeout: int, max_bytes: int) -> bytes:
    if not url.lower().startswith("https://"):
        raise CollectionError(f"refusing non-HTTPS source: {url}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json, application/geo+json, application/rss+xml, application/xml, text/xml;q=0.9"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise CollectionError(f"response exceeds {max_bytes} bytes")
            data = response.read(max_bytes + 1)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise CollectionError(str(exc)) from exc
    if len(data) > max_bytes:
        raise CollectionError(f"response exceeds {max_bytes} bytes")
    return data


def _json(data: bytes):
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"invalid JSON: {exc}") from exc


def _ms_epoch(value) -> str:
    try:
        return iso_utc(datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc))
    except (TypeError, ValueError, OSError):
        return iso_utc(None)


def parse_nws(source: SourceConfig, data: bytes) -> list[Observation]:
    payload = _json(data)
    observations = []
    for feature in payload.get("features", []):
        props = feature.get("properties", {})
        external_id = props.get("id") or feature.get("id") or props.get("@id") or props.get("sent") or props.get("headline")
        observations.append(
            Observation(
                source_id=source.id,
                source_name=source.name,
                source_type=source.source_type,
                source_family=source.source_family,
                external_id=str(external_id),
                title=props.get("headline") or props.get("event") or "NWS alert",
                body=props.get("description") or props.get("instruction") or "",
                url=props.get("@id") or feature.get("id") or source.url,
                category="weather",
                location=props.get("areaDesc") or "",
                severity=(props.get("severity") or "unknown").lower(),
                published_at=iso_utc(props.get("sent") or props.get("effective")),
                raw=feature,
            )
        )
    return observations


def parse_usgs(source: SourceConfig, data: bytes) -> list[Observation]:
    payload = _json(data)
    observations = []
    for feature in payload.get("features", []):
        props = feature.get("properties", {})
        magnitude = props.get("mag")
        severity = "extreme" if isinstance(magnitude, (int, float)) and magnitude >= 7 else "severe" if isinstance(magnitude, (int, float)) and magnitude >= 6 else "moderate"
        observations.append(
            Observation(
                source_id=source.id,
                source_name=source.name,
                source_type=source.source_type,
                source_family=source.source_family,
                external_id=str(feature.get("id") or props.get("code") or props.get("time")),
                title=props.get("title") or f"Magnitude {magnitude} earthquake",
                body=f"Magnitude {magnitude}; tsunami flag {props.get('tsunami', 0)}; felt reports {props.get('felt', 0)}.",
                url=props.get("url") or source.url,
                category="earthquake",
                location=props.get("place") or "",
                severity=severity,
                published_at=_ms_epoch(props.get("time")),
                raw=feature,
            )
        )
    return observations


def parse_swpc(source: SourceConfig, data: bytes) -> list[Observation]:
    payload = _json(data)
    observations = []
    for item in payload if isinstance(payload, list) else []:
        product_code = item.get("product_id") or item.get("message_id") or "SWPC"
        product_id = f"{product_code}|{item.get('issue_datetime') or item.get('message', '')[:80]}"
        message = (item.get("message") or "").strip()
        first_line = next((line.strip() for line in message.splitlines() if line.strip()), "NOAA SWPC alert")
        observations.append(
            Observation(
                source_id=source.id,
                source_name=source.name,
                source_type=source.source_type,
                source_family=source.source_family,
                external_id=product_id,
                title=first_line[:240],
                body=message,
                url=source.url,
                category="space-weather",
                location="GLOBAL",
                severity="moderate",
                published_at=iso_utc(item.get("issue_datetime")),
                raw=item,
            )
        )
    return observations


def _node_text(node: ET.Element, local_name: str) -> str:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1].lower() == local_name.lower():
            return (child.text or "").strip()
    return ""


def parse_rss(source: SourceConfig, data: bytes) -> list[Observation]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise CollectionError(f"invalid XML: {exc}") from exc
    items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    observations = []
    for item in items:
        title = _node_text(item, "title") or "Untitled report"
        body = _node_text(item, "description") or _node_text(item, "summary") or _node_text(item, "content")
        link = _node_text(item, "link")
        if not link:
            for child in item:
                if child.tag.rsplit("}", 1)[-1].lower() == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        external_id = _node_text(item, "guid") or _node_text(item, "id") or link or title
        date_text = _node_text(item, "pubDate") or _node_text(item, "published") or _node_text(item, "updated")
        try:
            published = iso_utc(parsedate_to_datetime(date_text)) if date_text else iso_utc(None)
        except (TypeError, ValueError, OverflowError):
            published = iso_utc(date_text)
        observations.append(
            Observation(
                source_id=source.id,
                source_name=source.name,
                source_type=source.source_type,
                source_family=source.source_family,
                external_id=external_id,
                title=title,
                body=body,
                url=link or source.url,
                category=source.category,
                published_at=published,
                raw={"title": title, "body": body, "link": link, "published": date_text},
            )
        )
    return observations


PARSERS = {
    "nws_alerts": parse_nws,
    "usgs_geojson": parse_usgs,
    "swpc_alerts": parse_swpc,
    "rss": parse_rss,
}


def collect(source: SourceConfig, *, user_agent: str, timeout: int, max_bytes: int) -> list[Observation]:
    parser = PARSERS.get(source.kind)
    if not parser:
        raise CollectionError(f"unknown collector kind: {source.kind}")
    return parser(source, fetch_bytes(source.url, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes))
