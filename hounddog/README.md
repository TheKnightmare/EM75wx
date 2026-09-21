# Hound Dog

Hound Dog is KQ4ODY's provenance-first public-source collector for GhostNet traffic. It does **not** decide truth and it cannot transmit. It collects observations, preserves the original evidence, groups likely repeats into claims, distinguishes independent source families from mirrors, and puts concise candidates in front of a human operator.

> We do not determine truth. We collect claims, preserve provenance, seek independent corroboration, and help a human operator decide what is worth transmitting.

## What works in v0.1

- NWS active-alert JSON for TN, NC, KY, and GA
- USGS significant-earthquake GeoJSON
- NOAA SWPC alert JSON
- GDACS RSS
- Any operator-approved RSS/Atom feed, including community-monitoring feeds
- SQLite evidence store with original text and raw payloads
- Duplicate suppression and similarity correlation
- Independent-source-family counting so mirrors do not manufacture corroboration
- Confidence labels: `UNVERIFIED`, `REPORTED`, `CORROBORATED`, `OFFICIAL-REPORT`, `CONFIRMED`
- Significance scoring and the workflow `NEW → CORRELATING/REVIEW → TX_CANDIDATE → SENT/REJECTED`
- GhostNet-sized output with confidence and source-family counts attached

There is deliberately no JS8Call, VarAC, Winlink, CAT, serial, or PTT integration. `TX_CANDIDATE` is the end of the software pipeline. KQ4ODY makes the transmit decision.

## Install

Windows PowerShell, from this directory:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
hounddog init
```

Edit `config.toml` and replace the example address in `collection.user_agent` with a real contact. NWS asks API clients to identify themselves. Then run:

```powershell
hounddog run-once
hounddog queue --min-score 35
hounddog show 1
```

## Human review flow

```powershell
hounddog mark 1 review
hounddog show 1
hounddog format 1
hounddog mark 1 tx_candidate
# transmit manually only after reviewing the original sources
hounddog mark 1 sent
```

The state machine rejects `NEW → SENT`. A claim cannot be marked sent without passing through `REVIEW` and `TX_CANDIDATE`.

## Add chatter sources carefully

Copy an example `[[sources]]` block into `config.toml`. Use `source_type = "community"`. The key field is `source_family`: reposts, mirrors, screenshots, and sites repeating the same originating report must share one family. Two URLs are not automatically two sources.

Only configure feeds whose terms allow automated access. Prefer APIs and native RSS/Atom. Hound Dog refuses non-HTTPS collector URLs, limits response size, times out stalled requests, and never bypasses authentication.

## Schedule locally

Use Windows Task Scheduler to run this every 15 minutes while the PC is on:

```text
Program: C:\path\to\EM75wx\hounddog\.venv\Scripts\hounddog.exe
Arguments: --config C:\path\to\EM75wx\hounddog\config.toml run-once
Start in: C:\path\to\EM75wx\hounddog
```

Run `hounddog queue --min-score 35` when you sit down to operate. This keeps collection automated and RF editorial judgment human.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The tests use local fixtures and do not require Internet access.

## Near-term collectors

NASA FIRMS is next once a FIRMS `MAP_KEY` is available. IODA and aviation sources need source-specific rate-limit and attribution handling before they should be enabled. Forum collectors should use a permitted feed/API; Hound Dog will not ship fragile login scraping.

