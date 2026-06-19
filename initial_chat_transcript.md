
/

Photos (1).sqlite
sqlite

Do a literature review on the purpose and workings of  ACHANGE
 ATRANSACTION
 ATRANSACTIONSTRING

Jun 18

Identified database tables, planned forensic analysis and literature research
Identified database tables, planned forensic analysis and literature research
I'll approach this in two parts: first inspect the actual database to ground the analysis in your file's real schema, then pull together the forensics/technical literature on these structures. Let me start by checking how to read the uploaded file.


Deliberated on inspecting unfamiliar database components systematically
Deliberated on inspecting unfamiliar database components systematically
The skill routes SQLite as "use the right tool" — for a SQLite database that's the sqlite3 CLI. Let me inspect your actual file so the review is grounded in its real schema.


Pivoted toward Python's sqlite3 module alternative
Pivoted toward Python's sqlite3 module alternative
sqlite3 CLI isn't here, but Python has the module built in. Let me inspect the three tables directly.


Synthesizing architectural insights into transaction management systems
Synthesizing architectural insights into transaction management systems
Confirmed — this is an Apple Photos Core Data library with the three Persistent History Tracking tables present and populated (1,533 changes / 499 transactions / 23 distinct strings). Let me pull sample data to see how they actually interlink, then I'll bring in the forensics literature.


Synthesized database schema analysis and identified forensic literature sources
Synthesized database schema analysis and identified forensic literature sources
The file confirms the structure precisely. Now let me bring in the published literature — Apple's Core Data documentation and the digital-forensics research on these tables — to round out the review.


Synthesizing schema documentation and API references comprehensively
Synthesizing schema documentation and API references comprehensively
Good developer-side coverage. Let me get the digital-forensics angle specifically, since the change-type enum and tombstones are where the forensic value lies.


Synthesizing schema, data, and Core Data documentation comprehensively
Synthesizing schema, data, and Core Data documentation comprehensively
I have what I need. Let me do one targeted search on the forensic significance, since your file is specifically an iOS Photos library.


Synthesized technical documentation, forensic literature, and file data comprehensively
Synthesized technical documentation, forensic literature, and file data comprehensively
I inspected your file and confirmed all three tables are present and populated (1,533 rows in ACHANGE, 499 in ATRANSACTION, 23 in ATRANSACTIONSTRING), then cross-referenced the developer and forensic literature. Here's the review.

What these three tables collectively are
They are not Photos-specific. They are the standard schema that Core Data's Persistent History Tracking (PHT) writes into any SQLite-backed Core Data store — and the Apple Photos library (Photos.sqlite) is one such store. PHT was introduced at WWDC 2017 for iOS 11 as Apple's mechanism in Core Data for tracking changes. Its purpose is coordination: with persistent history tracking enabled, the app writes a transaction record for any change in the store, whether the change originates from an app extension, a background context, or elsewhere. Multiple "actors" — the foreground app, share/widget extensions, CloudKit sync, background daemons — can then replay the log and stay consistent. 
Medium
Morioh

Mechanically, the log is maintained below the Core Data API by SQLite triggers. As Michael Tsai documented from the undocumented internals, the tables are updated using SQLite triggers that are not directly exposed in Core Data, and because they fire on all database changes they also capture batch updates and deletions. That "fires on everything" property is exactly why the tables are forensically interesting — they record activity the normal object graph would not. 
Michael Tsai

The division of labour across the three tables is summarised well in the developer literature: ATRANSACTION holds the transactions not yet cleared, ATRANSACTIONSTRING holds the string identifiers for authors and context names, and ACHANGE holds the changed data. Below is each one, grounded in what's actually in your file. 
Fatbobman's Swift Weekly

ATRANSACTION — the "who / when / where" envelope
One row per committed save. Your file's schema:

Z_PK, Z_ENT, Z_OPT,
ZAUTHORTS, ZBUNDLEIDTS, ZCONTEXTNAMETS, ZPROCESSIDTS,   -- integer refs into ATRANSACTIONSTRING
ZTIMESTAMP FLOAT,                                        -- Cocoa/Mac absolute time
ZAUTHOR, ZBUNDLEID, ZCONTEXTNAME, ZPROCESSID VARCHAR,    -- inline string fallbacks
ZQUERYGEN BLOB                                           -- NSQueryGenerationToken
This maps directly onto the public NSPersistentHistoryTransaction object, whose properties are author, bundleID, contextName, processID, timestamp and token. The ZTIMESTAMP is the key artifact and needs no token math — as the developer write-ups note, the timestamp is recorded directly in ATRANSACTION and can be read without conversion, unlike the token. It's stored as Cocoa absolute time (seconds since 2001-01-01 UTC); decoding the first rows in your file gives commit times of 2024-11-12 09:34 and 2024-11-19 10:18. 
Fatbobman's Swift Weekly

The ...TS columns are integer foreign keys into ATRANSACTIONSTRING, while the parallel VARCHAR columns are an inline fallback (null in your file, because the strings are interned). In your data, ZAUTHORTS=1, ZBUNDLEIDTS=2, ZPROCESSIDTS=3 resolve to [0,1], com.apple.assetsd, and assetsd — i.e. these transactions were authored by assetsd, the iOS photo-library daemon, not by direct user edits. That attribution is the practical payoff of the table.

ACHANGE — the granular change log
One row per changed object per transaction (so a single transaction fans out into many ACHANGE rows). Your schema:

Z_PK, Z_ENT, Z_OPT,
ZCHANGETYPE INTEGER, ZENTITY INTEGER, ZENTITYPK INTEGER,
ZTRANSACTIONID INTEGER,                                  -- -> ATRANSACTION.Z_PK
ZCOLUMNS BLOB,                                           -- bitmap of updated properties
ZTOMBSTONE0..3 BLOB                                      -- preserved pre-deletion values
This maps to NSPersistentHistoryChange, whose public surface is changeType, changedObjectID, tombstone, and updatedProperties (the ZCOLUMNS bitmap). The fields work as follows:

ZCHANGETYPE is the NSPersistentHistoryChangeType enum: 0 = insert, 1 = update, 2 = delete. Your file breaks down as 324 inserts, 1,205 updates, 4 deletes.
ZENTITY + ZENTITYPK identify the affected object: ZENTITY is the Core Data entity code (joinable to Z_PRIMARYKEY for the name) and ZENTITYPK is that row's primary key. In your file the most-changed entities are Asset (538), AdditionalAssetAttributes (411), and PhotosHighlight (207) — so you can pivot a change straight back to a specific ZASSET row.
ZTRANSACTIONID joins each change to its ATRANSACTION envelope, inheriting that transaction's timestamp and author.
ZTOMBSTONE0..3 are the forensic gold. For attributes flagged preserveValueOnDeletionInPersistentHistory, Core Data copies the pre-deletion value into a tombstone so the change log still describes the object after it's gone — Tsai confirms setting an attribute to be preserved after deletion is what populates the tombstone, toggled by preserveValueOnDeletionInPersistentHistory. One documented limit: attributes using allowsExternalBinaryDataStorage are not tombstoned, even for small values stored in the database. 
Michael Tsai
Michael Tsai
ATRANSACTIONSTRING — the interned string pool
A small normalization table (Z_PK, Z_ENT, Z_OPT, ZNAME) that deduplicates the strings ATRANSACTION would otherwise repeat across thousands of rows — author tokens, bundle IDs, process names, context names. Its 23 entries in your file are a readable map of the Photos subsystems generating changes: com.apple.assetsd, assetsd, method-signature contexts like -[PLModelMigrator photoLibrary] and -[PLLibraryServicesManager newPhotoLibrary], and context names such as moment library, non-transient library, high/low priority transient library. These name the internal machinery (model migration, library services) rather than user gestures.

Why this matters in the forensic literature
Photos.sqlite is treated as a high-value iOS artifact because forensic examination of it yields information about the owner's visual record, whereabouts, and potentially deleted or hidden media, supporting timeline construction and verification of activity. These three tables specifically have been flagged but under-explored: Scott Koenig (The Forensic Scooter) notes that ACHANGE, ATRANSACTION and ATRANSACTIONSTRING are believed to contain a lot of information about asset activity within the photo library, though his decoding work had focused on other tables. The investigative uses that follow from the structure above are timeline reconstruction (transaction timestamps + change type per asset), attribution (which daemon/bundle/context authored a change), and residual-data recovery on deleted assets via tombstones. 
Hawk Eye Forensic
The Forensic Scooter

Two important caveats from the literature:

First, the history is prunable, so its absence proves nothing. Tsai found that turning history off only batch-deletes the ACHANGE and ATRANSACTION tables; it does not clear the interned strings in ATRANSACTIONSTRING or remove the tables — which is why a populated ATRANSACTIONSTRING can outlive the transactions that referenced it. Apps routinely call deleteHistory(before:) to trim merged transactions during normal operation. 
Michael Tsai

Second, standard SQLite carving applies on top: deleted ACHANGE/ATRANSACTION rows may persist in freelist pages or unallocated space, recoverable with SQLite forensic tooling unless overwritten or securely deleted.

A note on scope: most of the deep field-by-field semantics here come from reverse-engineering of an API Apple has kept largely undocumented (Tsai's work, the SwiftLee/fatbobman developer write-ups) plus the iOS-forensics community (The Forensic Scooter, iLEAPP) — there isn't a formal peer-reviewed corpus dedicated to these three tables specifically, so treat the tombstone/column-bitmap details as well-evidenced community findings rather than vendor-confirmed.

If it's useful, I can decode the ZCOLUMNS bitmaps and ZTOMBSTONE blobs in your actual file to show what specific asset attributes were changed or preserved on the 4 deletions — that's where the concrete evidence in this particular library would surface. I can also package this review as a Word or PDF document.

Can you tell which tables and records are added/modifie/deleted at what time?

Jun 18

Architected database query strategy to reconstruct change timeline
Architected database query strategy to reconstruct change timeline
This is exactly the join the three tables are designed for: ACHANGE (what changed) → ATRANSACTION (when + who) → Z_PRIMARYKEY (which entity). Let me decode it against your file.


Architected comprehensive timeline mapping table changes across transaction history
Architected comprehensive timeline mapping table changes across transaction history
Clear picture now: activity runs from Nov 2024 to 17 June 2026, and all 4 deletes happened in a single transaction on 2026-03-16 14:23:43 — one photo (Asset #7) plus its attribute row and two underlying resource rows. Let me build the full decoded timeline, enrich asset changes with filenames, and decode the tombstone of that deleted photo.


Decoding deleted asset lifecycles and tombstone records
Decoding deleted asset lifecycles and tombstone records
Found it — the asset table is ZGENERICASSET (with ZFILENAME/ZADDEDDATE). Now I'll build the complete decoded timeline, write a full downloadable change log, and surface the meaningful events inline.


Diagnosing NULL tombstone blobs and type inconsistencies
Diagnosing NULL tombstone blobs and type inconsistencies
The lifecycle is fully reconstructable — Asset #7 was inserted 2025-11-19 14:43:41 and deleted 2026-03-16 14:23:43. Let me decode its tombstone to recover what survived the deletion.


Excavated database timeline and recovered deleted asset identifiers
Excavated database timeline and recovered deleted asset identifiers
The deleted photo's UUID survived in its tombstone — that's the forensic payoff I described earlier. Here's the full decoded change log:


Presented file
Presented file
Yes — every change is timestamped and attributable. The attached CSV has all 1,533 decoded changes (timestamp, INSERT/UPDATE/DELETE, entity/table, record PK, filename where it's an asset, and the authoring process). Here's what it shows.

How the timeline was reconstructed
Each ACHANGE row was joined to its ATRANSACTION (via ZTRANSACTIONID) for the timestamp and author, the entity code resolved through Z_PRIMARYKEY to a table name, and asset PKs matched against ZGENERICASSET for filenames. All timestamps are the transaction's ZTIMESTAMP (Cocoa absolute time) in UTC. Every transaction in your file was authored by assetsd (com.apple.assetsd) — the library daemon — so these are the system committing library state, not a direct record of taps.

Activity span and shape
Changes run from 2024-11-12 09:34 to 2026-06-17 12:53, in roughly 28 bursts. The overall mix is 324 inserts, 1,205 updates, 4 deletes. A few representative sessions:

2024-11-12 09:34 — library scaffolding created: 27 FetchingAlbum, 7 AlbumList, 6 Album, 5 Folder inserts. This is the empty-library setup.
2025-11-19 14:43–16:54 — heavy import + analysis: dozens of Asset/AdditionalAssetAttributes updates, plus DetectedFace, DetectedFaceprint, Person, DetectedFaceGroup, and Sceneprint inserts (face detection and scene classification running over new photos).
2026-06-17 11:56–12:53 — the largest burst: 301 Asset updates, 33 Asset inserts, 59 InternalResource inserts, 24 Sceneprint, plus Memory updates. A bulk import of ~33 photos.
Records added (when each photo entered the library)
The asset INSERT rows give a clean import timeline — 51 assets, e.g. IMG_0001.HEIC at 2024-11-19 10:18:11, IMG_0018.MOV at 2026-06-12 14:44:02, through IMG_0051.MOV at 2026-06-17 12:22:17. The full per-asset list is in the CSV (filter change=INSERT, entity=Asset).

The deletion event
One photo was deleted, and its full lifecycle is visible:

Inserted: Asset #7 on 2025-11-19 14:43:41
Deleted: 2026-03-16 14:23:43, in a single transaction that removed Asset #7, its AdditionalAssetAttributes #7, and two backing InternalResource rows (#13, #14) — i.e. the photo plus its metadata and rendered resources.
The asset row itself is gone, but its tombstone preserved the asset's UUID: 811DBDC8-18A3-47DD-92D0-64CE4A5377E2. That identifier is what you'd pivot on to correlate the deleted photo against thumbnails (the Thumbnails/V2 folders are named by UUID), iCloud records, or a prior backup. The filename wasn't tombstoned, so the CSV shows it as (deleted).

Two caveats on interpretation
This log only covers history that hasn't been pruned — Core Data's deleteHistory(before:) periodically clears old ACHANGE/ATRANSACTION rows, so the earliest entries here aren't necessarily the library's true origin, and absence of a change isn't proof it never happened. And because every transaction is authored by assetsd, these timestamps mark when the system committed a change (import, analysis pass, deletion), which is close to but not identical with the moment of a user action.

If you want, I can produce a per-asset lifecycle view (one row per photo: added → each modification → deleted/last-seen), or decode the ZCOLUMNS update bitmaps to show which fields changed on the high-volume update bursts.


Photos change log
Table · CSV 
generate a python program that runs this on any type of database with this transactionlog.

Jun 18

Debugging CSV row-writing and cursor execution issues
Debugging CSV row-writing and cursor execution issues
Good idea — what I ran was bespoke. Let me write a proper, reusable tool that works on any Core Data store with these tables (Photos, Notes, Health, Messages-adjacent stores, etc.), then test it against your file before handing it over.

The key to making it generic is to discover the schema rather than hardcode it: dynamic column detection on all three tables, entity-name resolution via Z_PRIMARYKEY, and entity→backing-table mapping by scanning for tables whose Z_ENT column contains each entity code (this is what correctly resolves quirks like Asset → ZGENERICASSET).


Generic Core Data persistent-history decoder CLI
Generic Core Data persistent-history decoder CLI
Now let me test it against your file to confirm it reproduces the earlier results.


Finalizing script and crafting concise explanation
Finalizing script and crafting concise explanation
The summary is correct, but the CSV wrote only 1 row — a shared-cursor bug: the label lookups reuse the same cursor that's streaming the main query, cutting it short. Let me fix it by giving the iterator and the labeller their own cursors.


Validated comprehensive solution and prepared script presentation
Validated comprehensive solution and prepared script presentation
Working end to end — 1,533 rows, labels resolved, and the deleted asset's UUID recovered from its tombstone, all reproducing the earlier hand-rolled analysis on a fully generic code path.


Presented file
Presented file
coredata_history.py is attached — a dependency-free CLI (standard library only) that decodes the persistent-history log of any Core Data store, not just this Photos library.

What makes it generic is that it discovers the schema at runtime instead of hardcoding it:

Dynamic column detection on all three tables — it finds the author-style fields in ATRANSACTION by pairing each interned-reference column (ending TS) with its inline sibling, and finds however many ZTOMBSTONE* columns the store happens to have. So a Notes or Health store with a different column set still works.
Entity naming via Z_PRIMARYKEY, and entity→backing-table mapping by scanning every Z* table for the entity code in its Z_ENT column. That's what correctly resolves quirks like Asset → ZGENERICASSET without being told.
Best-effort record labels (filename/title/name/uuid…), overridable per entity with --label Entity=TABLE.COLUMN.
Read-only and immutable open (immutable=1), so it never creates a -wal/journal or writes to the file — safe to run on evidence.
Usage:

python3 coredata_history.py LIBRARY.sqlite -o changes.csv --summary
python3 coredata_history.py LIBRARY.sqlite --deletes
python3 coredata_history.py LIBRARY.sqlite --label Asset=ZGENERICASSET.ZFILENAME
The CSV carries one row per change (timestamp, INSERT/UPDATE/DELETE, entity, record PK, label, transaction id, every author field, and each tombstone column). --summary prints the session-grouped timeline; --deletes prints each deletion with the values its tombstones preserved.

Two limits worth keeping in mind, both inherent to the data rather than the tool: it can only report history that hasn't been pruned by deleteHistory(before:), and the timestamps are when the authoring process committed each change, which is close to but not the same as the user action behind it.

If you want, I can add a per-record lifecycle view (one row per object: created → modifications → deleted/last-seen) or ZCOLUMNS bitmap decoding to show which fields each UPDATE touched.


Coredata history
PY 
Interview about the tool. Does it show which table is updated?

Jun 18

Clarified tool's entity tracking capabilities and acknowledged column-level limitations
Clarified tool's entity tracking capabilities and acknowledged column-level limitations
Yes — for every change, including updates. The CSV has an entity column and a change column, so an update appears as a row like:

2026-06-17 11:57:09,UPDATE,Asset,29,IMG_0029.HEIC,...
That tells you the entity that was updated (Asset), the change type (UPDATE), and which specific record (entity_pk 29, here labelled with its filename). The --summary view rolls these up per session as Asset UPDATEx301, etc.

Two precisions worth stating plainly, since this is the kind of thing that matters under scrutiny:

First, what it reports is the Core Data entity name, not the raw SQLite table name. For most stores those line up, but they can diverge — the Photos "Asset" entity is physically stored in the ZGENERICASSET table. The tool actually resolves that backing table internally (it's how it pulls the filename label), but it doesn't print it as a column. If you need the physical table name in the output for, say, correlating against a raw SQLite carve, that's a one-line addition.

Second — and this is the more important limitation — it shows which table/record was updated, but not which fields within that record changed. That field-level information does exist: ACHANGE.ZCOLUMNS is a bitmap of the updated properties (it maps to NSPersistentHistoryChange.updatedProperties). I flagged decoding it as an optional extra but haven't implemented it. So as it stands, an UPDATE row tells you "Asset #29 was modified at this time by assetsd," not "the favorite flag was set" or "the location was edited."

So if the question is "can it tell me a given table/record was updated and when," yes, fully. If it's "can it tell me what changed inside that row," not yet — that needs the ZCOLUMNS bitmap decoded against each entity's column list. Want me to add that?

Write a manual, forensic description, reference for this tool. Also do a novelty scan and refer prior art.

Jun 18

Excavating prior-art evidence regarding forensic database query methodologies
Excavating prior-art evidence regarding forensic database query methodologies
Good scope. Before writing, let me run a proper novelty scan — I need to know what existing tools and literature actually do with these tables so the prior-art section is honest rather than hand-wavy.


Scrutinizing forensic tooling gaps and prior art scarcity
Scrutinizing forensic tooling gaps and prior art scarcity
Strong signal already: the leading Photos.sqlite researcher (Scott Koenig) explicitly left these three tables undecoded, and iLEAPP's parsers target current-state tables. Let me check the academic literature and any generic Core Data tooling to complete the scan.


Synthesized prior art landscape and architected comprehensive forensic documentation
Synthesized prior art landscape and architected comprehensive forensic documentation
I now have a complete picture of the landscape. Writing the reference document — it covers the manual, the forensic description, the data-structure reference, and the novelty/prior-art scan grounded in what I found.


Comprehensive manual + forensic reference + novelty scan for the tool
Comprehensive manual + forensic reference + novelty scan for the tool
cont

9:07 AM

Presented file
Presented file
The reference document is attached. It's structured in eight parts: an overview, the manual (install, CLI options, workflows), the output reference (CSV schema, summary, deletes report), a data-structure reference (timestamps, change-type enum, entity/record resolution, attribution, tombstones, the ZCOLUMNS bitmap), the forensic section (soundness, evidentiary value, the interpretation caveats that belong in any report, and the validation performed against your file), known limitations, and the novelty scan with prior art.

On the novelty scan specifically, here's the honest bottom line.

The tables themselves and their forensic relevance are not novel. The leading Photos.sqlite researcher, Scott Koenig, documented that ACHANGE, ATRANSACTION, and ATRANSACTIONSTRING contained data during testing but were not included in his queries, and elsewhere stated plainly that he believes these tables hold a lot of asset-activity information but had focused on decoding other tables and did not want to speculate on their contents. So the persistent-history tables are a known but deliberately unworked gap in applied iOS forensics. The internal semantics are also prior art: Michael Tsai reverse-engineered that the history lives in the same SQLite file under differently-prefixed tables, that a single column can store primary keys for different entity types, and that the tables are maintained by SQLite triggers firing on all changes including batch operations. 
SwiftLee + 2

What I could not find in the open-source prior art — and what the document frames as the tool's actual contribution — is the combination of: offline extraction without the data model or live Core Data runtime (Apple's own API is a runtime mechanism where the changed data in these tables is ultimately converted into managed object IDs, i.e. it needs the live stack); a store-agnostic, schema-discovering design that resolves entity-to-table mapping generically; and history-as-timeline output with author attribution and tombstone recovery. The existing forensic parsers — Koenig's queries and iLEAPP's Ph1–Ph30 — all target current-state tables (ZASSET/ZGENERICASSET, ZSHARE, etc.), not the change log. 
Medium

Two honesty caveats are written into the document so the claim isn't overstated: the survey covers open sources only, so closed commercial suites (Cellebrite, Magnet AXIOM) could implement undisclosed equivalents; and the field semantics are reverse-engineered rather than Apple-confirmed, so critical findings should be corroborated against independent artifacts.

If you'd like, I can convert this to PDF or Word for distribution, or fold in the field-level ZCOLUMNS decoding and physical-table-name column and update the limitations section to match.


Coredata history reference
Document · MD 
Does any of the references actually decode this type of information in a forensic context? A few seem more for developpers,

9:15 AM

Confirming forensic sources neglect Core Data history decoding
Confirming forensic sources neglect Core Data history decoding
Fair challenge — and you're right to press it. Let me verify rather than just assert, because if there is a forensic source that actually decodes these tables, it changes the novelty claim. Let me hunt specifically for that.


Categorized references by type; acknowledged developer sources versus forensic gap
Categorized references by type; acknowledged developer sources versus forensic gap
You're right, and it's worth being precise about it. None of the references actually decode these three tables in a forensic context. Splitting the list by what each one really is:

Reference	Type	Does it decode ACHANGE/ATRANSACTION/ATRANSACTIONSTRING?
Apple NSPersistentHistory docs / WWDC	Developer / API	Defines the runtime API; doesn't touch the on-disk tables at all
Michael Tsai	Developer / reverse-engineering	Decodes the internals (triggers, tombstones, shared-PK column) — but for app development, not forensics
fatbobman	Developer	Explains table roles + timestamp for building sync; not forensic
SwiftLee	Developer	Same — developer usage guide
Scott Koenig (Forensic Scooter)	Forensic	No — explicitly excluded them and declined to decode
iLEAPP	Forensic tooling	No — its parsers target current-state tables (ZASSET, ZSHARE, …)
RealityNet index	Forensic index	No — a link directory, decodes nothing
So the list splits cleanly into two groups that were doing different jobs in my document, and I conflated them. The four developer sources are there to attribute the underlying semantics the tool relies on (the change-type enum, tombstones, the Cocoa timestamp, string interning) — they decode the structure, but in a developer setting, never as forensic timeline reconstruction. The three forensic sources are there as prior art, and the honest finding there is an absence: the leading forensic work on this exact database deliberately leaves these tables undecoded. Koenig states that the three tables contained data during testing but were not included in his queries, and separately that he had not dedicated enough time to researching their contents and would not speculate on what they hold. 
SwiftLee
GitHub

My second, forensics-targeted search round reinforced this. The forensic sources that surface for "deleted data / timeline" on iOS decode other artifacts — deleted-app container traces, sms.db and Biome message remnants, CallHistory free-page carving, wipe logs — none of them the Core Data persistent-history log.

Two honesty caveats on that conclusion, which I'd want stated rather than buried: my scan is open-source only, so closed commercial suites (Cellebrite, Magnet AXIOM, Belkasoft) could decode these tables internally without publishing how; and conference talks or paywalled training (e.g. SANS FOR585 material) could cover ground I can't see. What I can say is that across two rounds of targeted searching I found no published forensic source that decodes these three tables — which is consistent with, and actually the basis for, the novelty framing rather than undercutting it.

If you'd like, I can revise the reference section of the document to label each entry by type — "semantics attribution (developer)" versus "forensic prior art (gap)" — so the distinction you flagged is explicit on the page rather than something a reader has to infer. That would make the prior-art claim more defensible, not less. Want me to make that edit?

Revise the reference section.

9:17 AM

Verifying reference consistency across document sections
Verifying reference consistency across document sections
Now I'll replace the entire references section, grouping entries by type and adding an explicit note on what each group contributes.


Reconciling discrepancies between edited and stale file copies
Reconciling discrepancies between edited and stale file copies
Let me verify the revised section reads cleanly.


Identified stale file copy and orchestrated synchronization
Identified stale file copy and orchestrated synchronization
The presented copy is stale — my edit landed in the working file but the output copy wasn't refreshed. Let me confirm the working file has the revision and republish it.


Ran a command, read a file
Ran a command, read a file
Done. Section 7.4 is now split into three groups, each entry annotated with what it contributes and whether it decodes these tables forensically:

Group A — Semantics attribution (developer / reverse-engineering; not forensic): Apple's API, Tsai, fatbobman, SwiftLee. Each now carries a Used for note (the change-type enum, tombstones, the Cocoa timestamp, string interning, etc.) and an explicit Forensic decoding: none.
Group B — Forensic prior art (does not decode these tables): Koenig, iLEAPP, RealityNet. Each carries a Relevance note and a Forensic decoding of these tables: none line — with Koenig's exclusion called out as documented future work.
Group C — Forensic decoding of these tables: None identified in the surveyed open literature.
A short preamble explains the two purposes the list serves so a reader doesn't conflate them, and the open-source-only scope caveat is repeated right there at the references rather than only in §7.3.

One consistency note worth flagging: §7.2 still uses its own A/B/C/D categorization for the prior-art narrative, and §7.3 cross-references those as "Prior art A/C." Those are a different axis from the new "Group A/B/C" in §7.4, and they're labeled differently ("Prior art" vs "Group"), so they don't contradict — but if you'd prefer a single unified scheme across the whole chapter to remove any chance of a reader mixing them up, I can renumber §7.2/7.3 to match. Want me to, or convert the finished document to PDF/Word?


Coredata history reference
Document · MD 
Update the script to also output the columnnames (one column in the output with concattenated seperated column names of the updated columns)

9:21 AM

Unraveling bitmap indexing discrepancies between columns and bits
Unraveling bitmap indexing discrepancies between columns and bits
This is the ZCOLUMNS bitmap decoding I'd flagged as future work. The bit-to-column mapping isn't officially documented, so before I write it into the tool I'll reverse-engineer the convention against your file empirically rather than guess. Let me inspect the actual bitmaps.


Strategizing correlation methods for UPDATE operations
Strategizing correlation methods for UPDATE operations
The simple "bit = column position" assumption is already falsified — the bitmap is 15 bytes (120 bits) but the table has 99 columns, and set bits go up to index 118. So the bitmap indexes Core Data's internal property order, not the physical column order. Let me correlate against an INSERT (where I can check the bitmap against the row's actually-populated columns) to pin down the real mapping.


Populating updated columns with physical names, handling index overflow
Populating updated columns with physical names, handling index overflow
Important finding before I write this: the bitmap does not index physical columns. ACHANGE.ZCOLUMNS is 15 bytes (120 bits) but ZGENERICASSET has 99 columns, and set bits reach index 118 — so the bitmap indexes Core Data's internal property list (attributes plus relationships, including to-many relationships that have no physical column), in alphabetical order. INSERT rows carry a null bitmap (consistent with the API: updatedProperties is nil for inserts).

That has a real consequence for forensic honesty: an exact bit→name mapping needs the compiled data model, because to-many relationships occupy index slots but aren't recoverable as columns. So I'll implement a best-effort decoder that maps to the physical column names, clearly flags its assumption, falls back to a raw #index token when it can't be sure, and accepts an authoritative override. Let me look at the parts I need to change.


Viewed 2 files
Viewed 2 files
Now I'll add the property-order builder and bitmap decoder. First, build the per-entity ordered property list right after the entity-table index:


Build per-entity property order from physical columns and allow overrides
Build per-entity property order from physical columns and allow overrides
Now wire the decoded columns into iter_changes. Let me view that section:


Viewed a file, edited a file
Viewed a file, edited a file
Now add the updated_columns column to the CSV writer:


Viewed a file, edited a file
Viewed a file, edited a file
Now add the --property-map CLI option and wire it in. Let me view the argparse section:


Viewed a file, edited a file
Viewed a file, edited a file
Now let me test the updated script end to end:


Validating token mapping and verifying property override application
Validating token mapping and verifying property override application
Works: UPDATE rows now carry an updated_columns field (e.g. ZENDDATE, ZCLOUDBATCHPUBLISHDATE), inserts/deletes are empty as expected, and where a bit index lands beyond the known property list it emits a raw #index token (e.g. #4) rather than guessing a name. Let me run a quick sanity check on the decoder and confirm the help text:


Validated token mapping results and orchestrated documentation updates
Validated token mapping results and orchestrated documentation updates
Good result to report honestly: 88% of updated-column tokens resolve to named columns; 11% are the to-many-relationship slots that fall beyond the physical columns and are shown as raw #index. The --property-map override applies correctly too. Now let me sync the reference document so it doesn't contradict the tool — updating the output schema, the ZCOLUMNS section, and the limitations.


Edited 3 files
Edited 3 files
Now let me also update the CLI options table in the manual (§2.1) to include --property-map, then republish both files.


Edited a file, ran a command, read a file
Edited a file, ran a command, read a file
Both files are updated. The script now emits an updated_columns column, and the reference document is synced so it no longer contradicts the tool.

What the change does: for each UPDATE, it decodes the ACHANGE.ZCOLUMNS bitmap into a ;-separated list of changed property names, placed as a single CSV column between label and txn_id. Inserts and deletes are blank (their bitmap is null, matching the API).

The one thing I want to be candid about, because it bears on how much weight you can put on the output: this mapping is best-effort, not exact, for a structural reason rather than a coding shortcut. The bitmap indexes Core Data's full property list — attributes plus relationships — in alphabetical order, and to-many relationships occupy index slots while having no physical column in the SQLite file. So a to-many relationship that sorts early in the alphabet can shift the indices of everything after it, and there's no way to recover those relationship names from the database alone. The tool handles this conservatively: it maps a bit to the physical column at that index when the index is in range, and otherwise emits a raw #index token rather than inventing a name. On your Photos store that came out to about 88% named columns and 11% raw indices.

For evidentiary-grade exactness on a given entity you can supply the authoritative ordered property list (extracted from the app's compiled .momd model) via --property-map Asset=prop0,prop1,..., which overrides the heuristic for that entity. All of this — the bit order, the property-vs-column index space, the to-many caveat, and the override — is now written into §4.6 of the reference, with the limitations section and CLI table updated to match.

If you want to push accuracy higher, the natural next step is a small helper that dumps the ordered property list out of a .momd/.mom file into the --property-map format, which would make decoding exact wherever you have the model. I can build that if it's useful.


Coredata history
PY 

Coredata history reference
Document · MD 
Explain this trigger: CREATE TRIGGER Z_RT_Asset_boundedByRect_INSERT AFTER INSERT ON ZASSET FOR EACH ROW BEGIN INSERT OR REPLACE INTO Z_RT_Asset_boundedByRect (Z_PK, ZLATITUDE_MIN, ZLATITUDE_MAX, ZLONGITUDE_MIN, ZLONGITUDE_MAX) VALUES (NEW.Z_PK, NEW.ZLATITUDE ,NEW.ZLATITUDE, NEW.ZLONGITUDE, NEW.ZLONGITUDE) ; END;

9:29 AM

Dissected spatial indexing trigger mechanics and forensic implications
Dissected spatial indexing trigger mechanics and forensic implications
This is one of Core Data's auto-generated R-tree (spatial index) maintenance triggers — a different breed from the persistent-history triggers we've been working with, but installed by the same framework under the hood and likewise invisible to the Core Data API.

Reading it line by line:

Z_RT_Asset_boundedByRect_INSERT — the name encodes everything. Z_RT_ is Core Data's prefix for "R-Tree," Asset is the entity, boundedByRect is the name of a spatial fetch-index defined on that entity in the app's data model, and _INSERT is the event it fires on. (In your earlier Photos.sqlite the same index appeared as Z_RT_GenericAsset_boundedByRect plus its _rowid / _node / _parent shadow tables — the naming just tracks whether the schema calls the entity Asset or GenericAsset.)

AFTER INSERT ON ZASSET FOR EACH ROW — it runs once for every new row added to the asset table, immediately after the insert commits to that table.

The body keeps a companion spatial-index table in sync. Z_RT_Asset_boundedByRect is a SQLite R*Tree virtual table, declared roughly as RTREE(Z_PK, ZLATITUDE_MIN, ZLATITUDE_MAX, ZLONGITUDE_MIN, ZLONGITUDE_MAX) — an integer key plus a min/max pair per dimension, here two dimensions (latitude and longitude). An R-tree indexes bounding boxes so the database can answer "which rows fall inside this rectangle?" without scanning the whole table.

The clever part is what gets inserted:

VALUES (NEW.Z_PK, NEW.ZLATITUDE, NEW.ZLATITUDE, NEW.ZLONGITUDE, NEW.ZLONGITUDE)
NEW is the row just inserted, so NEW.ZLATITUDE / NEW.ZLONGITUDE are the asset's coordinates. A photo is a single point, not an area, so the trigger stores a degenerate (zero-area) box — the same latitude as both the min and the max, and the same longitude as both min and max. A point is just a rectangle collapsed to a dot, and that's the legal way to put a point into an R-tree.

INSERT OR REPLACE keyed on Z_PK makes it idempotent: if an index entry for that primary key already exists it's overwritten rather than duplicated, so the index can't drift out of one-to-one correspondence with the base table.

This trigger is one of a set. Core Data generates a matching _UPDATE trigger (re-writes the box when an asset's lat/lon change) and _DELETE trigger (removes the entry when the asset row is deleted), so the R-tree always mirrors the live coordinates in ZASSET. In Photos itself, this index is what powers fast location queries — the map view, the Places album, location-based moment grouping — by letting SQLite return all assets whose point lies within a queried map rectangle.

A note connecting it back to the forensic thread: this table is a derived index, not source evidence — the authoritative coordinates live in ZASSET.ZLATITUDE / ZLONGITUDE, and the R-tree is just a maintained copy for query speed. So for analysis you'd cite the asset row, not the R-tree. That said, the R-tree and its _node/_parent shadow tables can occasionally be a corroboration or recovery surface: because they're maintained by separate triggers and stored as their own B-tree pages, a stale or deleted-but-not-yet-overwritten entry there could in principle reveal a coordinate that's already gone from the live asset row. Treat any such finding as a lead to validate against the base table and page-level carving, not as standalone proof.

I didn't see any triggers filling ACHANGE, ATRANSACTION and ATRANSACTIONSTRING.. does that make sense?

9:32 AM

Reconciled observed schema against prior claims about trigger mechanisms
Reconciled observed schema against prior claims about trigger mechanisms
Good catch — that's exactly the right thing to notice, and it's worth checking rather than hand-waving, especially since I earlier cited Tsai's "maintained by triggers" line. Let me look at what triggers actually exist in your file.


Verified absence of triggers, reconciled framework mechanism discrepancy
Verified absence of triggers, reconciled framework mechanism discrepancy
It makes complete sense, and you're right to flag it against what I said earlier. I just checked your file: there are 59 triggers, and not one of them references or fires into ACHANGE, ATRANSACTION, or ATRANSACTIONSTRING. So your observation is correct, and it means the "maintained by SQLite triggers" line I leaned on earlier (citing Tsai) is too strong as applied to this database. Let me give you the more accurate picture.

What the triggers in your file actually are, all 59 of them, fall into two Core Data-internal families:

ZT_* — derived-attribute maintainers. These cache aggregate values, e.g. ZT_ZPERSON_ZFACECOUNT_ZDETECTEDFACE_INSERT_INCREMENT bumps a person's face-count when a detected face is inserted. Core Data generates these for "derived" attributes.
Z_RT_* — the R-tree spatial-index maintainers, like the boundedByRect trigger you just asked about.
Those two families are almost certainly the source of the general "Core Data uses triggers" reputation — and they are persistent, visible triggers, which is exactly why you can see them. The history tables are populated by a different mechanism entirely.

How the history actually gets there: with persistent history tracking enabled, Core Data's own SQLite store engine writes the ATRANSACTION / ACHANGE / ATRANSACTIONSTRING rows directly, as part of the same commit that writes the data changes. The framework already knows what it inserted, updated, and deleted in a given save, so it records those rows itself within the transaction. There's no standing trigger in the schema doing it — which is precisely why nothing in sqlite_master references those tables.

That leaves the one thing Tsai's account was really pointing at: how the history still captures batch operations (NSBatchUpdateRequest / NSBatchDeleteRequest) that bypass the managed object context and hit the store coordinator directly. The framework needs to discover which rows a batch touched, and the mechanism for that can involve triggers created and dropped transiently around the operation — which by definition wouldn't survive in a snapshot of the schema you inspect afterward. So "fired for all database changes, including batch ops" can be true behaviorally without any of those triggers being visible in your file.

Two honesty notes on the limits of this. First, the distinction between "written directly by the store code" and "transient triggers for batch paths" is Core Data framework internals that aren't fully documented and that I can't prove from a static file — what your file does prove definitively is the negative: no persistent triggers populate these tables. Second, this could also be version drift: Tsai's observations are from 2019, and Apple has reworked Core Data's store internals across releases, so a mechanism that looked trigger-driven then may not be now (and your store is an iOS 14-era schema). I should have framed that earlier rather than presenting "maintained by triggers" as a flat fact.

None of this changes the forensic conclusions we drew. However the rows get written, they're still committed by Core Data as a transactional log, still carry the timestamps and authors we decoded, still capture batch operations, and are still prunable by deleteHistory(before:). The population mechanism was the one part I overstated; the contents and their evidentiary use stand.

I noticed the length of the ZCOLUMNS blob varies, does it correlate with the column numbers of the specific ZENTITY?

9:42 AM

Formulated testable hypothesis correlating blob length to entity property counts
Formulated testable hypothesis correlating blob length to entity property counts
Excellent question, and very testable. Let me check it directly against your file rather than reason about it — grouping ZCOLUMNS lengths by entity and comparing to each entity's column count.


Analyzed empirical data to confirm blob length correlates with entity properties
Analyzed empirical data to confirm blob length correlates with entity properties
This is a great catch and the data tells a more interesting story than a simple "length = column count." Let me confirm the key anomaly before I explain — the Album/Folder rows look like they share a table.


Validated inheritance hypothesis and correlated blob width to entity properties
Validated inheritance hypothesis and correlated blob width to entity properties
Confirmed — and the answer is more nuanced than "yes." Here's what your file shows.

First, the length is fixed per entity, not per row: every Asset change is 15 bytes, every AlbumList change is 1 byte, and so on. Each entity showed exactly one distinct blob length. So the variation you noticed is between entities, and in that sense yes, it's tied to the specific ZENTITY.

But it tracks the entity's modeled property count (attributes + relationships as defined in the data model), rounded up to whole bytes — length_bytes ≈ ceil(property_count / 8) — not the number of physical columns in the backing table. Those two numbers diverge in two opposite ways, both visible in your data:

entity	physical cols	blob bytes	bits (=len×8)	max bit seen
Asset	96	15	120	118
AdditionalAssetAttributes	72	10	80	78
Album	64	5	40	31
Folder	64	5	40	39
MomentList	7	2	16	15
Moment	20	3	24	19
The max set bit is always below the allocated width, so the bitmap is sized to hold the entity's properties with no overflow. The divergences from physical column count:

Bits exceed columns for most entities (Asset 96→120, MomentList 7→16). That's the to-many-relationship effect we hit earlier: to-many relationships are properties and get bitmap slots, but they have no physical column, so the bitmap is wider than the table.

Bits are fewer than columns for Album and Folder — 40 bits against a 64-column table — which looks impossible until you see the cause: both are subclasses of GenericAlbum (Album is Z_ENT=26, Folder is 32, both with Z_SUPER=25), and both are stored in the same ZGENERICALBUM table. That table's 64 columns are the union of every GenericAlbum subtype's attributes, so it's larger than any one concrete entity's own property set. The bitmap is per concrete entity (~40 properties each), not per shared table. Core Data uses single-table inheritance, and your Asset→GenericAsset→ZGENERICASSET follows the same pattern.

So the precise statement is: the ZCOLUMNS length is determined by the concrete entity's property count, rounded up to a byte — which correlates with, but is not equal to, the backing table's column count, biased up by to-many relationships and biased down by table sharing through inheritance.

This also sharpens the decoding caveat from earlier. Mapping bit positions against physical columns is best-effort in general because of to-many relationships, but for shared-table entities like Album/Folder it's doubly off, because the correct index space is each entity's own ordered property list, not the 64-column union of ZGENERICALBUM. That's the strongest argument yet for supplying --property-map (from the compiled data model) when you need exact column names for those entities, rather than trusting the heuristic.

Could the blob be truncated (stripped zero)? But I think I see a lot of zero trailing bytes.

9:48 AM

Confirming blob integrity through fixed-width entity analysis
Confirming blob integrity through fixed-width entity analysis
You've essentially answered it yourself — and your instinct is right. The presence of trailing zero bytes is itself the proof it's not truncated: a zero-stripped encoding would never leave a trailing 0x00, because the whole point of stripping is to drop them, so the final byte would always have at least one bit set. Let me verify both halves against your file.


Analyzed blob structure and vindicated fixed-width bitmap hypothesis
Analyzed blob structure and vindicated fixed-width bitmap hypothesis
No truncation — it's a fixed-width bitmap with the trailing zeros retained, and your file proves it three ways.

The decisive one is this Asset row:

hex = 00d100000000000000000000000000   (15 bytes)
Its highest set bit is at position 15 (byte 1). If the blob were zero-stripped it would be just two bytes (00d1); instead all 13 trailing all-zero bytes are kept, padding it to the full 15-byte width. Every one of the 486 Asset changes is exactly 15 bytes regardless of which properties changed.

The aggregate stats say the same thing: 459 of 486 Asset rows and 318 of 359 AdditionalAssetAttributes rows have a last byte of 0x00 — which is simply impossible under a stripping scheme, since stripping exists precisely to remove trailing zeros. So the zeros you're seeing are the signature of a non-truncated, fixed-width field.

The width is allocated up front as the minimum whole number of bytes needed to address the entity's full property set — ceil(property_count / 8) — and then bits are set only for the changed properties. Low-indexed changes leave the high bytes zero, which is what you're noticing.

One subtlety that might otherwise look like an exception: a few entities (AlbumList, Folder, DetectedFaceGroup) showed zero rows ending in 0x00. That's not truncation either — their bitmaps are narrow (1 byte for AlbumList, 5 for Folder), and the properties that actually change happen to sit in the last byte. Folder's highest changed property is index 39, which is the final bit of its 5th byte, so its last byte is never empty. The width is exactly enough to hold the highest-indexed property and no more, so whether the last byte is zero just depends on which properties an entity tends to change.

Two practical takeaways. The decoder already handles this correctly — it walks every byte and trailing zeros simply contribute no set bits, so nothing needs changing. And because the width is fixed and unstripped, the blob length is itself a reliable fingerprint of the entity: a constant byte-length per ZENTITY is exactly what you'd expect, and a row whose ZCOLUMNS length doesn't match its entity's norm would be worth a second look as a possible integrity or parsing issue.

Explain clear to me. Is the nr. of bytes consistent when we assume it describes all columns including the hierarchie of tables?

9:52 AM

Exposing fundamental inconsistency between bytes and columns
Exposing fundamental inconsistency between bytes and columns
Direct answer: no — it is not consistent under that assumption. If the bitmap described all columns of the backing table including the inheritance hierarchy, the byte counts would fail in two opposite directions, and in the Album/Folder case it's not just inconsistent but mathematically impossible. Let me show it cleanly with the numbers from your file.


Analyzed data model inconsistencies and debunked column-counting hypothesis
Analyzed data model inconsistencies and debunked column-counting hypothesis
No. If the bitmap covered all the table's columns including the inheritance hierarchy, the byte counts wouldn't line up — and one case proves it outright.

The clinching example is Album (and Folder). Both are stored in the shared ZGENERICALBUM table, which has 64 columns. But their ZCOLUMNS blob is only 5 bytes = 40 bits. Forty bits cannot address 64 columns — there physically aren't enough bits. So the bitmap simply cannot be describing all the columns of that table. That's not a rounding mismatch; it's impossible.

And the mismatch runs the other way too. Asset sits in a 96-column table but its blob is 15 bytes = 120 bits — 24 bits more than there are columns.

So under the "all columns incl. hierarchy" assumption you get failures in both directions: too few bits for Album/Folder, too many for Asset, AdditionalAssetAttributes, Memory, MomentList, PhotosHighlight. A few entities happen to "fit" by coincidence, but no single column-count rule explains all of them.

Here's the rule that is consistent, in plain terms. The bitmap is sized to the concrete entity's own list of properties in the data model — its attributes plus its relationships — rounded up to whole bytes:

blob bytes = ceil( (entity's own attributes + relationships) / 8 )
That count differs from the physical table's column count for two separate reasons, and your data shows both:

It can be larger than the columns. A to-many relationship is a property but is not stored as a column (it lives in a join table or as a foreign key on the other side). So it gets a bit but no column. That's the surplus on Asset (+3 bytes), AdditionalAssetAttributes, etc.
It can be smaller than the columns. When several entities share one table through inheritance, that table's columns are the union of all the siblings' fields. ZGENERICALBUM's 64 columns are everything GenericAlbum + Album + Folder need between them. But Album's bitmap only covers Album's own ~40 properties, not the whole union — so it's narrower than the table.
In short: the byte count is consistent with the per-entity property list from the model, not with the columns of the (possibly shared, possibly relationship-light) SQLite table. Physical column count is neither a floor nor a ceiling for it — which is exactly why decoding bit positions to column names from the table alone is only approximate, and why the authoritative model (--property-map) is what makes it exact.

Alternative hypotheses: the columns have a (invisible?) number, and the current blob length includes columns that are removed from the schema. Second hypothesis: certain columns use two(?) bits? Does the database schema contain column descriptors? (besides the table create)

10:49 AM

Excavating schema metadata and entity version hashes
Excavating schema metadata and entity version hashes
These are sharp hypotheses, and two of them are directly testable against the file. The most important is your last question — whether the DB carries column/property descriptors beyond the CREATE TABLE — because if it does, it could settle the others. Let me look.


Unearthing property names within decompressed binary plist structures
Unearthing property names within decompressed binary plist structures
Two useful results already. Z_METADATA holds a plist, but it's versioning/integrity data — model version hashes (opaque SHA digests per entity), framework version, entity list — not property names or indices. And there's a Z_MODELCACHE table containing a single 115 KB blob that is almost certainly the compiled model, but it reads as garbage, meaning it's compressed. If I can decompress it, it could contain the authoritative property list and settle your hypotheses. Let me try.


Navigating NSKeyedArchiver object graph architecture
Navigating NSKeyedArchiver object graph architecture
That's the key find: Z_MODELCACHE is the full compiled Core Data model, stored as a raw-zlib-compressed binary plist. Decompressed it yields all the property names in their modeled camelCase form — including relationships that have no column. So the "invisible numbers" you intuited are real: they live here, in the model cache, not in the table definition. Let me parse it and pull the actual per-entity property list to test your hypotheses against Asset's 120-bit width.


Extracting entity descriptions and property nomenclature
Extracting entity descriptions and property nomenclature
It's an NSKeyedArchiver archive of the managed object model — 4,061 objects. Let me navigate the object graph to extract Asset's ordered property list and count, which directly tests your hypotheses.


Reconciling property count discrepancies with byte allocation findings
Reconciling property count discrepancies with byte allocation findings
The vocabulary is clear: NSEntityDescription → NSEntityName + NSProperties (an ordered array). Let me extract Asset's full property list and count, and compare to the 120-bit width.


Deciphering property indices and underlying reference structures
Deciphering property indices and underlying reference structures
Strong result, with one wrinkle worth chasing. Asset has 116 properties in the model → ceil(116/8) = 15 bytes, exactly the observed width. And 116 − 96 physical columns = 20, matching the to-many-relationship surplus. But the max set bit I saw earlier (118) sits above 116, and Folder showed a max bit of 39 against only ~29 listed properties — so the bit index isn't simply the position in that array. Let me resolve that and read the proxy property names properly.


Mapping contiguous property refIDs to alphabetical schema ordering
Mapping contiguous property refIDs to alphabetical schema ordering
There it is — the proxy carries an NSReferenceID (an explicit integer) and points to an underlying property. That NSReferenceID is very likely the "invisible number" you hypothesized — the actual bit index. Let me extract those IDs for Asset and check whether they're contiguous or have gaps (which would confirm your removed-column idea).


Validating bitmap structure against plausible Asset properties
Validating bitmap structure against plausible Asset properties
This is the crux. Asset has 116 properties with reference IDs 0–115, contiguous, no gaps, assigned alphabetically (0:addedDate, 1:adjustmentTimestamp, 2:analysisStateModificationDate…). That's the authoritative answer to your "invisible number" idea — the numbers are real, they live in Z_MODELCACHE, and the bitmap is keyed on them. Let me do the definitive cross-check: map the actually-observed set bits to these names and confirm the width arithmetic.


Validated three hypotheses through systematic bit-mapping analysis
Validated three hypotheses through systematic bit-mapping analysis
This turned out to be the most revealing test yet, and it resolves all three of your questions. Mapping the observed set bits back to the model:

bit 65 -> uniformTypeIdentifier
bit 77 -> dayGroupHighlightBeingKeyAsset
bit 28 -> duration
A change that sets one bit maps to exactly one real property name. So here are your answers.

Does the schema contain column descriptors beyond CREATE TABLE? Yes — and this is the big one. The Z_MODELCACHE table holds the entire compiled Core Data model: a raw-zlib-compressed (zlib.decompress(blob, -15)) NSKeyedArchiver binary plist. Inside, every entity carries its NSProperties array, and every property carries an explicit integer NSReferenceID. For Asset those IDs run 0–115, contiguous, assigned alphabetically by camelCase property name (0:addedDate, 1:adjustmentTimestamp, 2:analysisStateModificationDate, 3:avalanchePickType…). Z_METADATA separately holds version hashes and the entity list, but the property descriptors — names and numbers — are in Z_MODELCACHE. This corrects something I told you earlier: exact bit→name decoding does not require an external .momd; the authoritative map is embedded in the database itself.

Your "invisible number" hypothesis: essentially correct. The bitmap is keyed on that NSReferenceID, not on physical column position — which is why it diverges from the table's column count. And you intuited the key wrinkle: the observed set bits for Asset reach 118, three positions past the highest property in the cached model (115). Bits 116, 117, 118 are genuinely used (12, 6, and 2 times respectively) but correspond to no property in the cache. The in-range bits still decode correctly (bit 28 is still duration), so nothing shifted — the three extras are appended at the high end, not inserted alphabetically. That's the fingerprint of Core Data's stable reference IDs: properties keep their assigned number across model versions, and new ones get the next free ID rather than triggering a renumber. So those three bits are almost certainly properties added in a model version newer than the cached snapshot. The same mechanism means a removed property would leave its slot behind — which is exactly the effect you proposed; I just didn't find removed-property holes in Asset (its 0–115 are gap-free), I found added-after-cache slots instead. Either way, the bitmap width reflects a stable number space, not the current visible column list.

Your "two bits per column" hypothesis: ruled out. A single-property change sets exactly one bit (bit 65 alone → uniformTypeIdentifier). Two bits per property would need ~29 bytes for Asset's 116 properties; the blob is 15 bytes, which is ceil(116/8) — one bit each, four bits of byte-boundary padding. The earlier Album/Folder "40 bits vs 64 columns" oddity is now fully explained too: those are subentities sharing GenericAlbum's reference-ID space, so their bitmap tracks the hierarchy's property numbering, not the union table's columns.

The practical upshot is significant: since Z_MODELCACHE contains the exact name↔NSReferenceID mapping, the tool can decode updated_columns exactly — including the to-many relationships and the post-cache slots — straight from the file, with no external model and no heuristic. If you want, I can add a step that reads and decompresses Z_MODELCACHE, builds the per-entity reference-ID map, and uses it automatically (falling back to the heuristic only when the cache is absent). That would turn the 88%/11% best-effort result into exact decoding.

Update the code as you suggest. Bits that can not be mapped appear as "Unknown_[bitnr]"

11:00 AM

Deciphering property structures and reference identifiers
Deciphering property structures and reference identifiers
Before wiring it in, I need to make sure the parser captures relationship properties too — earlier my quick extraction missed reference IDs on relationship descriptions (that's why Folder looked like it stopped at 27 when its bitmap reaches 39). Let me inspect how relationships store their ID and name so the map is complete.


Architected model cache integration with fallback property mapping strategy
Architected model cache integration with fallback property mapping strategy

Inspect how relationship properties store reference IDs and names

Script
I'm seeing the pattern now—most properties are wrapped in proxy objects with reference IDs, but there's one exception: the childCollections relationship is a direct NSRelationshipDescription without a reference ID, using a different set of keys like NSDestinationEntity.

The real puzzle is that Folder's bitmap reaches bit 39, yet the NSProperties array only defines reference IDs up to 27. Those extra IDs (28-39) aren't in Folder's own property list, so they must be coming from somewhere else—possibly inherited from the superentity GenericAlbum, or maybe the reference IDs are indexed differently at the subentity level than I initially thought. So Folder's properties are only a subset of the full GenericAlbum refID space (0-63), which explains why the proxy refIDs only go to 27 even though the bitmap extends to 39. The bitmap might be setting bits for properties that aren't in Folder's own property list—possibly bits corresponding to other subentities in the GenericAlbum hierarchy like Album or ProjectAlbum. So Album and Folder both inherit the 0-27 refIDs from GenericAlbum, but then add their own properties starting at 28 — Album gets 28-31 (4 properties) and Folder extends to 28-39 (12 properties). The puzzle is that when I extracted Folder's NSProperties, I only saw 0-27 and didn't find the 28-39 range, even though childCollections (which is Folder-specific) appeared in the list without a refID assigned. So the per-entity NSProperties array in the model cache doesn't fully enumerate all properties that the bitmap can reference — it seems to under-count for subentities in a multi-subentity hierarchy. The bits 28-39 likely belong to sibling subentities of Folder within GenericAlbum, but since Folder's bitmap is sized in the shared hierarchy space, it can theoretically address the full range. Yet if Folder's maxbit is 39, that means some Folder change actually set bit 39, which means bit 39 must map to a real Folder property that isn't showing up in the cached NSProperties array.

The likely culprit is that direct NSRelationshipDescription objects (like childCollections) don't have an explicit NSReferenceID stored in the per-entity array the way proxied relationships do, so their refIDs are assigned implicitly and end up occupying those higher bit indices.

Given the user's requirement to label unmapped bits as Unknown_[bitnr], I should stick with that approach—the proxy refID map gives me solid coverage for most properties, and anything that doesn't map gets marked as unknown. The direct relationship descriptions don't have explicit refIDs in the NSProperties array, and since the array order doesn't correlate with refID order, I can't infer them from position. I'll leave those unmapped relationships as Unknown_N rather than over-engineering a solution that won't be reliable.

Now I need to establish the priority hierarchy: user overrides should take precedence, then the model cache (keyed by entity name) as the primary source, with the physical-column heuristic as a fallback when cache data isn't available. I'm setting up the implementation by adding a method to load the model cache during schema initialization, which will populate a model_props dictionary for lookups.

Implementing the decode logic... The model cache will give me proper camelCase property names instead of the Z-prefixed uppercase names from the physical column heuristic, which is more accurate to the actual schema. I need to be careful with the text_factory setting since it returns bytes for all text columns — plistlib.loads handles the bplist decoding correctly and returns proper strings, but I should verify how entity names come through from the archive. I'm deciding not to walk the superentity hierarchy since it won't capture the missing relationship refIDs—those are direct relationships without refIDs that should just map to Unknown. For each entity, I'll stick with collecting refIDs from its own NSProperties proxies, which gave full coverage for Asset and acceptable partial coverage for Folder. I'm considering whether to build a global refID mapping for better name resolution across all entities, but I'm still thinking through whether that's necessary. So the approach would be to build a unified refID→name map for each backing table by merging all entities that share it, then use that map when decoding. This way Folder could inherit property names from sibling GenericAlbum entities at refIDs 28-39 if they expose them. I need to be careful though—different entities shouldn't assign conflicting names to the same refID within a shared hierarchy, since the numbering is supposed to be consistent across the table. IDs are scoped to the backing table, so unioning by table is the right approach. I'll build a map keyed by backing table that merges all refID-to-name mappings from entities sharing that table, then during decoding look up the entity's table and use that merged map. Entity-level overrides still take precedence. Now I need to implement this and measure how much coverage it gives us across the Folder hierarchy.

To populate the cache, I'll iterate through each entity in the model, resolve its code via the name-to-code lookup, find its backing table, and merge its refID map into that table's aggregated map. Abstract entities like GenericAlbum that have no rows won't appear in the code-to-table index, but their properties' refIDs are identical to their concrete subentities', so merging the subentities automatically covers them.

I'll write a standalone test first to verify the coverage improvement before integrating it. Finishing the decode_columns logic to handle the bit-to-property mapping with fallback naming, then I'm implementing _load_model_cache to build a reference map keyed by entity code that unions the model's refmap data from backing tables.

Writing cache initialization... Parsing the plist data... Parsing property names... Continuing to decode the column names by iterating through each bit in the blob data, checking property overrides first, then falling back to the reference map or physical properties depending on what's available.

I need to add zlib and plistlib to the imports at the top since they're both standard library modules. I also need to make sure plistlib.UID is available, which it is in Python 3.8+. Then in the _load_schema method, I'll call _load_model_cache() after building the entity table index and once the entity properties are set up, since it depends on code_to_table and entity_name being available first.

Let me check the current structure of _build_entity_table_index to see exactly where entity_props and decode_columns are positioned so I can place the _load_model_cache call in the right spot.

Show more
Done
The structure is clear now: properties carry an explicit NSReferenceID, but a few to-many relationships (like Folder's childCollections) are stored as direct descriptions without one — those are exactly the bits that should become Unknown_N. And because subentities share a reference-ID space (Album and Folder both have 0:cachedCount), I'll merge the map across entities sharing a backing table for maximum coverage. Let me view the current decode code and wire in the model-cache reader.


Viewed 2 files, edited a file
Viewed 2 files, edited a file
Now let me view the current property/decode section to edit it precisely:


Strategized code refactoring and column decoding implementation
Strategized code refactoring and column decoding implementation


cont



Claude is AI and can make mistakes. Please double-check responses.
Artifacts

Photos change log
Table · CSV 

Coredata history reference
Document · MD 

Coredata history
PY 
Project content
IPhone Database
Created by you

Add PDFs, documents, or other text to reference in this project.
Content

Photos (1).sqlite
sqlite


