import os
import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

# YAHAN APNA COPIED URL PASTE KAREIN (Quotes "" ke andar)
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- Connection pool -------------------------------------------------------
# Free-tier Postgres (e.g. Neon's free plan) caps concurrent connections and
# bills "compute time" for every second the DB is awake. The old code called
# psycopg2.connect()/conn.close() on every scheduler tick (every 10s) and on
# every HTTP request, which meant a fresh TLS handshake each time, and the
# database effectively never got to idle/auto-suspend. A small connection
# pool is reused across requests so at most a handful of real connections
# stay open, and everything else borrows/returns instead of opening new
# sockets.
#
# DB_POOL_MIN / DB_POOL_MAX let you tune this per deployment via env vars.
# Defaults are deliberately small since free-tier plans often cap total
# concurrent connections in the low tens.
_POOL_MIN = int(os.environ.get("DB_POOL_MIN", 1))
_POOL_MAX = int(os.environ.get("DB_POOL_MAX", 5))

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL environment variable is not set.")
        _pool = psycopg2.pool.ThreadedConnectionPool(
            _POOL_MIN,
            _POOL_MAX,
            dsn=DATABASE_URL,
            cursor_factory=RealDictCursor,
        )
    return _pool


class PooledConnection:
    """
    Thin proxy around a real psycopg2 connection.

    The rest of the codebase (app.py, scheduler.py) already calls
    conn.close() everywhere it's done with a connection. Rather than
    rewriting every call site, this proxy makes .close() return the
    connection to the pool instead of tearing down the socket, so all
    existing code gets pooling "for free".
    """
    __slots__ = ("_conn", "_pool", "_released")

    def __init__(self, conn, pool_ref):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", pool_ref)
        object.__setattr__(self, "_released", False)

    def close(self):
        if self._released:
            return
        object.__setattr__(self, "_released", True)
        try:
            # Roll back any uncommitted work before the connection is
            # recycled, so the next borrower always starts clean.
            if self._conn.closed == 0:
                self._conn.rollback()
            self._pool.putconn(self._conn)
        except Exception:
            # If the connection is broken, drop it for good rather than
            # returning a poisoned connection to the pool.
            try:
                self._pool.putconn(self._conn, close=True)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def get_connection():
    """Borrow a connection from the pool. Always call .close() (or use
    `with database.get_connection() as conn:`) when done to release it
    back to the pool."""
    p = _get_pool()
    raw = p.getconn()
    raw.autocommit = False
    return PooledConnection(raw, p)


def close_pool():
    """Optional: call on graceful shutdown to close every pooled socket."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # PostgreSQL syntax: AUTOINCREMENT ki jagah SERIAL use hota hai
    cur.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            id                SERIAL PRIMARY KEY,
            title             TEXT NOT NULL,
            text_en           TEXT,
            text_hi           TEXT,
            language          TEXT NOT NULL CHECK(language IN ('en', 'hi', 'both')),
            announcement_type TEXT NOT NULL CHECK(announcement_type IN ('daily', 'onetime', 'weekly')),
            schedule_time     TEXT NOT NULL,
            schedule_date     TEXT,
            schedule_day      TEXT,
            repeat_count      INTEGER NOT NULL DEFAULT 1,
            is_active         INTEGER NOT NULL DEFAULT 1,
            last_triggered    TEXT,
            audio_en_path     TEXT,
            audio_hi_path     TEXT,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id            SERIAL PRIMARY KEY,
            schedule_id   INTEGER,
            title         TEXT,
            language      TEXT,
            trigger_type  TEXT NOT NULL CHECK(trigger_type IN ('scheduled', 'manual')),
            status        TEXT NOT NULL CHECK(status IN ('success', 'failed')),
            details       TEXT,
            triggered_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE SET NULL
        )
    ''')

    # Helpful index: every scheduler tick filters on (is_active, schedule_time).
    # Without this, Postgres does a sequential scan every 10-30s forever.
    cur.execute('''
        CREATE INDEX IF NOT EXISTS idx_schedules_active_time
        ON schedules (is_active, schedule_time)
    ''')

    # --- Aug 2026 fix: store generated audio INSIDE Postgres (BYTEA) ---------
    # Free hosts (Render/Railway free tier) use an ephemeral filesystem: any
    # file written to local disk (the old static/audio/*.mp3 approach) gets
    # silently wiped whenever the service restarts, redeploys, or wakes from
    # sleep. The database, however, is persistent. Storing the mp3 bytes
    # directly in Postgres means the audio survives restarts exactly like
    # the schedule text already does. `ADD COLUMN IF NOT EXISTS` makes this
    # migration safe to run against an existing production database.
    cur.execute('ALTER TABLE schedules ADD COLUMN IF NOT EXISTS audio_en_data BYTEA')
    cur.execute('ALTER TABLE schedules ADD COLUMN IF NOT EXISTS audio_hi_data BYTEA')

    conn.commit()
    conn.close()
    print("[DATABASE] PostgreSQL Cloud Database Connected Successfully! (pooled)")