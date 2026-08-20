import os
import psycopg2
from psycopg2.extras import RealDictCursor

# Password ab environment variable se aayega (Render settings se)
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_connection():
    # Simple, direct connection (No pooling trap!)
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    
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
    conn.commit()
    conn.close()
    print("[DATABASE] PostgreSQL Cloud Database Connected Successfully! (Direct Mode)")