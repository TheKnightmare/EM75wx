from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from .model import Observation, token_set, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    confidence_label TEXT NOT NULL DEFAULT 'UNVERIFIED',
    confidence_score INTEGER NOT NULL DEFAULT 0,
    significance_score INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL DEFAULT 0,
    independent_families INTEGER NOT NULL DEFAULT 0,
    official_families INTEGER NOT NULL DEFAULT 0,
    UNIQUE(fingerprint)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_family TEXT NOT NULL,
    external_id TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    url TEXT NOT NULL,
    category TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'unknown',
    published_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, significance_score DESC, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_observations_claim ON observations(claim_id);
"""


@contextmanager
def connect(path: str):
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _similar_claim(connection: sqlite3.Connection, observation: Observation, hours: int = 72) -> int | None:
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    rows = connection.execute(
        "SELECT id, title, location FROM claims WHERE category = ? AND last_seen >= ? ORDER BY last_seen DESC LIMIT 200",
        (observation.category, cutoff),
    ).fetchall()
    incoming = token_set(f"{observation.title} {observation.location}")
    if not incoming:
        return None
    best_id, best_score = None, 0.0
    for row in rows:
        existing = token_set(f"{row['title']} {row['location']}")
        union = incoming | existing
        score = len(incoming & existing) / len(union) if union else 0.0
        if score > best_score:
            best_id, best_score = row["id"], score
    return best_id if best_score >= 0.68 else None


def _significance(severities: list[str], title: str, category: str) -> int:
    severity_points = {"unknown": 5, "minor": 10, "moderate": 35, "severe": 65, "extreme": 90}
    score = max([severity_points.get(item.lower(), 5) for item in severities] or [5])
    text = title.lower()
    high = ["evacuat", "outage", "explosion", "earthquake", "tornado", "wildfire", "cyber", "pipeline", "refinery", "emergency", "tsunami"]
    score += min(20, sum(5 for word in high if word in text))
    if category in {"chatter", "general"}:
        score -= 10
    return max(0, min(100, score))


def recompute_claim(connection: sqlite3.Connection, claim_id: int) -> None:
    rows = connection.execute(
        "SELECT source_type, source_family, severity, title FROM observations WHERE claim_id = ?",
        (claim_id,),
    ).fetchall()
    families = {row["source_family"] for row in rows}
    official = {row["source_family"] for row in rows if row["source_type"] == "official"}
    community = {row["source_family"] for row in rows if row["source_type"] == "community"}
    if official and len(families) >= 2:
        label, confidence = "CONFIRMED", 90
    elif official:
        label, confidence = "OFFICIAL-REPORT", 75
    elif len(families) >= 2:
        label, confidence = "CORROBORATED", 60
    elif community:
        label, confidence = "UNVERIFIED", 20
    else:
        label, confidence = "REPORTED", 35
    title = rows[0]["title"] if rows else ""
    claim = connection.execute("SELECT category FROM claims WHERE id = ?", (claim_id,)).fetchone()
    significance = _significance([row["severity"] for row in rows], title, claim["category"])
    connection.execute(
        """UPDATE claims SET observation_count=?, independent_families=?, official_families=?,
        confidence_label=?, confidence_score=?, significance_score=? WHERE id=?""",
        (len(rows), len(families), len(official), label, confidence, significance, claim_id),
    )


def ingest(connection: sqlite3.Connection, observation: Observation) -> tuple[int, bool]:
    existing = connection.execute(
        "SELECT claim_id FROM observations WHERE content_hash = ? OR (source_id = ? AND external_id = ?)",
        (observation.content_hash, observation.source_id, observation.external_id),
    ).fetchone()
    if existing:
        return existing["claim_id"], False
    claim = connection.execute("SELECT id FROM claims WHERE fingerprint = ?", (observation.event_fingerprint,)).fetchone()
    claim_id = claim["id"] if claim else _similar_claim(connection, observation)
    if claim_id is None:
        cursor = connection.execute(
            """INSERT INTO claims(fingerprint,title,category,location,first_seen,last_seen)
            VALUES(?,?,?,?,?,?)""",
            (observation.event_fingerprint, observation.title, observation.category, observation.location, observation.observed_at, observation.observed_at),
        )
        claim_id = cursor.lastrowid
    connection.execute(
        """INSERT INTO observations(claim_id,source_id,source_name,source_type,source_family,external_id,
        content_hash,title,body,url,category,location,severity,published_at,observed_at,raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            claim_id, observation.source_id, observation.source_name, observation.source_type,
            observation.source_family, observation.external_id, observation.content_hash,
            observation.title, observation.body, observation.url, observation.category,
            observation.location, observation.severity, observation.published_at,
            observation.observed_at, observation.raw_json(),
        ),
    )
    connection.execute("UPDATE claims SET last_seen=? WHERE id=?", (observation.observed_at, claim_id))
    recompute_claim(connection, claim_id)
    return claim_id, True


ALLOWED_TRANSITIONS = {
    "NEW": {"CORRELATING", "REVIEW", "REJECTED"},
    "CORRELATING": {"REVIEW", "REJECTED"},
    "REVIEW": {"TX_CANDIDATE", "REJECTED"},
    "TX_CANDIDATE": {"SENT", "REJECTED", "REVIEW"},
    "SENT": set(),
    "REJECTED": {"REVIEW"},
}


def transition(connection: sqlite3.Connection, claim_id: int, new_status: str) -> None:
    row = connection.execute("SELECT status FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not row:
        raise ValueError(f"claim {claim_id} not found")
    new_status = new_status.upper()
    if new_status not in ALLOWED_TRANSITIONS.get(row["status"], set()):
        raise ValueError(f"invalid transition {row['status']} -> {new_status}")
    connection.execute("UPDATE claims SET status=? WHERE id=?", (new_status, claim_id))


def queue(connection: sqlite3.Connection, minimum_score: int = 0, limit: int = 50):
    return connection.execute(
        """SELECT * FROM claims WHERE status NOT IN ('SENT','REJECTED') AND significance_score >= ?
        ORDER BY significance_score DESC, confidence_score DESC, last_seen DESC LIMIT ?""",
        (minimum_score, limit),
    ).fetchall()


def claim_detail(connection: sqlite3.Connection, claim_id: int):
    claim = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not claim:
        raise ValueError(f"claim {claim_id} not found")
    observations = connection.execute(
        "SELECT * FROM observations WHERE claim_id=? ORDER BY published_at, id", (claim_id,)
    ).fetchall()
    return claim, observations

