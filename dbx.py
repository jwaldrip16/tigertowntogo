"""
Database layer for Fleet Foot Delivery.

SQLite by default (one file on disk). When DATABASE_URL is set (Railway Postgres), the
same app code runs on Postgres: this file translates the app's SQLite-style SQL on the fly
(? placeholders, INSERT OR REPLACE / OR IGNORE, date()/datetime()/strftime(), LIKE, etc.),
hands back rows that work like sqlite3.Row, and copies the old SQLite file into Postgres
once, the first time it starts on Postgres.
"""
import os
import re
import sqlite3
import threading
import weakref

DATABASE_URL = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or "").strip()
PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
TZ = os.environ.get("TZ", "America/Chicago")
NOW_SQL = "datetime('now','localtime')"

IntegrityError = (sqlite3.IntegrityError,)
OperationalError = (sqlite3.OperationalError,)

if PG:
    import psycopg
    from psycopg.adapt import Dumper, Loader
    from psycopg_pool import ConnectionPool
    IntegrityError = (sqlite3.IntegrityError, psycopg.IntegrityError)
    OperationalError = (sqlite3.OperationalError, psycopg.Error)


# ------------------------------------------------------------------ sqlite

def _sqlite_connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def connect(path):
    if PG:
        return PGConn(_pool().getconn())
    return _sqlite_connect(path)


def tune(con):
    if PG:
        return
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=4000")


def columns(con, table):
    if PG:
        rows = con.raw("""SELECT column_name FROM information_schema.columns
                          WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position""",
                       (table.lower(),))
        return [r[0] for r in rows]
    rows = con.execute("PRAGMA table_info(" + table + ")").fetchall()
    return [r["name"] for r in rows]


def where():
    if PG:
        return "Postgres"
    return "SQLite (" + os.environ.get("DB_PATH", "delivery.db") + ")"


# ------------------------------------------------------------------ postgres

_POOL = None
_POOL_LOCK = threading.Lock()


def _pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                url = DATABASE_URL
                if url.startswith("postgres://"):
                    url = "postgresql://" + url[len("postgres://"):]
                _POOL = ConnectionPool(url, min_size=1, max_size=int(os.environ.get("PG_POOL_MAX", "25")),
                                       kwargs={"autocommit": True}, configure=_configure, timeout=30,
                                       open=True)
    return _POOL


if PG:
    class _IntUnknown(Dumper):
        """Send Python ints as untyped values so they compare with TEXT or INTEGER columns
        the way SQLite did."""
        oid = 0

        def dump(self, obj):
            return str(int(obj)).encode()

    class _NumLoader(Loader):
        """SUM()/AVG() come back as whole numbers or floats, like SQLite, never Decimal."""
        def load(self, data):
            s = bytes(data).decode()
            if s in ("NaN", "Infinity", "-Infinity"):
                return float(s)
            if "." in s or "e" in s.lower():
                f = float(s)
                return int(f) if f.is_integer() and "." not in s.rstrip("0").rstrip(".") else f
            return int(s)

    class _BoolLoader(Loader):
        def load(self, data):
            return 1 if bytes(data) in (b"t", b"true", b"1") else 0


def _configure(conn):
    conn.adapters.register_dumper(int, _IntUnknown)
    conn.adapters.register_dumper(bool, _IntUnknown)
    conn.adapters.register_loader("numeric", _NumLoader)
    conn.adapters.register_loader("bool", _BoolLoader)
    with conn.cursor() as c:
        c.execute("SET TIME ZONE %s" % _lit(TZ))


def _lit(s):
    return "'" + str(s).replace("'", "''") + "'"


class Row(tuple):
    """Works like sqlite3.Row: row["name"] (any case), row[0], row.keys(), dict(row)."""
    __slots__ = ()
    _names = ()
    _idx = {}

    def __new__(cls, values, names, idx):
        r = tuple.__new__(cls, values)
        return r

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return tuple.__getitem__(self, self._idx_of(k))
            except KeyError:
                raise IndexError("No item with that key")
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._names)


def _row_class(names):
    names = tuple(names)
    idx = {}
    for i, n in enumerate(names):
        idx.setdefault(n, i)
        idx.setdefault(n.lower(), i)

    class _R(Row):
        __slots__ = ()
        _names = names

        def _idx_of(self, k, _idx=idx):
            if k in _idx:
                return _idx[k]
            return _idx[k.lower()]
    return _R


class PGCursor:
    def __init__(self, rows=None, rowcount=-1, lastrowid=None, description=None):
        self._rows = rows or []
        self._i = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = description

    def fetchone(self):
        if self._i < len(self._rows):
            r = self._rows[self._i]
            self._i += 1
            return r
        return None

    def fetchall(self):
        r = self._rows[self._i:]
        self._i = len(self._rows)
        return r

    def fetchmany(self, n=1):
        r = self._rows[self._i:self._i + n]
        self._i += len(r)
        return r

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        pass


def _giveback(conn):
    try:
        _pool().putconn(conn)
    except Exception:
        pass


class PGConn:
    """sqlite3.Connection look-alike on top of a pooled Postgres connection (autocommit)."""

    def __init__(self, conn):
        self._c = conn
        self._fin = weakref.finalize(self, _giveback, conn)
        self.row_factory = None

    # sqlite-style API
    def execute(self, sql, params=()):
        plan = translate(sql)
        if plan is None:
            return PGCursor(rowcount=0)
        q, kind, table, cols = plan
        if isinstance(params, dict):
            params = {k: v for k, v in params.items()}
        else:
            params = tuple(params or ())
        returning = False
        if kind:
            q, returning = self._finish_insert(q, kind, table, cols)
        with self._c.cursor() as cur:
            cur.execute(q, params)
            rows, desc, lastid = [], cur.description, None
            if desc is not None:
                data = cur.fetchall()
                if returning:
                    lastid = data[0][0] if data else None
                    desc = None
                else:
                    R = _row_class([d.name for d in desc])
                    rows = [R(v, None, None) for v in data]
            return PGCursor(rows, cur.rowcount, lastid, desc)

    def executemany(self, sql, seq):
        n = 0
        for p in seq:
            c = self.execute(sql, p)
            n += max(c.rowcount, 0)
        return PGCursor(rowcount=n)

    def executescript(self, script):
        for stmt in split_sql(script):
            self.execute(stmt)
        return PGCursor()

    def raw(self, q, params=None):
        with self._c.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall() if cur.description is not None else []

    def commit(self):
        pass

    def rollback(self):
        pass

    def cursor(self):
        return _CursorShim(self)

    def close(self):
        if self._fin.alive:
            self._fin()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # INSERT helpers
    def _finish_insert(self, q, kind, table, cols):
        info = table_info(self, table)
        if not info:
            return q, False
        tail = ""
        if kind == "ignore":
            tail = " ON CONFLICT DO NOTHING"
        elif kind == "replace":
            use = cols or info["cols"]
            key = None
            for uk in info["uniques"]:
                if all(c in use for c in uk):
                    key = uk
                    break
            if key:
                rest = [c for c in use if c not in key]
                tail = " ON CONFLICT (" + ",".join(key) + ") " + (
                    "DO UPDATE SET " + ",".join(c + "=EXCLUDED." + c for c in rest) if rest else "DO NOTHING")
        returning = False
        if "id" in info["cols"] and not re.search(r"\bRETURNING\b", q, re.I):
            tail += " RETURNING id"
            returning = True
        return q.rstrip().rstrip(";") + tail, returning


class _CursorShim:
    def __init__(self, con):
        self._con = con
        self._cur = PGCursor()

    def execute(self, sql, params=()):
        self._cur = self._con.execute(sql, params)
        return self

    def executemany(self, sql, seq):
        self._cur = self._con.executemany(sql, seq)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur.fetchall())

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    def close(self):
        pass


_TINFO = {}
_TINFO_LOCK = threading.Lock()


def table_info(con, table):
    t = table.lower()
    if t in _TINFO:
        return _TINFO[t]
    cols = [r[0] for r in con.raw("""SELECT column_name FROM information_schema.columns
                                     WHERE table_schema=current_schema() AND table_name=%s
                                     ORDER BY ordinal_position""", (t,))]
    if not cols:
        return None
    uq = con.raw("""SELECT i.indisprimary, array_agg(a.attname ORDER BY k.n) FROM pg_index i
                    JOIN pg_class c ON c.oid=i.indrelid
                    JOIN pg_namespace ns ON ns.oid=c.relnamespace AND ns.nspname=current_schema()
                    CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, n)
                    JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum
                    WHERE c.relname=%s AND i.indisunique AND i.indpred IS NULL
                    GROUP BY i.indexrelid, i.indisprimary ORDER BY i.indisprimary DESC""", (t,))
    info = {"cols": cols, "uniques": [list(r[1]) for r in uq]}
    with _TINFO_LOCK:
        _TINFO[t] = info
    return info


def forget_table_info():
    _TINFO.clear()


# ------------------------------------------------------------------ SQL translation

_CACHE = {}
_STR = "\x00%d\x00"


def _mask(sql):
    """Pull out string literals and quoted names so rewrites only touch SQL itself.
    Turns ? into %s and doubles every % for the Postgres driver."""
    out, lits, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            lits.append(sql[i:j + 1].replace("%", "%%"))
            out.append(_STR % (len(lits) - 1))
            i = j + 1
        elif ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            i = j
        elif ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif ch == "?":
            out.append("%s")
            i += 1
        elif ch == "%":
            out.append("%%")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), lits


def _unmask(s, lits):
    return re.sub("\x00(\\d+)\x00", lambda m: lits[int(m.group(1))], s)


def split_sql(script):
    """Split a script into statements (semicolons outside strings, triggers kept whole)."""
    masked, lits = _mask(script)
    masked = masked.replace("%%", "%").replace("%s", "?")
    parts, buf, depth_trigger = [], [], False
    for piece in re.split(r"(;)", masked):
        if piece == ";":
            stmt = "".join(buf)
            if depth_trigger and not re.search(r"\bEND\s*$", stmt.strip(), re.I):
                buf.append(";")
                continue
            if stmt.strip():
                parts.append(_unmask(stmt, [l.replace("%%", "%") for l in lits]))
            buf, depth_trigger = [], False
            continue
        if not buf and re.match(r"\s*CREATE\s+TRIGGER\b", piece, re.I):
            depth_trigger = True
        buf.append(piece)
    rest = "".join(buf)
    if rest.strip():
        parts.append(_unmask(rest, [l.replace("%%", "%") for l in lits]))
    return parts


def _scalar_minmax(s):
    """SQLite MAX(a,b) / MIN(a,b) with two or more arguments -> GREATEST / LEAST."""
    out, i = [], 0
    for m in re.finditer(r"\b(MAX|MIN)\s*\(", s, re.I):
        pass
    res, pos = [], 0
    pat = re.compile(r"\b(MAX|MIN)\s*\(", re.I)
    while True:
        m = pat.search(s, pos)
        if not m:
            res.append(s[pos:])
            break
        depth, j, top_comma = 1, m.end(), False
        while j < len(s) and depth:
            c = s[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == "," and depth == 1:
                top_comma = True
            j += 1
        res.append(s[pos:m.start()])
        name = m.group(1).upper()
        if top_comma:
            res.append(("GREATEST" if name == "MAX" else "LEAST") + "(")
        else:
            res.append(s[m.start():m.end()])
        pos = m.end()
    return "".join(res)


_TYPE_MAP = [(re.compile(r"\bid\s+INTEGER\s+PRIMARY\s+KEY(\s+AUTOINCREMENT)?", re.I), "id BIGSERIAL PRIMARY KEY"),
             (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.I), "BIGSERIAL PRIMARY KEY"),
             # types only (a word after a column name), so a column called "blob" keeps its name
             (re.compile(r"(\b\w+\s+)INTEGER\b", re.I), r"\1BIGINT"),
             (re.compile(r"(\b\w+\s+)REAL\b", re.I), r"\1DOUBLE PRECISION"),
             (re.compile(r"(\b\w+\s+)BLOB\b", re.I), r"\1BYTEA"),
             (re.compile(r"(\b\w+\s+)(DATETIME|TIMESTAMP)\b", re.I), r"\1TEXT"),
             (re.compile(r"\s+COLLATE\s+NOCASE", re.I), "")]


def translate(sql):
    hit = _CACHE.get(sql)
    if hit is not None or sql in _CACHE:
        return hit
    s, lits = _mask(sql)
    head = s.lstrip()[:40].upper()
    plan = None
    if head.startswith(("PRAGMA", "VACUUM", "ANALYZE", "REINDEX", "CREATE TRIGGER", "DROP TRIGGER",
                        "BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE")) or not s.strip():
        _CACHE[sql] = None
        return None
    kind, table, cols = None, None, None
    if head.startswith(("CREATE TABLE", "ALTER TABLE")):
        for pat, rep in _TYPE_MAP:
            s = pat.sub(rep, s)
    else:
        m = re.match(r"\s*(?:INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO|REPLACE\s+INTO|INSERT\s+INTO)\s+([\w\"]+)\s*(\(([^)]*)\))?",
                     s, re.I)
        if m:
            how = (m.group(1) or ("REPLACE" if s.lstrip().upper().startswith("REPLACE") else "")).lower()
            kind = how or "insert"
            table = m.group(2).strip('"')
            if m.group(4) and not re.match(r"\s*SELECT\b", m.group(4), re.I):
                cols = [c.strip().strip('"').lower() for c in m.group(4).split(",")]
            s = s[:m.start()] + re.sub(r"INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO|REPLACE\s+INTO", "INSERT INTO",
                                       s[m.start():m.end()], count=1, flags=re.I) + s[m.end():]
        s = re.sub(r"(?<![\w.])(datetime|date|time|strftime|julianday)\s*\(", lambda x: "sqlite_" + x.group(1).lower() + "(", s, flags=re.I)
        s = re.sub(r"(?<![\w.])IFNULL\s*\(", "COALESCE(", s, flags=re.I)
        s = re.sub(r"([\w.]+(?:\([^()]*\))?)\s+COLLATE\s+NOCASE", r"lower(\1)", s, flags=re.I)
        s = re.sub(r"(?<![\w])(?<!I)LIKE\b", "ILIKE", s, flags=re.I)
        s = re.sub(r"\bIS\s+NOT\s+(%s|\x00\d+\x00|[\w.]+)(?<!NULL)(?<!TRUE)(?<!FALSE)",
                   lambda x: x.group(0) if x.group(1).upper() in ("NULL", "TRUE", "FALSE", "DISTINCT") else "IS DISTINCT FROM " + x.group(1), s, flags=re.I)
        s = re.sub(r"\bIS\s+(?!NOT\b|NULL\b|TRUE\b|FALSE\b|DISTINCT\b)(%s|\x00\d+\x00|[\w.]+)",
                   r"IS NOT DISTINCT FROM \1", s, flags=re.I)
        s = _scalar_minmax(s)
    q = _unmask(s, lits)
    plan = (q, kind, table, cols)
    if len(_CACHE) < 5000:
        _CACHE[sql] = plan
    return plan


# ------------------------------------------------------------------ Postgres helpers

PG_FUNCS = r"""
CREATE OR REPLACE FUNCTION sqlite_ts(VARIADIC a text[]) RETURNS timestamp LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE
  base text := btrim(a[1]);
  t timestamp;
  m text;
  i int;
  n double precision;
  unit text;
  tz text := __TZ__;
  unixepoch boolean := false;
BEGIN
  IF base IS NULL THEN RETURN NULL; END IF;
  FOR i IN 2..coalesce(array_length(a,1),1) LOOP
    IF lower(btrim(a[i])) = 'unixepoch' THEN unixepoch := true; END IF;
  END LOOP;
  IF lower(base) = 'now' THEN
    t := (now() AT TIME ZONE 'UTC');
  ELSIF unixepoch OR base ~ '^-?[0-9]+(\.[0-9]+)?$' AND NOT base ~ '^[0-9]{4}-' THEN
    IF unixepoch THEN t := (to_timestamp(base::double precision) AT TIME ZONE 'UTC');
    ELSE t := timestamp '4714-11-24 12:00:00 BC' + (base::double precision) * interval '1 day'; END IF;
  ELSE
    BEGIN
      t := replace(base, 'T', ' ')::timestamp;
    EXCEPTION WHEN others THEN
      BEGIN
        IF base ~ '^[0-9]{2}:[0-9]{2}' THEN t := ('2000-01-01 ' || base)::timestamp; ELSE RETURN NULL; END IF;
      EXCEPTION WHEN others THEN RETURN NULL; END;
    END;
  END IF;
  FOR i IN 2..coalesce(array_length(a,1),1) LOOP
    m := lower(btrim(a[i]));
    IF m IS NULL THEN RETURN NULL; END IF;
    IF m = 'localtime' THEN
      t := (t AT TIME ZONE 'UTC') AT TIME ZONE tz;
    ELSIF m = 'utc' THEN
      t := (t AT TIME ZONE tz) AT TIME ZONE 'UTC';
    ELSIF m = 'unixepoch' THEN
      NULL;
    ELSIF m = 'start of day' THEN t := date_trunc('day', t);
    ELSIF m = 'start of month' THEN t := date_trunc('month', t);
    ELSIF m = 'start of year' THEN t := date_trunc('year', t);
    ELSIF m ~ '^weekday [0-6]$' THEN
      t := t + ((substr(m, 9, 1)::int - extract(dow from t)::int + 7) % 7) * interval '1 day';
    ELSIF m ~ '^[+-]?\s*[0-9]+(\.[0-9]+)?\s+[a-z]+$' THEN
      n := (regexp_match(m, '^([+-]?\s*[0-9]+(\.[0-9]+)?)'))[1]::text::double precision;
      n := replace((regexp_match(m, '^([+-]?)'))[1] || abs(n)::text, ' ', '')::double precision;
      unit := (regexp_match(m, '([a-z]+)$'))[1];
      unit := rtrim(unit, 's');
      IF unit = 'day' THEN t := t + n * interval '1 day';
      ELSIF unit = 'hour' THEN t := t + n * interval '1 hour';
      ELSIF unit = 'minute' THEN t := t + n * interval '1 minute';
      ELSIF unit = 'second' THEN t := t + n * interval '1 second';
      ELSIF unit = 'month' THEN t := t + (n::int) * interval '1 month';
      ELSIF unit = 'year' THEN t := t + (n::int) * interval '1 year';
      ELSE RETURN NULL; END IF;
    ELSIF m ~ '^[+-]?[0-9]{2}:[0-9]{2}(:[0-9]{2})?$' THEN
      t := t + (CASE WHEN left(m,1)='-' THEN -1 ELSE 1 END) * (ltrim(m, '+-'))::interval;
    ELSE
      RETURN NULL;
    END IF;
  END LOOP;
  RETURN t;
END $f$;

CREATE OR REPLACE FUNCTION sqlite_datetime(VARIADIC a text[]) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
  SELECT to_char(sqlite_ts(VARIADIC a), 'YYYY-MM-DD HH24:MI:SS') $f$;
CREATE OR REPLACE FUNCTION sqlite_date(VARIADIC a text[]) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
  SELECT to_char(sqlite_ts(VARIADIC a), 'YYYY-MM-DD') $f$;
CREATE OR REPLACE FUNCTION sqlite_time(VARIADIC a text[]) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
  SELECT to_char(sqlite_ts(VARIADIC a), 'HH24:MI:SS') $f$;
CREATE OR REPLACE FUNCTION sqlite_julianday(VARIADIC a text[]) RETURNS double precision LANGUAGE sql IMMUTABLE AS $f$
  SELECT extract(epoch from sqlite_ts(VARIADIC a)) / 86400.0 + 2440587.5 $f$;
CREATE OR REPLACE FUNCTION sqlite_strftime(VARIADIC a text[]) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE
  fmt text := a[1];
  t timestamp := sqlite_ts(VARIADIC a[2:]);
  out text := '';
  i int := 1;
  c text;
BEGIN
  IF t IS NULL OR fmt IS NULL THEN RETURN NULL; END IF;
  WHILE i <= length(fmt) LOOP
    c := substr(fmt, i, 1);
    IF c = '%' AND i < length(fmt) THEN
      i := i + 1;
      c := substr(fmt, i, 1);
      out := out || CASE c
        WHEN 'Y' THEN to_char(t, 'YYYY') WHEN 'm' THEN to_char(t, 'MM') WHEN 'd' THEN to_char(t, 'DD')
        WHEN 'H' THEN to_char(t, 'HH24') WHEN 'M' THEN to_char(t, 'MI') WHEN 'S' THEN to_char(t, 'SS')
        WHEN 'f' THEN to_char(t, 'SS.MS') WHEN 'j' THEN to_char(t, 'DDD')
        WHEN 'w' THEN extract(dow from t)::int::text
        WHEN 'u' THEN extract(isodow from t)::int::text
        WHEN 'W' THEN lpad(((extract(doy from t)::int + 6 - ((extract(dow from t)::int + 6) % 7)) / 7)::text, 2, '0')
        WHEN 's' THEN floor(extract(epoch from t))::bigint::text
        WHEN 'J' THEN (extract(epoch from t) / 86400.0 + 2440587.5)::text
        WHEN '%' THEN '%'
        ELSE '%' || c END;
    ELSE
      out := out || c;
    END IF;
    i := i + 1;
  END LOOP;
  RETURN out;
END $f$;

-- SQLite let TEXT and INTEGER be compared directly
CREATE OR REPLACE FUNCTION ff_txt_eq_int(text, bigint) RETURNS boolean LANGUAGE sql IMMUTABLE AS 'SELECT $1 = $2::text';
CREATE OR REPLACE FUNCTION ff_int_eq_txt(bigint, text) RETURNS boolean LANGUAGE sql IMMUTABLE AS 'SELECT $1::text = $2';
CREATE OR REPLACE FUNCTION ff_txt_ne_int(text, bigint) RETURNS boolean LANGUAGE sql IMMUTABLE AS 'SELECT $1 <> $2::text';
CREATE OR REPLACE FUNCTION ff_int_ne_txt(bigint, text) RETURNS boolean LANGUAGE sql IMMUTABLE AS 'SELECT $1::text <> $2';
DO $d$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_operator o JOIN pg_namespace n ON n.oid=o.oprnamespace
                 WHERE o.oprname='=' AND o.oprleft='text'::regtype AND o.oprright='bigint'::regtype
                   AND n.nspname=current_schema()) THEN
    CREATE OPERATOR = (LEFTARG=text, RIGHTARG=bigint, FUNCTION=ff_txt_eq_int);
    CREATE OPERATOR = (LEFTARG=bigint, RIGHTARG=text, FUNCTION=ff_int_eq_txt);
    CREATE OPERATOR <> (LEFTARG=text, RIGHTARG=bigint, FUNCTION=ff_txt_ne_int);
    CREATE OPERATOR <> (LEFTARG=bigint, RIGHTARG=text, FUNCTION=ff_int_ne_txt);
  END IF;
END $d$;

-- SQLite's SUM(a=b) / TOTAL over true/false
CREATE OR REPLACE FUNCTION ff_bool_add(bigint, boolean) RETURNS bigint LANGUAGE sql IMMUTABLE AS
  'SELECT COALESCE($1,0) + CASE WHEN $2 THEN 1 ELSE 0 END';
DO $d$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                 WHERE p.proname='sum' AND n.nspname=current_schema()) THEN
    CREATE AGGREGATE sum(boolean) (SFUNC=ff_bool_add, STYPE=bigint);
  END IF;
END $d$;

CREATE TABLE IF NOT EXISTS ff_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def install_functions(con):
    if not PG:
        return
    con.raw(PG_FUNCS.replace("__TZ__", _lit(TZ)))


_TS = "to_char(localtimestamp, 'YYYY-MM-DD\"T\"HH24:MI:SS')"
_TS_SP = "to_char(localtimestamp, 'YYYY-MM-DD HH24:MI:SS')"

PG_TRIGGERS = """
CREATE OR REPLACE FUNCTION ff_orders_before_ins() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
  IF NEW.kitchen_status = 'pending' THEN NEW.kitchen_sent_at := __TS__; END IF;
  RETURN NEW;
END $f$;

CREATE OR REPLACE FUNCTION ff_orders_before_upd() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
  -- kitchen gate: an order dispatch has not released never sits at the kitchen
  IF TG_ARGV[0] = 'kitchen' AND NEW.kitchen_go = 0 AND NEW.kitchen_status = 'pending' THEN
    NEW.kitchen_status := 'waiting';
    NEW.kitchen_sent_at := NULL;
    NEW.hold_reason := CASE WHEN NEW.dispatch_status IN ('held','queued') THEN 'tap Send to kitchen' ELSE NEW.hold_reason END;
  END IF;
  IF TG_ARGV[0] = 'dispatch' THEN
    IF NEW.dispatch_status = 'delivered' AND NEW.kitchen_status IN ('pending','preparing') THEN
      NEW.kitchen_status := 'ready';
      NEW.ready_at := COALESCE(NEW.ready_at, __TS__);
    ELSIF NEW.dispatch_status = 'cancelled' AND NEW.kitchen_status IN ('pending','preparing') THEN
      NEW.kitchen_status := 'waiting';
    END IF;
  END IF;
  IF NEW.kitchen_status = 'pending' AND OLD.kitchen_status IS DISTINCT FROM 'pending' THEN
    NEW.kitchen_sent_at := __TS__;
  END IF;
  IF NEW.dispatch_status = 'assigned' AND (OLD.dispatch_status IS DISTINCT FROM 'assigned'
       OR NEW.driver_id IS DISTINCT FROM OLD.driver_id OR NEW.paged_at IS DISTINCT FROM OLD.paged_at) THEN
    NEW.driver_paged_at := __TS__;
  END IF;
  RETURN NEW;
END $f$;

CREATE OR REPLACE FUNCTION ff_orders_after_ins() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
  INSERT INTO status_log(order_id,kind,status,at) VALUES(NEW.id,'order','placed',COALESCE(NEW.created_at,__TSSP__));
  INSERT INTO status_log(order_id,kind,status,at) VALUES(NEW.id,'kitchen',NEW.kitchen_status,COALESCE(NEW.created_at,__TSSP__));
  INSERT INTO status_log(order_id,kind,status,at) VALUES(NEW.id,'dispatch',NEW.dispatch_status,COALESCE(NEW.created_at,__TSSP__));
  RETURN NULL;
END $f$;

CREATE OR REPLACE FUNCTION ff_orders_after_upd() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
  IF OLD.kitchen_status <> NEW.kitchen_status THEN
    INSERT INTO status_log(order_id,kind,status,at) VALUES(NEW.id,'kitchen',NEW.kitchen_status,__TSSP__);
  END IF;
  IF OLD.dispatch_status <> NEW.dispatch_status THEN
    INSERT INTO status_log(order_id,kind,status,at) VALUES(NEW.id,'dispatch',NEW.dispatch_status,__TSSP__);
  END IF;
  IF OLD.payment_status <> NEW.payment_status THEN
    INSERT INTO status_log(order_id,kind,status,at) VALUES(NEW.id,'payment',NEW.payment_status,__TSSP__);
  END IF;
  RETURN NULL;
END $f$;

DROP TRIGGER IF EXISTS t1_orders_ins ON orders;
DROP TRIGGER IF EXISTS t2_orders_kitchen ON orders;
DROP TRIGGER IF EXISTS t3_orders_dispatch ON orders;
DROP TRIGGER IF EXISTS t4_orders_paged ON orders;
DROP TRIGGER IF EXISTS t8_orders_log_ins ON orders;
DROP TRIGGER IF EXISTS t9_orders_log_upd ON orders;
CREATE TRIGGER t1_orders_ins BEFORE INSERT ON orders FOR EACH ROW EXECUTE FUNCTION ff_orders_before_ins();
CREATE TRIGGER t2_orders_kitchen BEFORE UPDATE OF kitchen_status ON orders FOR EACH ROW EXECUTE FUNCTION ff_orders_before_upd('kitchen');
CREATE TRIGGER t3_orders_dispatch BEFORE UPDATE OF dispatch_status ON orders FOR EACH ROW EXECUTE FUNCTION ff_orders_before_upd('dispatch');
CREATE TRIGGER t4_orders_paged BEFORE UPDATE OF driver_id, paged_at ON orders FOR EACH ROW EXECUTE FUNCTION ff_orders_before_upd('paged');
CREATE TRIGGER t8_orders_log_ins AFTER INSERT ON orders FOR EACH ROW EXECUTE FUNCTION ff_orders_after_ins();
CREATE TRIGGER t9_orders_log_upd AFTER UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION ff_orders_after_upd();
"""

_DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS t1_orders_ins ON orders;
DROP TRIGGER IF EXISTS t2_orders_kitchen ON orders;
DROP TRIGGER IF EXISTS t3_orders_dispatch ON orders;
DROP TRIGGER IF EXISTS t4_orders_paged ON orders;
DROP TRIGGER IF EXISTS t8_orders_log_ins ON orders;
DROP TRIGGER IF EXISTS t9_orders_log_upd ON orders;
"""


def install_triggers(con):
    """Postgres versions of the SQLite triggers app.init_db() builds."""
    if not PG:
        return
    con.raw(PG_TRIGGERS.replace("__TSSP__", _TS_SP).replace("__TS__", _TS))


# ------------------------------------------------------------------ one-time copy from SQLite

def copy_from_sqlite_once(con, sqlite_path, log=print):
    """First start on Postgres: copy everything from the old SQLite file, once.
    Skipped when there is no SQLite file, it was already copied, or Postgres already has orders."""
    if not PG:
        return False
    if con.raw("SELECT 1 FROM ff_meta WHERE key='sqlite_copied'"):
        return False
    if not sqlite_path or not os.path.exists(sqlite_path) or os.path.getsize(sqlite_path) == 0:
        con.raw("INSERT INTO ff_meta(key,value) VALUES('sqlite_copied','no sqlite file') ON CONFLICT DO NOTHING")
        return False
    try:
        has = con.raw("SELECT COUNT(*) FROM orders")[0][0]
    except Exception:
        has = 0
    if has and not os.environ.get("FORCE_SQLITE_COPY"):
        con.raw("INSERT INTO ff_meta(key,value) VALUES('sqlite_copied','postgres already had orders') ON CONFLICT DO NOTHING")
        return False
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
    con.raw(_DROP_TRIGGERS)
    total = {}
    raw = con._c
    for t in tables:
        pg_types = dict(con.raw("""SELECT column_name, data_type FROM information_schema.columns
                                   WHERE table_schema=current_schema() AND table_name=%s""", (t.lower(),)))
        if not pg_types:
            log("sqlite copy: no table %s in Postgres, skipped" % t)
            continue
        scols = [r[1] for r in src.execute("PRAGMA table_info(%s)" % t).fetchall()]
        cols = [c for c in scols if c.lower() in pg_types]
        if not cols:
            continue
        conv = [_converter(pg_types[c.lower()]) for c in cols]
        raw.execute("DELETE FROM " + t)
        q = "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT DO NOTHING" % (
            t, ",".join(c.lower() for c in cols), ",".join(["%s"] * len(cols)))
        cur = src.execute("SELECT %s FROM %s" % (",".join('"%s"' % c for c in cols), t))
        n = 0
        with raw.cursor() as pc:
            while True:
                batch = cur.fetchmany(2000)
                if not batch:
                    break
                pc.executemany(q, [tuple(f(v) for f, v in zip(conv, r)) for r in batch])
                n += len(batch)
        total[t] = n
        if "id" in pg_types:
            raw.execute("""SELECT setval(pg_get_serial_sequence(%s,'id'), COALESCE((SELECT MAX(id) FROM """ + t +
                        """),0)+1, false) WHERE pg_get_serial_sequence(%s,'id') IS NOT NULL""", (t, t))
    src.close()
    con.raw("INSERT INTO ff_meta(key,value) VALUES('sqlite_copied',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (", ".join("%s %d" % kv for kv in sorted(total.items())),))
    log("Copied the SQLite database into Postgres: " + ", ".join("%s %d" % kv for kv in sorted(total.items())))
    return True


def _converter(pg_type):
    if pg_type in ("bigint", "integer", "smallint"):
        def f(v):
            if v is None or isinstance(v, int):
                return v
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None
        return f
    if pg_type in ("double precision", "real", "numeric"):
        def f(v):
            if v is None or isinstance(v, float):
                return v
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        return f
    if pg_type == "bytea":
        return lambda v: v if v is None or isinstance(v, bytes) else str(v).encode()
    def f(v):
        if v is None or isinstance(v, str):
            return v.replace("\x00", "") if isinstance(v, str) else v
        if isinstance(v, bytes):
            try:
                return v.decode()
            except UnicodeDecodeError:
                return v.decode("latin-1")
        return str(v)
    return f
