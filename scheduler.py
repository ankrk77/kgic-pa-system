import threading
import time
import os
import psycopg2
from datetime import datetime, timedelta
import database
import tts_engine

# Free-tier note: schedules only have minute-level precision anyway, so
# checking every 10s (6x/minute) was wasted work against the DB. 20s still
# guarantees we never miss a minute boundary, but cuts DB round-trips by
# ~2x. Override with SCHEDULER_INTERVAL_SECONDS if you want it tighter.
CHECK_INTERVAL = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", 5))


class AnnouncementScheduler:
    def __init__(self, audio_queue=None, queue_lock=None):
        self._thread = None
        self._running = False
        self.audio_queue = audio_queue
        self.queue_lock = queue_lock

    def start(self):
        if self._running: return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        print(f"[SCHEDULER] Background Cloud engine started (checking every {CHECK_INTERVAL}s).")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                self._check_schedules()
                self._pre_generate_upcoming()
            except Exception as exc:
                print(f"[SCHEDULER ERROR] {exc}")
            time.sleep(CHECK_INTERVAL)

    def _pre_generate_upcoming(self):
        now = datetime.now()
        future_time = (now + timedelta(minutes=5)).strftime('%H:%M')
        current_day = now.strftime('%A')
        current_date = now.strftime('%Y-%m-%d')

        conn = database.get_connection()
        try:
            rows = conn.cursor()
            rows.execute("SELECT * FROM schedules WHERE is_active = 1 AND schedule_time = %s", (future_time,))

            for row in rows.fetchall():
                is_valid_today = False
                if row['announcement_type'] == 'daily': is_valid_today = True
                elif row['announcement_type'] == 'onetime' and row['schedule_date'] == current_date: is_valid_today = True
                elif row['announcement_type'] == 'weekly' and row['schedule_day'] == current_day: is_valid_today = True

                if is_valid_today:
                    schedule_id = row['id']
                    lang = row['language']

                    needs_en = lang in ('en', 'both') and row['audio_en_data'] is None
                    needs_hi = lang in ('hi', 'both') and row['audio_hi_data'] is None

                    if needs_en or needs_hi:
                        audio_en_bytes = tts_engine.generate_audio_bytes(row['text_en'], 'en') if needs_en else None
                        audio_hi_bytes = tts_engine.generate_audio_bytes(row['text_hi'], 'hi') if needs_hi else None

                        update_cur = conn.cursor()
                        if needs_en and needs_hi:
                            update_cur.execute(
                                "UPDATE schedules SET audio_en_data = %s, audio_hi_data = %s WHERE id = %s",
                                (psycopg2.Binary(audio_en_bytes) if audio_en_bytes else None,
                                 psycopg2.Binary(audio_hi_bytes) if audio_hi_bytes else None, schedule_id))
                        elif needs_en:
                            update_cur.execute(
                                "UPDATE schedules SET audio_en_data = %s WHERE id = %s",
                                (psycopg2.Binary(audio_en_bytes) if audio_en_bytes else None, schedule_id))
                        elif needs_hi:
                            update_cur.execute(
                                "UPDATE schedules SET audio_hi_data = %s WHERE id = %s",
                                (psycopg2.Binary(audio_hi_bytes) if audio_hi_bytes else None, schedule_id))
                        conn.commit()
                        print(f"[PRE-GEN] Advance audio ready for schedule ID {schedule_id}")
        finally:
            conn.close()

    def _check_schedules(self):
        now = datetime.now()
        current_time = now.strftime('%H:%M')
        current_date = now.strftime('%Y-%m-%d')
        current_day = now.strftime('%A')
        minute_key = f"{current_date} {current_time}"

        print(f"\n[TESTING - SCHEDULER] Woke up at Server Time: {current_time} | Day: {current_day} | Date: {current_date}")

        conn = database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM schedules WHERE is_active = 1 AND schedule_time = %s", (current_time,))
            due_rows = cur.fetchall()
            
            print(f"[TESTING - DB] Found {len(due_rows)} active schedule(s) matching exactly {current_time}")
        finally:
            conn.close()

        for row in due_rows:
            print(f"[TESTING - TRIGGER] Processing Schedule ID: {row['id']} | Title: '{row['title']}' | Type: {row['announcement_type']}")
            
            if row['last_triggered'] == minute_key:
                print(f"[TESTING - SKIP] ID {row['id']} skipped. Already triggered in this minute ({minute_key}).")
                continue

            if row['announcement_type'] == 'daily':
                print(f"[TESTING - ACTION] Firing Daily Schedule ID {row['id']}")
                self._trigger(row, minute_key, deactivate=False)
            elif row['announcement_type'] == 'onetime' and row['schedule_date'] == current_date:
                print(f"[TESTING - ACTION] Firing Onetime Schedule ID {row['id']} and deactivating.")
                self._trigger(row, minute_key, deactivate=True)
            elif row['announcement_type'] == 'weekly' and row['schedule_day'] == current_day:
                print(f"[TESTING - ACTION] Firing Weekly Schedule ID {row['id']}")
                self._trigger(row, minute_key, deactivate=False)
            else:
                print(f"[TESTING - FAILED MATCH] ID {row['id']} matched time but failed date/day condition.")

    def _trigger(self, row, minute_key, deactivate):
        conn = database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE schedules SET last_triggered = %s WHERE id = %s", (minute_key, row['id']))
            if deactivate:
                cur.execute("UPDATE schedules SET is_active = 0 WHERE id = %s", (row['id'],))
            conn.commit()
        finally:
            conn.close()

        schedule_id, title, language, repeat_count = row['id'], row['title'], row['language'], row['repeat_count'] or 1

        audio_urls = []
        if row['audio_en_data'] is not None:
            audio_urls.append(f"/audio/{schedule_id}/en")
        if row['audio_hi_data'] is not None:
            audio_urls.append(f"/audio/{schedule_id}/hi")

        status = 'success'
        details = 'Queued to Browser Engine'

        if not audio_urls:
            status = 'failed'
            details = 'Audio files missing'
        else:
            with self.queue_lock:
                self.audio_queue.append({
                    'task_id': f"sched_{schedule_id}_{int(time.time())}",
                    'files': audio_urls,
                    'repeat_count': repeat_count,
                    'log_data': {'schedule_id': schedule_id, 'title': title, 'language': language, 'trigger_type': 'scheduled'}
                })

        log_conn = database.get_connection()
        try:
            log_cur = log_conn.cursor()
            
            # 🟢 FIX: IST Time create kiya
            now_ist = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # YAHAN CHANGE HAI: Query mein 'triggered_at' column add kiya aur aakhri mein 'now_ist' value pass ki 👇
            log_cur.execute(
                "INSERT INTO logs (schedule_id, title, language, trigger_type, status, details, triggered_at) VALUES (%s, %s, %s, 'scheduled', %s, %s, %s)",
                (schedule_id, title, language, status, details, now_ist)
            )
            log_conn.commit()
        finally:
            log_conn.close()