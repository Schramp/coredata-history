# coredata-history

Decode Apple Core Data Persistent History Tracking into a forensic timeline.

## What it does

Reads any Core Data SQLite store that has the persistent-history tables
(`ACHANGE`, `ATRANSACTION`, `ATRANSACTIONSTRING`) and produces a flat,
timestamped change log — one row per changed managed object.

Tested against: `Photos.sqlite`, `NoteStore.sqlite`, `healthdb`.

## Usage

```bash
# Full change log as CSV
python3 coredata_history.py LIBRARY.sqlite -o changes.csv

# Session-grouped timeline summary
python3 coredata_history.py LIBRARY.sqlite --summary

# Deletions with recovered tombstone values
python3 coredata_history.py LIBRARY.sqlite --deletes

# Convert timestamps to a local timezone
python3 coredata_history.py LIBRARY.sqlite --summary --timezone Europe/Amsterdam

# Override the label column for a specific entity
python3 coredata_history.py LIBRARY.sqlite --label Asset=ZGENERICASSET.ZFILENAME
```

## Output columns

`timestamp_utc`, `change` (INSERT/UPDATE/DELETE), `entity`, `entity_pk`,
`label`, `txn_id`, plus one column per transaction author field
(`author`, `bundleid`, …) and one per tombstone column.

## Development

```bash
pip install ruff
ruff check coredata_history.py
python3 coredata_history.py examples/2026-06-17/Photos.sqlite --summary
```

CI runs on every push and PR (`.github/workflows/ci.yml`):
lint with `ruff` + regression tests against the example database.

## Background

See `initial_chat_transcript.md` for the original literature review on
`ACHANGE` / `ATRANSACTION` / `ATRANSACTIONSTRING` that motivated this tool.
