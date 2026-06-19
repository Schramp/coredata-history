# `coredata_history.py` — Manual, Forensic Reference, and Novelty Scan

**Tool:** `coredata_history.py`
**Purpose:** Decode the Core Data *Persistent History Tracking* log (`ACHANGE` /
`ATRANSACTION` / `ATRANSACTIONSTRING`) of any Apple Core Data SQLite store into a
timestamped, attributed, record-level change timeline.
**Dependencies:** Python 3 standard library only (no third-party packages).
**Document date:** 18 June 2026.

---

## 1. Overview

Apple's Core Data framework can keep a transactional change log inside the same
SQLite file as the data it manages. When *Persistent History Tracking* (PHT) is
enabled, every commit to the store is recorded across three tables:

| Table | Role |
|-------|------|
| `ATRANSACTION` | One row per committed save — the "who / when / where" envelope (timestamp, author, bundle ID, context name, process). |
| `ACHANGE` | One row per changed managed object per transaction — the "what" (insert / update / delete, which entity, which row, tombstones of deleted values). |
| `ATRANSACTIONSTRING` | An interned string pool that de-duplicates the author / bundle / process / context strings referenced by `ATRANSACTION`. |

These tables exist in *any* PHT-enabled Core Data store — Apple Photos
(`Photos.sqlite`), and other first- and third-party apps built on Core Data.
This tool reconstructs the change timeline from them **offline**, without the
original `.xcdatamodel`, without the live Core Data runtime, and without
modifying the file.

---

## 2. Installation & invocation

No installation step is required beyond a Python 3 interpreter.

```bash
python3 coredata_history.py <database.sqlite> [options]
```

### 2.1 Command-line options

| Option | Description |
|--------|-------------|
| `database` (positional) | Path to the Core Data `.sqlite` store. |
| `-o`, `--out PATH` | Write the full decoded change log to a CSV at `PATH`. |
| `--summary` | Print a session-grouped timeline summary to stdout. |
| `--deletes` | Print every deletion together with the values its tombstones preserved. |
| `--session-gap MIN` | Minutes of inactivity that start a new session in `--summary` (default 30). |
| `--label ENTITY=TABLE.COLUMN` | Override the column used to label a given entity's records. Repeatable. |
| `--property-map ENTITY=p0,p1,...` | Authoritative ordered property list for an entity, used to decode the `ZCOLUMNS` updated-column bitmap exactly. Repeatable. Without it, decoding is best-effort (§4.6). |
| `--no-auto-label` | Disable best-effort automatic record labelling. |

### 2.2 Typical workflows

```bash
# Full change log to CSV + on-screen session timeline
python3 coredata_history.py Photos.sqlite -o changes.csv --summary

# Focus on deletions and recovered tombstone values
python3 coredata_history.py Photos.sqlite --deletes

# Force the human label for an entity whose backing table is non-obvious
python3 coredata_history.py Photos.sqlite --label Asset=ZGENERICASSET.ZFILENAME
```

---

## 3. Output reference

### 3.1 CSV change log (`--out`)

One row per change, ordered by transaction time then change primary key.

| Column | Meaning |
|--------|---------|
| `timestamp_utc` | Commit time of the transaction, UTC (`YYYY-MM-DD HH:MM:SS`). |
| `change` | `INSERT`, `UPDATE`, or `DELETE`. |
| `entity` | Core Data entity name (e.g. `Asset`, `PhotosHighlight`). |
| `entity_pk` | Primary key (`Z_PK`) of the affected row in that entity's backing table. |
| `label` | Best-effort human label for the record (e.g. filename), if resolvable. |
| `updated_columns` | For UPDATEs, the names of the changed properties decoded from the `ZCOLUMNS` bitmap, separated by `;` (best-effort — see §4.6). Empty for inserts/deletes. |
| `txn_id` | `ATRANSACTION.Z_PK` of the owning transaction. |
| *author fields* | One column per discovered author field — typically `author`, `bundleid`, `contextname`, `processid`. |
| `ZTOMBSTONE0..n` | Decoded tombstone values preserved for deleted objects (empty for inserts/updates). |

### 3.2 Session summary (`--summary`)

Groups changes into activity sessions (split when the inter-change gap exceeds
`--session-gap`) and tallies per session as `Entity CHANGEx<count>`, followed by
overall insert/update/delete totals.

### 3.3 Deletions report (`--deletes`)

Lists each `DELETE` with its timestamp, entity, record PK, label, authoring
process, and every non-empty tombstone column — the recovery channel for
data about objects that no longer exist as live rows.

---

## 4. Data-structure reference

This section documents the field semantics the tool relies on. Apple has not
published the schema of these tables; the semantics below are drawn from
Apple's public `NSPersistentHistory*` API surface, community reverse
engineering, and direct observation, and should be treated as well-evidenced
rather than vendor-confirmed (see §7).

### 4.1 Timestamps

`ATRANSACTION.ZTIMESTAMP` is a floating-point **Cocoa/Core Data absolute time**:
seconds since the reference epoch **2001-01-01 00:00:00 UTC**. The tool converts
it by adding the value as seconds to that epoch. (This differs from the Unix
epoch by 978,307,200 seconds.) Timestamps are emitted in UTC; apply the
examiner's timezone offset downstream.

### 4.2 Change types (`ACHANGE.ZCHANGETYPE`)

Maps to `NSPersistentHistoryChangeType`:

| Value | Meaning |
|-------|---------|
| `0` | INSERT |
| `1` | UPDATE |
| `2` | DELETE |

### 4.3 Entity and record identification

- `ACHANGE.ZENTITY` is a Core Data entity code. The tool resolves it to a name
  via `Z_PRIMARYKEY (Z_ENT → Z_NAME)`.
- `ACHANGE.ZENTITYPK` is the `Z_PK` of the affected row within that entity's
  backing table, allowing each change to be joined back to live data (for
  inserts/updates) or recognised as orphaned (for deletes).
- The backing table for an entity is **not** named in the schema and does not
  always follow the `Z<ENTITYNAME>` convention (inheritance hierarchies are
  stored under the root entity's table — e.g. Photos stores the `Asset` entity
  in `ZGENERICASSET`). The tool therefore discovers the mapping by scanning
  every `Z*` table that has both `Z_PK` and `Z_ENT` columns and recording which
  entity codes appear in each — a model-free resolution that handles these
  quirks automatically.

### 4.4 Author attribution (`ATRANSACTION`)

PHT records, per transaction, the `author`, `bundleID`, `contextName`, and
`processID` exposed by `NSPersistentHistoryTransaction`. In the on-disk schema
these appear as paired columns: an interned-reference integer column ending in
`TS` (e.g. `ZAUTHORTS`) that points into `ATRANSACTIONSTRING.Z_PK`, plus an
inline `VARCHAR` fallback with the suffix stripped (e.g. `ZAUTHOR`). The tool
discovers these pairs dynamically, resolves the interned reference first, and
falls back to the inline value.

### 4.5 Tombstones (`ACHANGE.ZTOMBSTONE0..n`)

For attributes flagged `preserveValueOnDeletionInPersistentHistory`, Core Data
copies the pre-deletion value into a tombstone column so the change record still
describes the object after its live row is gone. The number of tombstone
columns varies by store; the tool discovers all `ZTOMBSTONE\d+` columns and
renders printable values directly and binary values as a size marker. Note that
attributes using `allowsExternalBinaryDataStorage` are not tombstoned.

### 4.6 Updated-property bitmap (`ACHANGE.ZCOLUMNS`)

For UPDATEs, `ZCOLUMNS` is a bitmap of which properties changed (corresponding
to `NSPersistentHistoryChange.updatedProperties`). It is null for inserts and
deletes. The tool decodes it into the `updated_columns` output field. The
decoding rests on observed, reverse-engineered conventions and is **best-effort**;
the important caveats below should accompany any reliance on it:

- **Bit order.** Bits are read least-significant-first within each byte; bit
  `i` of byte `b` is property index `b*8 + i`.
- **Index space is the *property* list, not the physical columns.** Core Data
  assigns every property (attribute *and* relationship) an index and orders them
  alphabetically. Attributes and to-one relationships are stored as physical
  columns (in that same alphabetical order); the tool uses those columns as its
  index space. Observed bitmaps are wider than the physical column count
  (e.g. an Apple Photos `Asset` bitmap is 15 bytes / 120 bits against 96
  attribute-and-relationship columns), confirming the bitmap also indexes
  **to-many relationships**, which have no physical column.
- **Consequence.** Because to-many relationships occupy index slots that the
  schema does not expose, exact name resolution is not guaranteed from the
  SQLite file alone — a to-many relationship sorting early in the alphabet can
  shift the indices of later properties. The tool therefore maps each set bit to
  the physical column at that index when in range, and otherwise emits a raw
  `#<index>` token so that nothing is silently mislabelled. In validation against
  an Apple Photos store, roughly 88% of update tokens resolved to named columns
  and ~11% were emitted as raw indices.
- **Exact decoding.** Supplying the entity's authoritative ordered property list
  via `--property-map ENTITY=prop0,prop1,...` (e.g. extracted from the compiled
  `.momd` data model) makes the mapping exact for that entity.
- **Reporting guidance.** Treat named columns as investigative leads to be
  corroborated, not as proof, unless an authoritative property map was supplied.

---

## 5. Forensic considerations

### 5.1 Evidentiary soundness

- **Read-only / immutable access.** The store is opened with SQLite's
  `immutable=1` URI parameter (falling back to `mode=ro`). No write-ahead log,
  shared-memory, or journal file is created, and the source bytes are not
  modified. Standard practice still applies: operate on a verified working copy
  and record hashes before and after.
- **Deterministic, inspectable logic.** The decoding is pure-Python, dependency
  free, and the SQL it issues is reconstructable from the source — supporting
  reproducibility and explanation in a report or testimony.

### 5.2 Evidentiary value

- **Timeline reconstruction.** Inserts mark when a record (e.g. a photo) first
  entered the store; updates mark subsequent modifications; deletes mark removal
  — each with a UTC timestamp.
- **Attribution.** Each change carries the authoring process / bundle / context,
  distinguishing system-daemon activity from other actors.
- **Deleted-object recovery.** Tombstones can preserve identifiers (e.g. a
  deleted asset's UUID) that survive the live row and can be pivoted against
  thumbnails, backups, cloud records, or other databases.

### 5.3 Interpretation caveats (must be stated in any report)

1. **Pruning.** Core Data routinely trims the log via
   `deleteHistory(before:)`. The earliest surviving entry is therefore **not**
   necessarily the store's true origin, and the absence of a change is **not**
   proof the event never occurred. Interned strings in `ATRANSACTIONSTRING` can
   outlive the transactions that referenced them, so a populated string pool
   alongside sparse `ACHANGE`/`ATRANSACTION` rows is consistent with pruning.
2. **Commit time ≠ user-action time.** A timestamp records when the *authoring
   process committed* the change (an import, an analysis pass, a deletion),
   which is close to but not identical with the underlying user gesture.
3. **System authorship.** Many changes are authored by background daemons
   (e.g. Photos' `assetsd`) performing library maintenance, not by direct user
   action. Do not infer user intent from change volume alone.
4. **Entity vs. physical table.** The `entity` column is the logical Core Data
   entity name, which may differ from the physical SQLite table name (§4.3).
5. **Semantics are reverse-engineered.** The field meanings in §4 are not
   formally documented by Apple (§7). Corroborate critical findings against
   independent artifacts.
6. **Page-level carving is separate.** Records pruned from these tables may
   still reside in SQLite freelist pages, WAL, or unallocated space; recovering
   those requires SQLite carving tooling and is outside this tool's scope.

### 5.4 Validation performed

The tool was validated against an iOS `Photos.sqlite` (Core Data, iOS 14-era
schema; 1,533 `ACHANGE`, 499 `ATRANSACTION`, 23 `ATRANSACTIONSTRING` rows). It
reproduced a previously hand-built timeline exactly: per-asset insert times, a
complete delete event (one asset plus its attribute and two backing-resource
rows in a single transaction), and recovery of the deleted asset's UUID from its
tombstone. Examiners should perform their own validation against known-state
test data for the OS version and app under examination.

---

## 6. Known limitations / roadmap

- **Field-level changes are decoded best-effort.** `ZCOLUMNS` (which properties
  an UPDATE changed) is now decoded into the `updated_columns` field, but exact
  name resolution is not guaranteed without the data model because to-many
  relationships occupy bitmap indices that the SQLite schema does not expose
  (§4.6). Unresolved indices are emitted as `#<index>`; `--property-map` makes a
  given entity exact.
- **Physical table name not emitted.** Resolved internally for labelling but not
  surfaced as a CSV column.
- **No page-level / WAL carving.** Operates only on live rows of the history
  tables.
- **Best-effort labelling.** Auto-labels use common column names; unusual
  schemas may need `--label` overrides.
- **Schema drift.** Column layouts vary across OS/Core Data versions; dynamic
  discovery mitigates but does not guarantee coverage of every future variant.
- **Validation**. The method needs to be experimentally validated agains some
  common databases. E.g. Photos.sqlite would seem te be a good candidate.

---

## 7. Novelty scan and prior art

### 7.1 Method

The scan surveyed: Apple's public Core Data / `NSPersistentHistory` API and
developer documentation; the iOS-forensics practitioner literature and the
open-source iLEAPP parser project; developer reverse-engineering write-ups; and
general SQLite-forensics tooling. The objective was to locate any existing
**offline, model-free, cross-store extractor** that turns the Core Data
persistent-history log into an attributed change timeline, and to attribute the
ideas this tool builds on.

### 7.2 Prior art

**A. Apple's first-party API (the canonical mechanism).**
Persistent History Tracking was introduced at WWDC 2017 (iOS 11). Apps read the
log through `NSPersistentHistoryChangeRequest` / `NSPersistentHistoryTransaction`
/ `NSPersistentHistoryChange`, and prune it with `deleteHistory(before:)`. This
is a *runtime* API: it requires the live Core Data stack and the app's data
model, and is designed for app/extension synchronisation — not offline forensic
parsing of a seized SQLite file.

**B. Developer reverse-engineering of the on-disk tables.**
Michael Tsai (2019) documented the internals that this tool depends on: the
history lives in the same SQLite file under `A`-prefixed tables; a single column
stores primary keys for different entity types; the tables are maintained by
SQLite triggers that fire on all changes (including batch operations); and
tombstone columns hold preserved pre-deletion values. Developer guides
(fatbobman; SwiftLee/Antoine van der Lee; Apple WWDC) describe `ATRANSACTION` /
`ACHANGE` / `ATRANSACTIONSTRING` roles, the Cocoa timestamp, and string
interning. fatbobman's open-source *PersistentHistoryTrackingKit* manages and
cleans history but, again, through Apple's runtime API inside a live app.

**C. iOS Photos forensics (the closest applied domain).**
The most developed body of `Photos.sqlite` forensic work — Scott Koenig (*The
Forensic Scooter*) and the open-source iLEAPP project (Alexis Brignoni) —
provides extensive parsers (e.g. iLEAPP's Ph1–Ph30) covering asset, album,
share, and analysis artifacts. Crucially, these target the **current-state**
tables (`ZASSET`/`ZGENERICASSET`, `ZADDITIONALASSETATTRIBUTES`, `ZSHARE`,
`ZGENERICALBUM`, etc.). Koenig's published documentation explicitly notes that
`ACHANGE`, `ATRANSACTION`, and `ATRANSACTIONSTRING` *contained data but were
deliberately excluded* from the queries, and that he had not yet researched
their contents and would not speculate on them. The persistent-history tables
are thus a recognised but largely **unworked gap** in applied iOS forensics.

**D. General SQLite forensics.**
Tooling for recovering deleted SQLite records from freelist pages, WAL, and
unallocated space (e.g. Sanderson's Forensic Toolkit/Browser for SQLite, and the
broader "SQLite gaps / rowid recovery" technique discussed in the community)
operates at the storage-page level. It is complementary to — and orthogonal
from — interpreting the Core Data history *schema*.

### 7.3 What appears novel here (relative to surveyed prior art)

Subject to the limits of an open-source survey (closed-source commercial suites
such as Cellebrite or Magnet AXIOM may implement undisclosed equivalents), the
following combination was not found in the prior art:

1. **Offline, model-free extraction.** Reconstructing the history timeline
   directly from the SQLite file without the `.xcdatamodel` or the live Core
   Data runtime — the inverse of Apple's runtime-only API (Prior art A).
2. **Store-agnostic, schema-discovering design.** Dynamic discovery of author
   fields, tombstone columns, and — notably — entity-code-to-backing-table
   mapping by scanning `Z_ENT` membership, which generalises beyond Photos and
   resolves inheritance quirks (e.g. `Asset → ZGENERICASSET`) with no
   per-app configuration. Existing forensic parsers (Prior art C) are
   app-specific and current-state-oriented.
3. **History-as-timeline output with attribution and tombstone recovery.**
   Emitting a per-record insert/update/delete timeline carrying authoring
   process/bundle/context and surfacing tombstone-preserved identifiers for
   deleted objects — directly filling the gap Koenig flagged (Prior art C).

The tool does **not** claim novel discovery of the tables themselves, of their
forensic relevance (credited to Koenig), or of their internal semantics
(credited to Tsai and Apple's API). Its contribution is the generalised,
offline, schema-discovering extraction-and-timelining method.

### 7.4 References

The references are grouped by the role they play in this document, because they
serve two distinct purposes that should not be conflated. **Group A** sources
establish the *semantics* the tool relies on — they decode the structure and
meaning of these tables, but in a software-development context, not a forensic
one. **Group B** sources are the *forensic prior art* — and the salient point is
what they do **not** do: the published forensic literature on this exact database
flags these tables yet leaves them undecoded. No surveyed reference in either
group performs offline, forensic timeline reconstruction from the
persistent-history tables; that gap is the basis for the novelty assessment in
§7.3.

A scope note: this survey covers open, published sources only. Closed commercial
forensic suites and non-public training material were not inspectable and may
treat these tables in ways not reflected here (see §7.3).

#### Group A — Semantics attribution (developer / reverse-engineering; not forensic)

These decode the tables' structure and field meaning, but for building apps
(synchronisation, change-merging), not for forensic analysis.

A1. Apple — Persistent History Tracking / `NSPersistentHistoryChangeRequest`,
   `NSPersistentHistoryTransaction`, `NSPersistentHistoryChange`,
   `NSPersistentHistoryChangeType`, `preserveValueOnDeletionInPersistentHistory`.
   Apple Developer Documentation; WWDC 2017 "What's New in Core Data."
   *Used for:* the change-type enum, tombstone preservation, the public object
   model these tables back. *Forensic decoding:* none — defines the runtime API,
   not the on-disk tables.

A2. Michael Tsai — "Persistent History Tracking in Core Data" (2019)
   https://mjtsai.com/blog/2019/08/21/persistent-history-tracking-in-core-data/ ;
   "Turning Off Core Data Persistent History Tracking" (2023)
   https://mjtsai.com/blog/2023/08/15/turning-off-core-data-persistent-history-tracking/
   *Used for:* the closest prior decoding of the on-disk internals — the
   differently-prefixed tables in the same file, the single column storing
   primary keys for multiple entity types, trigger-based maintenance covering
   batch operations, and tombstone columns. *Forensic decoding:* none — a
   developer reverse-engineering write-up.

A3. fatbobman — "Using Persistent History Tracking in CoreData" (2021)
   https://fatbobman.com/en/posts/persistenthistorytracking/ ;
   *PersistentHistoryTrackingKit* https://github.com/fatbobman/PersistentHistoryTrackingKit
   *Used for:* the `ATRANSACTION` / `ACHANGE` / `ATRANSACTIONSTRING` role split,
   the directly-readable Cocoa timestamp, string interning, and pruning behaviour
   via `deleteHistory(before:)`. *Forensic decoding:* none — a developer guide
   and a runtime management library.

A4. Antoine van der Lee (SwiftLee) — "Persistent History Tracking in Core Data" (2020).
   https://www.avanderlee.com/swift/persistent-history-tracking-core-data/
   *Used for:* author / context-name attribution semantics. *Forensic decoding:*
   none — a developer usage guide.

#### Group B — Forensic prior art (does not decode these tables)

The applied iOS-forensics work closest to this domain. Its relevance is the
documented *absence* of persistent-history decoding.

B1. Scott Koenig (The Forensic Scooter) — "Local Photo Library Photos.sqlite
   Query Documentation & Notable Artifacts" (2022)
   https://theforensicscooter.com/2022/05/02/photos-sqlite-query-documentation-notable-artifacts/ ;
   "Photos.sqlite Update" (2022)
   https://theforensicscooter.com/2022/02/21/photos-sqlite-update/ ;
   "iLEAPP Parsers & Photos.sqlite Queries" (2024)
   https://theforensicscooter.com/2024/05/18/ileapp-parsers-photos-sqlite-queries/
   *Relevance:* the most developed `Photos.sqlite` forensic work; targets
   current-state tables. Explicitly records that `ACHANGE`, `ATRANSACTION`, and
   `ATRANSACTIONSTRING` held data during testing but were excluded, and that
   their contents had not been researched. *Forensic decoding of these tables:*
   none (stated as future work).

B2. Alexis Brignoni et al. — iLEAPP (iOS Logs, Events, And Plists Parser).
   https://github.com/abrignoni/iLEAPP
   *Relevance:* the open-source standard for `Photos.sqlite` parsing (e.g.
   Ph1–Ph30). *Forensic decoding of these tables:* none — parsers operate on
   current-state tables (`ZASSET`/`ZGENERICASSET`, `ZSHARE`, `ZGENERICALBUM`, …).

B3. RealityNet — iOS Forensics References (curated index).
   https://github.com/RealityNet/iOS-Forensics-References
   *Relevance:* corroborates the landscape — indexes the Photos.sqlite forensic
   work but lists no persistent-history-table decoding. *Forensic decoding:* none
   (a reference directory).

#### Group C — Forensic decoding of these tables

None identified in the surveyed open literature.

---

## 8. Appendix — quick start

```bash
# 1. Work on a copy; record a hash.
sha256sum evidence_Photos.sqlite

# 2. Decode everything.
python3 coredata_history.py evidence_Photos.sqlite -o change_log.csv --summary --deletes

# 3. Confirm the source was untouched.
sha256sum evidence_Photos.sqlite   # unchanged
```
