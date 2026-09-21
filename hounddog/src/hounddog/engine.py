from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tomllib

from .collectors import CollectionError, collect
from .db import connect, ingest
from .model import SourceConfig


@dataclass(slots=True)
class Settings:
    database: str
    max_age_days: int
    timeout: int
    max_bytes: int
    user_agent: str
    callsign: str
    network: str
    region_label: str
    max_message_chars: int
    sources: list[SourceConfig]


def load_settings(path: str) -> Settings:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        payload = tomllib.load(handle)
    operator = payload.get("operator", {})
    collection = payload.get("collection", {})
    sources = [SourceConfig(**item) for item in payload.get("sources", [])]
    database = str(collection.get("database", "data/hounddog.db"))
    if not Path(database).is_absolute():
        database = str((config_path.parent / database).resolve())
    return Settings(
        database=database,
        max_age_days=int(collection.get("max_age_days", 7)),
        timeout=int(collection.get("request_timeout_seconds", 20)),
        max_bytes=int(collection.get("max_response_bytes", 5_000_000)),
        user_agent=str(collection.get("user_agent", "KQ4ODY-HoundDog/0.1")),
        callsign=str(operator.get("callsign", "KQ4ODY")),
        network=str(operator.get("network", "GHOSTNET")),
        region_label=str(operator.get("region_label", "ETN/WNC/TN/KY/GA/NC")),
        max_message_chars=int(operator.get("max_message_chars", 500)),
        sources=sources,
    )


def run_once(settings: Settings) -> dict:
    result = {"sources_ok": 0, "sources_failed": 0, "seen": 0, "inserted": 0, "errors": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.max_age_days)
    enabled = [source for source in settings.sources if source.enabled]
    collected: dict[str, tuple[SourceConfig, list]] = {}
    workers = max(1, min(8, len(enabled)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hounddog") as pool:
        futures = {
            pool.submit(
                collect,
                source,
                user_agent=settings.user_agent,
                timeout=settings.timeout,
                max_bytes=settings.max_bytes,
            ): source
            for source in enabled
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                collected[source.id] = (source, future.result())
                result["sources_ok"] += 1
            except CollectionError as exc:
                result["sources_failed"] += 1
                result["errors"].append(f"{source.id}: {exc}")
    with connect(settings.database) as connection:
        for configured_source in enabled:
            batch = collected.get(configured_source.id)
            if batch is None:
                continue
            _, observations = batch
            for observation in observations:
                try:
                    published = datetime.fromisoformat(observation.published_at.replace("Z", "+00:00"))
                except ValueError:
                    published = datetime.now(timezone.utc)
                if published < cutoff:
                    continue
                result["seen"] += 1
                _, inserted = ingest(connection, observation)
                result["inserted"] += int(inserted)
    return result
