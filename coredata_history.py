#!/usr/bin/env python3
"""
coredata_history.py — Decode Core Data Persistent History Tracking into a timeline.

Works on ANY Apple Core Data SQLite store that has the persistent-history
tables (ACHANGE / ATRANSACTION / ATRANSACTIONSTRING): Photos.sqlite,
NoteStore.sqlite, healthdb, etc. The schema is discovered at runtime, so it
adapts to different Core Data versions and different apps.

What it produces
----------------
A flat, timestamped change log — one row per changed managed object — with:
    timestamp_utc, change (INSERT/UPDATE/DELETE), entity, entity_pk,
    label (best-effort human label for the record), txn_id,
    and one column per transaction author field (author, bundleid, ...).
Optionally: a session-grouped summary and a deletions report that surfaces
the tombstone values preserved for deleted objects.

Design notes
------------
* The DB is opened read-only and immutable (no -wal/-shm/journal is created),
  so it is safe to run against evidence.
* No third-party dependencies — standard library only.
* Entity code -> backing table is resolved by scanning every Z* table for a
  Z_ENT column that contains the code. This handles inheritance quirks (e.g.
  the Photos "Asset" entity is stored in ZGENERICASSET, not ZASSET).

Usage
-----
    python3 coredata_history.py LIBRARY.sqlite -o changes.csv --summary
    python3 coredata_history.py LIBRARY.sqlite --deletes
    python3 coredata_history.py LIBRARY.sqlite --label Asset=ZGENERICASSET.ZFILENAME
"""

import argparse
import csv
import datetime
import re
import sqlite3
import sys
import zoneinfo

# Core Data / Cocoa reference epoch: 2001-01-01 00:00:00 UTC
COCOA_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)

# NSPersistentHistoryChangeType enum
CHANGE_TYPES = {0: "INSERT", 1: "UPDATE", 2: "DELETE"}

# Columns tried, in order, when auto-labelling a record (after Z-prefix forms)
DEFAULT_LABEL_COLUMNS = [
    "ZFILENAME", "ZTITLE", "ZNAME", "ZDISPLAYNAME", "ZIDENTIFIER",
    "ZURLSTRING", "ZSNIPPET", "ZUUID", "ZUNIQUEIDENTIFIER",
    "FILENAME", "TITLE", "NAME",
]

REQUIRED_TABLES = ("ACHANGE", "ATRANSACTION", "ATRANSACTIONSTRING")


def cocoa_to_dt(value):
    """Convert a Cocoa/Core Data absolute timestamp (float seconds) to UTC datetime."""
    if value is None:
        return None
    try:
        return COCOA_EPOCH + datetime.timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def fmt(dt, tz=None):
    if not dt:
        return ""
    if tz is not None:
        dt = dt.astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


class CoreDataHistory:
    def __init__(self, path):
        # immutable=1 => never touch the file; safe for read-only / evidence.
        uri = "file:{}?immutable=1".format(path.replace("?", "%3f"))
        try:
            self.con = sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError:
            # Fallback for environments without immutable support
            self.con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        self.con.text_factory = bytes  # read everything as bytes; decode ourselves
        self.cur = self.con.cursor()
        self._verify()
        self._load_schema()

    # ----- setup -------------------------------------------------------------
    def _tables(self):
        rows = self.cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {self._s(r[0]) for r in rows}

    def _columns(self, table):
        rows = self.cur.execute("PRAGMA table_info({})".format(table)).fetchall()
        return [self._s(r[1]) for r in rows]

    @staticmethod
    def _s(v):
        """Decode a possibly-bytes value to str (for names/text)."""
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v).decode("utf-8", "replace")
        return v

    def _verify(self):
        present = self._tables()
        missing = [t for t in REQUIRED_TABLES if t not in present]
        if missing:
            raise SystemExit(
                "Not a persistent-history store — missing table(s): "
                + ", ".join(missing)
            )

    def _load_schema(self):
        # Entity code -> entity name
        self.entity_name = {}
        if "Z_PRIMARYKEY" in self._tables():
            for ent, name in self.cur.execute(
                "SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY"
            ):
                self.entity_name[ent] = self._s(name)

        # Interned string pool: pk -> string
        self.strings = {
            pk: self._s(name)
            for pk, name in self.cur.execute(
                "SELECT Z_PK, ZNAME FROM ATRANSACTIONSTRING"
            )
        }

        # Discover ATRANSACTION author-style fields.
        # Each interned reference column ends in 'TS' and has a sibling inline
        # column with the 'TS' stripped (e.g. ZAUTHORTS <-> ZAUTHOR).
        tcols = self._columns("ATRANSACTION")
        tset = set(tcols)
        self.author_fields = []  # (label, ref_col_or_None, inline_col_or_None)
        for c in tcols:
            if c.endswith("TS") and c[:-2] in tset and c not in ("ZTIMESTAMP",):
                inline = c[:-2]
                label = inline[1:].lower() if inline.startswith("Z") else inline.lower()
                self.author_fields.append((label, c, inline))
        # Inline-only author columns (rare): ZAUTHOR with no ZAUTHORTS
        for c in tcols:
            if c.startswith("Z") and (c + "TS") not in tset:
                base = c[1:].lower()
                if base in ("author", "bundleid", "contextname", "processid") and \
                   not any(f[0] == base for f in self.author_fields):
                    self.author_fields.append((base, None, c))

        self.has_timestamp = "ZTIMESTAMP" in tset

        # Discover ACHANGE tombstone columns
        ccols = self._columns("ACHANGE")
        self.tombstone_cols = sorted(
            [c for c in ccols if re.fullmatch(r"ZTOMBSTONE\d+", c)],
            key=lambda x: int(re.search(r"\d+", x).group()),
        )
        self.has_columns_bitmap = "ZCOLUMNS" in ccols

        # Build entity-code -> backing table index (for labelling records).
        self._build_entity_table_index()

    def _build_entity_table_index(self):
        """Map each entity code to the table that stores its rows.

        Core Data stores every entity in a table that has Z_PK and Z_ENT
        columns; the Z_ENT column holds the code of the row's concrete entity
        (or a subclass). We scan such tables and record which codes appear.
        """
        self.code_to_table = {}
        self.table_columns = {}
        for tbl in sorted(self._tables()):
            if not tbl.startswith("Z") or tbl.startswith("Z_"):
                continue
            cols = self._columns(tbl)
            if "Z_PK" not in cols or "Z_ENT" not in cols:
                continue
            self.table_columns[tbl] = cols
            try:
                codes = self.cur.execute(
                    "SELECT DISTINCT Z_ENT FROM {}".format(tbl)
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for (code,) in codes:
                # First table wins; concrete entity tables are unambiguous here.
                self.code_to_table.setdefault(code, tbl)

    # ----- labelling ---------------------------------------------------------
    def _pick_label_column(self, table, preference):
        cols = self.table_columns.get(table, [])
        for cand in preference:
            if cand in cols:
                return cand
        return None

    def build_labeller(self, overrides, label_pref):
        """Return a function (entity_code, pk) -> label string (best effort)."""
        # overrides: {entity_name: (table, column)}
        name_to_code = {v: k for k, v in self.entity_name.items()}
        resolved = {}  # entity_code -> (table, column)
        for ent_name, (tbl, col) in overrides.items():
            code = name_to_code.get(ent_name)
            if code is not None and tbl in self.table_columns:
                if col in self.table_columns[tbl]:
                    resolved[code] = (tbl, col)

        cache = {}
        lookup = self.con.cursor()  # independent of the streaming change cursor

        def label(code, pk):
            if pk is None:
                return ""
            key = (code, pk)
            if key in cache:
                return cache[key]
            tbl_col = resolved.get(code)
            if tbl_col is None:
                tbl = self.code_to_table.get(code)
                if tbl:
                    col = self._pick_label_column(tbl, label_pref)
                    tbl_col = (tbl, col) if col else None
                resolved[code] = tbl_col  # cache the (maybe None) resolution
            out = ""
            if tbl_col:
                tbl, col = tbl_col
                try:
                    row = lookup.execute(
                        "SELECT {} FROM {} WHERE Z_PK=?".format(col, tbl), (pk,)
                    ).fetchone()
                    if row and row[0] is not None:
                        out = self._s(row[0])
                except sqlite3.OperationalError:
                    out = ""
            cache[key] = out
            return out

        return label

    # ----- core extraction ---------------------------------------------------
    def iter_changes(self):
        """Yield decoded change dicts, ordered by time then change PK."""
        ref_select = []
        for label, ref_col, inline_col in self.author_fields:
            if ref_col:
                ref_select.append("tr.{} AS {}__ref".format(ref_col, label))
            if inline_col:
                ref_select.append("tr.{} AS {}__inl".format(inline_col, label))
        extra = (", " + ", ".join(ref_select)) if ref_select else ""
        ts = "tr.ZTIMESTAMP" if self.has_timestamp else "NULL"
        tomb = "".join(", c.{}".format(t) for t in self.tombstone_cols)
        cols_bitmap = ", c.ZCOLUMNS" if self.has_columns_bitmap else ""

        sql = (
            "SELECT c.Z_PK, c.ZCHANGETYPE, c.ZENTITY, c.ZENTITYPK, "
            "c.ZTRANSACTIONID, {ts} AS ztime{extra}{tomb}{bitmap} "
            "FROM ACHANGE c "
            "LEFT JOIN ATRANSACTION tr ON c.ZTRANSACTIONID = tr.Z_PK "
            "ORDER BY ztime, c.Z_PK"
        ).format(ts=ts, extra=extra, tomb=tomb, bitmap=cols_bitmap)

        cursor = self.con.cursor()  # dedicated cursor: label lookups use another
        cursor.execute(sql)
        names = [d[0] for d in cursor.description]
        for row in cursor:
            rec = dict(zip(names, row))
            change = {
                "change_pk": rec["Z_PK"],
                "change": CHANGE_TYPES.get(rec["ZCHANGETYPE"], rec["ZCHANGETYPE"]),
                "entity_code": rec["ZENTITY"],
                "entity": self.entity_name.get(rec["ZENTITY"], "ent{}".format(rec["ZENTITY"])),
                "entity_pk": rec["ZENTITYPK"],
                "txn_id": rec["ZTRANSACTIONID"],
                "dt": cocoa_to_dt(rec.get("ztime")),
                "authors": {},
                "tombstones": {},
            }
            for label, ref_col, inline_col in self.author_fields:
                val = ""
                ref = rec.get("{}__ref".format(label))
                if ref is not None:
                    val = self.strings.get(ref, "")
                if not val:
                    inl = rec.get("{}__inl".format(label))
                    if inl is not None:
                        val = self._s(inl)
                change["authors"][label] = val
            for t in self.tombstone_cols:
                change["tombstones"][t] = self._render_tomb(rec.get(t))
            yield change

    @staticmethod
    def _render_tomb(v):
        if v is None:
            return ""
        if isinstance(v, (bytes, bytearray, memoryview)):
            b = bytes(v)
            try:
                txt = b.decode("utf-8")
                if txt.isprintable():
                    return txt
            except UnicodeDecodeError:
                pass
            return "<blob:{}B>".format(len(b))
        return str(v)

    # ----- outputs -----------------------------------------------------------
    def write_csv(self, path, labeller=None, tz=None):
        author_labels = [a[0] for a in self.author_fields]
        ts_label = "timestamp_utc" if tz is None else "timestamp"
        header = [ts_label, "change", "entity", "entity_pk", "label", "txn_id"]
        header += author_labels
        header += self.tombstone_cols
        n = 0
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for ch in self.iter_changes():
                label = labeller(ch["entity_code"], ch["entity_pk"]) if labeller else ""
                row = [fmt(ch["dt"], tz), ch["change"], ch["entity"], ch["entity_pk"],
                       label, ch["txn_id"]]
                row += [ch["authors"].get(a, "") for a in author_labels]
                row += [ch["tombstones"].get(t, "") for t in self.tombstone_cols]
                w.writerow(row)
                n += 1
        return n, header

    def summary(self, session_gap_min=30, tz=None):
        """Group changes into time sessions and tally entity/change counts."""
        gap = datetime.timedelta(minutes=session_gap_min)
        sessions = []
        cur_s = None
        last = None
        total = {"INSERT": 0, "UPDATE": 0, "DELETE": 0}
        for ch in self.iter_changes():
            dt = ch["dt"]
            total[ch["change"]] = total.get(ch["change"], 0) + 1
            if dt is None:
                continue
            if last is None or (dt - last) > gap:
                cur_s = {"start": dt, "end": dt, "tally": {}}
                sessions.append(cur_s)
            cur_s["end"] = dt
            last = dt
            key = (ch["entity"], ch["change"])
            cur_s["tally"][key] = cur_s["tally"].get(key, 0) + 1
        return sessions, total

    def deletions(self, labeller=None, tz=None):
        out = []
        for ch in self.iter_changes():
            if ch["change"] != "DELETE":
                continue
            tombs = {k: v for k, v in ch["tombstones"].items() if v}
            out.append({
                "dt": ch["dt"], "entity": ch["entity"], "entity_pk": ch["entity_pk"],
                "label": labeller(ch["entity_code"], ch["entity_pk"]) if labeller else "",
                "authors": ch["authors"], "tombstones": tombs,
            })
        return out

    def close(self):
        self.con.close()


def parse_label_overrides(items):
    """Parse --label Entity=TABLE.COLUMN flags into {Entity: (TABLE, COLUMN)}."""
    out = {}
    for it in items or []:
        m = re.match(r"^([^=]+)=([^.]+)\.(.+)$", it)
        if not m:
            raise SystemExit("Bad --label '{}'. Use Entity=TABLE.COLUMN".format(it))
        out[m.group(1)] = (m.group(2), m.group(3))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Decode Core Data persistent history (ACHANGE/ATRANSACTION/"
                    "ATRANSACTIONSTRING) into a timeline.")
    ap.add_argument("database", help="Path to the Core Data .sqlite store")
    ap.add_argument("-o", "--out", help="Write full change log to this CSV path")
    ap.add_argument("--summary", action="store_true",
                    help="Print a session-grouped timeline summary")
    ap.add_argument("--deletes", action="store_true",
                    help="Print deletions with recovered tombstone values")
    ap.add_argument("--session-gap", type=int, default=30,
                    help="Minutes of inactivity that start a new session (default 30)")
    ap.add_argument("--label", action="append", metavar="ENTITY=TABLE.COLUMN",
                    help="Override the column used to label an entity's records "
                         "(repeatable)")
    ap.add_argument("--no-auto-label", action="store_true",
                    help="Disable best-effort automatic record labelling")
    ap.add_argument("--timezone", metavar="TZ",
                    help="Convert timestamps to this timezone (e.g. Europe/Amsterdam, "
                         "America/New_York). Default: UTC")
    args = ap.parse_args(argv)

    tz = None
    if args.timezone:
        try:
            tz = zoneinfo.ZoneInfo(args.timezone)
        except zoneinfo.ZoneInfoNotFoundError:
            raise SystemExit("Unknown timezone: {}".format(args.timezone))

    h = CoreDataHistory(args.database)
    overrides = parse_label_overrides(args.label)
    labeller = None
    if not args.no_auto_label or overrides:
        labeller = h.build_labeller(overrides, DEFAULT_LABEL_COLUMNS)

    # Always print a short header about what was found.
    span_lo = span_hi = None
    if h.has_timestamp:
        row = h.cur.execute(
            "SELECT MIN(ZTIMESTAMP), MAX(ZTIMESTAMP) FROM ATRANSACTION"
        ).fetchone()
        span_lo, span_hi = cocoa_to_dt(row[0]), cocoa_to_dt(row[1])
    n_changes = h.cur.execute("SELECT COUNT(*) FROM ACHANGE").fetchone()[0]
    n_txn = h.cur.execute("SELECT COUNT(*) FROM ATRANSACTION").fetchone()[0]
    print("Store: {}".format(args.database))
    print("Changes: {}  Transactions: {}  Interned strings: {}".format(
        n_changes, n_txn, len(h.strings)))
    print("Author fields: {}".format(
        ", ".join(a[0] for a in h.author_fields) or "(none)"))
    print("Tombstone columns: {}".format(", ".join(h.tombstone_cols) or "(none)"))
    if span_lo:
        print("Activity span: {} -> {}".format(fmt(span_lo, tz), fmt(span_hi, tz)))
    print()

    if args.out:
        n, header = h.write_csv(args.out, labeller, tz=tz)
        print("Wrote {} rows to {}".format(n, args.out))
        print("Columns: {}".format(", ".join(header)))
        print()

    if args.summary:
        sessions, total = h.summary(args.session_gap, tz=tz)
        print("=== TIMELINE BY SESSION (gap > {} min) ===".format(args.session_gap))
        for s in sessions:
            start = s["start"].astimezone(tz) if tz else s["start"]
            end = s["end"].astimezone(tz) if tz else s["end"]
            span = fmt(s["start"], tz)
            if s["end"] != s["start"]:
                span += end.strftime("  ->  %H:%M:%S")
            parts = ["{} {}x{}".format(e, ct, c)
                     for (e, ct), c in sorted(s["tally"].items(), key=lambda x: -x[1])]
            print("\n{}".format(span))
            print("   " + ", ".join(parts[:12]) + (" ..." if len(parts) > 12 else ""))
        print("\nTotals: " + ", ".join("{} {}".format(v, k) for k, v in total.items()))
        print()

    if args.deletes:
        dels = h.deletions(labeller, tz=tz)
        print("=== DELETIONS ({}) ===".format(len(dels)))
        for d in dels:
            who = d["authors"].get("processid") or d["authors"].get("author") or ""
            line = "{}  {}#{}".format(fmt(d["dt"], tz), d["entity"], d["entity_pk"])
            if d["label"]:
                line += "  ({})".format(d["label"])
            if who:
                line += "  by {}".format(who)
            print(line)
            for col, val in d["tombstones"].items():
                print("      preserved {} = {}".format(col, val))
        if not dels:
            print("(none)")

    h.close()


if __name__ == "__main__":
    main()
