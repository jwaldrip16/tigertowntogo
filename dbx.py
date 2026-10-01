"""
Database layer for Fleet Delivery.

SQLite only. One file on disk, opened per request, with the pragmas the app
wants. Nothing else in the app has to know what is running underneath.
"""
import os
import sqlite3

PG = False
TZ = os.environ.get("TZ", "America/Chicago")
NOW_SQL = "datetime('now','localtime')"


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def tune(con):
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=4000")


def columns(con, table):
    rows = con.execute("PRAGMA table_info(" + table + ")").fetchall()
    return [r["name"] for r in rows]


def install_triggers(con):
    """Postgres needed these. SQLite builds its own in app.init_db()."""
    return


def where():
    return "SQLite (" + os.environ.get("DB_PATH", "delivery.db") + ")"
