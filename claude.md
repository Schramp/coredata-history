# coredata-history

Decode Apple Core Data Persistent History Tracking into a forensic timeline.

See **README.md** for the full manual, forensic reference, and novelty scan.

## What it does

Reads any Core Data SQLite store with the persistent-history tables
(`ACHANGE`, `ATRANSACTION`, `ATRANSACTIONSTRING`) and produces a flat,
timestamped, attributed change log — one row per changed managed object.

Tested against: `Photos.sqlite`, `NoteStore.sqlite`, `healthdb`.

## Quick start

```bash
# Full change log as CSV + session timeline
python3 coredata_history.py Photos.sqlite -o changes.csv --summary

# Deletions with recovered tombstone values
python3 coredata_history.py Photos.sqlite --deletes

# Convert timestamps to a local timezone
python3 coredata_history.py Photos.sqlite --summary --timezone Europe/Amsterdam

# Override the label column for a specific entity
python3 coredata_history.py Photos.sqlite --label Asset=ZGENERICASSET.ZFILENAME
```

See README.md §2 for the full CLI reference.

## Output columns

`timestamp_utc`, `change` (INSERT/UPDATE/DELETE), `entity`, `entity_pk`,
`label`, `updated_columns`, `txn_id`, plus one column per author field
(`author`, `bundleid`, `contextname`, `processid`) and one per tombstone column.

See README.md §3 for full column semantics.

## Key forensic notes

- **Read-only**: opened with `immutable=1` — source file is never modified.
- **No dependencies**: Python 3 standard library only.
- **Timestamps**: Cocoa epoch (2001-01-01 UTC); see README.md §4.1.
- **Tombstones**: deleted-object values preserved in `ZTOMBSTONE*` columns; see README.md §4.5.
- **Pruning caveat**: Core Data trims history; absence of a record ≠ event never occurred; see README.md §5.3.

See README.md §5 for full forensic considerations and caveats.

## Development

```bash
pip install ruff
ruff check coredata_history.py
python3 coredata_history.py examples/2026-06-17/Photos.sqlite --summary
```

CI (`.github/workflows/ci.yml`) runs lint + regression tests on every push and PR.

## Background

- `initial_chat_transcript.md` — original literature review and design discussion
- README.md §7 — novelty scan and prior art (iLEAPP, Koenig, Tsai, Apple docs)
