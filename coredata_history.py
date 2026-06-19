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
import plistlib
import re
import sqlite3
import sys
import zlib

# Core Data / Cocoa reference epoch: 2001-01-01 00:00:00 UTC
COCOA_EPOCH = datetime.datetime(2001, 1, 1)

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


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


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

        # Physical-column fallback property order (used only when the compiled
        # model cache is unavailable). Attributes / to-one relationships are
        # stored as columns in alphabetical (property) order; to-many
        # relationships have no column, so this is best-effort.
        self.entity_props = {}
        system = {"Z_PK", "Z_ENT", "Z_OPT"}
        for code, tbl in self.code_to_table.items():
            cols = [c for c in self.table_columns.get(tbl, []) if c not in system]
            self.entity_props[code] = cols
        # Optional authoritative overrides: {entity_code: [name, name, ...]}
        self.property_overrides = {}
        # Exact reference-ID -> name maps decoded from Z_MODELCACHE, keyed by
        # backing table (reference IDs are shared across an inheritance
        # hierarchy, i.e. across entities that share one table).
        self.model_refmap_by_table = {}
        self.model_cache_status = "absent"
        self._load_model_cache()

    def _load_model_cache(self):
        """Decode Z_MODELCACHE (the compiled NSManagedObjectModel) to build
        exact {referenceID: propertyName} maps per backing table.

        Z_MODELCACHE stores a raw-zlib-compressed NSKeyedArchiver binary plist.
        Each NSEntityDescription lists NSProperties; each property proxy carries
        an explicit NSReferenceID that equals its bit position in ZCOLUMNS, plus
        a name (directly or via NSUnderlyingProperty). Relationships stored as
        direct descriptions may lack a reference ID; those bits stay unmapped.
        """
        if "Z_MODELCACHE" not in self._tables():
            return
        try:
            row = self.con.cursor().execute(
                "SELECT Z_CONTENT FROM Z_MODELCACHE LIMIT 1").fetchone()
            if not row or row[0] is None:
                self.model_cache_status = "empty"
                return
            blob = bytes(row[0])
            data = None
            for fn in (lambda b: zlib.decompress(b, -15),
                       lambda b: zlib.decompress(b),
                       lambda b: __import__("gzip").decompress(b),
                       lambda b: __import__("lzma").decompress(b)):
                try:
                    data = fn(blob)
                    break
                except Exception:
                    continue
            if data is None:
                self.model_cache_status = "undecodable (unknown compression)"
                return
            pl = plistlib.loads(data)
            objs = pl.get("$objects") if isinstance(pl, dict) else None
            if not isinstance(objs, list):
                self.model_cache_status = "unrecognized archive"
                return

            def D(x):
                return objs[x.data] if isinstance(x, plistlib.UID) else x

            def classname(o):
                if isinstance(o, dict) and "$class" in o:
                    c = D(o["$class"])
                    return c.get("$classname") if isinstance(c, dict) else None
                return None

            def text(v):
                v = D(v)
                if isinstance(v, (bytes, bytearray)):
                    return bytes(v).decode("utf-8", "replace")
                return v

            name_to_code = {v: k for k, v in self.entity_name.items()}
            covered = set()
            # Each _NSPropertyDescriptionProxy carries its reference ID, a back
            # reference to its entity, and (via NSUnderlyingProperty) its name.
            # Scanning all proxies globally covers more entities than walking
            # each entity's NSProperties array. Reference IDs are shared across a
            # backing table's inheritance hierarchy, so we key the map by table.
            for o in objs:
                if classname(o) != "_NSPropertyDescriptionProxy":
                    continue
                if "NSReferenceID" not in o:
                    continue
                rid = D(o["NSReferenceID"])
                if not isinstance(rid, int):
                    continue
                ed = D(o.get("NSEntityDescription"))
                ent_name = text(ed.get("NSEntityName")) if isinstance(ed, dict) else None
                code = name_to_code.get(ent_name) if ent_name else None
                tbl = self.code_to_table.get(code) if code is not None else None
                if tbl is None:
                    continue
                nm = None
                up = o.get("NSUnderlyingProperty")
                if up is not None:
                    upd = D(up)
                    if isinstance(upd, dict):
                        for k in ("NSPropertyName", "NSName"):
                            if k in upd:
                                nm = text(upd[k]); break
                if nm is None:
                    for k in ("NSPropertyName", "NSName"):
                        if k in o:
                            nm = text(o[k]); break
                if nm:
                    self.model_refmap_by_table.setdefault(tbl, {})[rid] = nm
                    covered.add(ent_name)
            n_ent = len(covered)
            self.model_cache_status = (
                "loaded — exact names for {} entit{} (others use column "
                "fallback)".format(n_ent, "y" if n_ent == 1 else "ies")
                if n_ent else "parsed, no reference IDs found")
        except Exception as exc:  # never let model parsing break the run
            self.model_cache_status = "error: {}".format(exc.__class__.__name__)

    def set_property_overrides(self, overrides_by_name):
        """overrides_by_name: {entity_name: [ordered property names]}."""
        name_to_code = {v: k for k, v in self.entity_name.items()}
        for ent_name, props in overrides_by_name.items():
            code = name_to_code.get(ent_name)
            if code is not None:
                self.property_overrides[code] = props

    def decode_columns(self, entity_code, blob, sep=";"):
        """Decode a ZCOLUMNS bitmap into a separated list of property names.

        Bits are read least-significant-first within each byte; bit i is
        property reference-ID i. Resolution order:
          1. an explicit --property-map override (position-indexed list),
          2. the exact reference-ID map from Z_MODELCACHE,
          3. the physical-column fallback (best-effort, alphabetical).
        Any bit that cannot be resolved is emitted as 'Unknown_<index>'.
        """
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            return ""
        data = bytes(blob)
        override = self.property_overrides.get(entity_code)          # list or None
        refmap = None
        phys = None
        if override is None:
            tbl = self.code_to_table.get(entity_code)
            refmap = self.model_refmap_by_table.get(tbl) if tbl else None  # dict or None
            if not refmap:  # absent OR empty -> use physical-column fallback
                refmap = None
                phys = self.entity_props.get(entity_code, [])         # list
        names = []
        for byte_index, value in enumerate(data):
            for bit in range(8):
                if value >> bit & 1:
                    idx = byte_index * 8 + bit
                    nm = None
                    if override is not None:
                        nm = override[idx] if idx < len(override) else None
                    elif refmap is not None:
                        nm = refmap.get(idx)
                    elif phys is not None:
                        nm = phys[idx] if idx < len(phys) else None
                    names.append(nm if nm else "Unknown_{}".format(idx))
        return sep.join(names)

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
                "updated_columns": self.decode_columns(
                    rec["ZENTITY"], rec.get("ZCOLUMNS")
                ) if self.has_columns_bitmap else "",
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
    def write_csv(self, path, labeller=None):
        author_labels = [a[0] for a in self.author_fields]
        header = ["timestamp_utc", "change", "entity", "entity_pk", "label",
                  "updated_columns", "txn_id"]
        header += author_labels
        header += self.tombstone_cols
        n = 0
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for ch in self.iter_changes():
                label = labeller(ch["entity_code"], ch["entity_pk"]) if labeller else ""
                row = [fmt(ch["dt"]), ch["change"], ch["entity"], ch["entity_pk"],
                       label, ch["updated_columns"], ch["txn_id"]]
                row += [ch["authors"].get(a, "") for a in author_labels]
                row += [ch["tombstones"].get(t, "") for t in self.tombstone_cols]
                w.writerow(row)
                n += 1
        return n, header

    def summary(self, session_gap_min=30):
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

    def deletions(self, labeller=None):
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
    ap.add_argument("--property-map", action="append",
                    metavar="ENTITY=prop0,prop1,...",
                    help="Authoritative ordered property list for an entity, used "
                         "to decode the ZCOLUMNS updated-column bitmap exactly "
                         "(repeatable). Without this, decoding is best-effort.")
    args = ap.parse_args(argv)

    h = CoreDataHistory(args.database)
    overrides = parse_label_overrides(args.label)
    if args.property_map:
        pmap = {}
        for item in args.property_map:
            if "=" not in item:
                raise SystemExit("Bad --property-map '{}'. Use ENTITY=p0,p1,..."
                                 .format(item))
            ent, props = item.split("=", 1)
            pmap[ent] = [p for p in props.split(",") if p]
        h.set_property_overrides(pmap)
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
    print("Model cache (exact column names): {}".format(h.model_cache_status))
    if span_lo:
        print("Activity span: {} -> {} (UTC)".format(fmt(span_lo), fmt(span_hi)))
    print()

    if args.out:
        n, header = h.write_csv(args.out, labeller)
        print("Wrote {} rows to {}".format(n, args.out))
        print("Columns: {}".format(", ".join(header)))
        print()

    if args.summary:
        sessions, total = h.summary(args.session_gap)
        print("=== TIMELINE BY SESSION (gap > {} min) ===".format(args.session_gap))
        for s in sessions:
            span = fmt(s["start"])
            if s["end"] != s["start"]:
                span += s["end"].strftime("  ->  %H:%M:%S")
            parts = ["{} {}x{}".format(e, ct, c)
                     for (e, ct), c in sorted(s["tally"].items(), key=lambda x: -x[1])]
            print("\n{}".format(span))
            print("   " + ", ".join(parts[:12]) + (" ..." if len(parts) > 12 else ""))
        print("\nTotals: " + ", ".join("{} {}".format(v, k) for k, v in total.items()))
        print()

    if args.deletes:
        dels = h.deletions(labeller)
        print("=== DELETIONS ({}) ===".format(len(dels)))
        for d in dels:
            who = d["authors"].get("processid") or d["authors"].get("author") or ""
            line = "{}  {}#{}".format(fmt(d["dt"]), d["entity"], d["entity_pk"])
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
