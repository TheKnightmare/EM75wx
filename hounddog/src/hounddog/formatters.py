from __future__ import annotations

import re


def compact(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def ghostnet_message(claim, observations, *, callsign: str, network: str, max_chars: int = 500) -> str:
    label = claim["confidence_label"]
    location = compact(claim["location"] or "LOCATION-NOT-STATED").upper()
    title = compact(claim["title"]).upper()
    sources = len({row["source_family"] for row in observations})
    official = len({row["source_family"] for row in observations if row["source_type"] == "official"})
    suffix = f" SRC-FAMILIES:{sources} OFFICIAL:{official} REVIEWED-BY:{callsign}"
    prefix = f"@{network} {label}/{location} - "
    room = max(20, max_chars - len(prefix) - len(suffix))
    if len(title) > room:
        title = title[: room - 3].rstrip() + "..."
    return prefix + title + suffix


def evidence_summary(claim, observations) -> str:
    lines = [
        f"CLAIM {claim['id']} | {claim['confidence_label']} | SCORE {claim['significance_score']} | {claim['status']}",
        f"{claim['title']}",
        f"LOCATION: {claim['location'] or 'not stated'}",
        f"INDEPENDENT SOURCE FAMILIES: {claim['independent_families']} (OFFICIAL: {claim['official_families']})",
    ]
    for row in observations:
        lines.append(f"- [{row['source_type']}/{row['source_family']}] {row['source_name']}: {row['url']}")
    return "\n".join(lines)

