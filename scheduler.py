import threading
import time
import os
from datetime import datetime, timedelta
import database
import tts_engine

CHECK_INTERVAL = 10

class AnnouncementScheduler:
    def __init__(self):
        self._thread = None
        self._running = False

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
        rows = conn.cursor().execute("SELECT * FROM schedules WHERE is_active = 1 AND schedule_time = %s", (future_time,))
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
                audio_en = row['audio_en_path']
                audio_hi = row['audio_hi_path']
                
                needs_update = False
                if lang in ('en', 'both') and (not audio_en or not os.path.exists(audio_en)):
                    audio_en = tts_engine.generate_audio(row['text_en'], 'en', f"sched_{schedule_id}_en.mp3")
                    needs_update = True
                if lang in ('hi', 'both') and (not audio_hi or not os.path.exists(audio_hi)):
                    audio_hi = tts_engine.generate_audio(row['text_hi'], 'hi', f"sched_{schedule_id}_hi.mp3")
                    needs_update = True
                    
                if needs_update:
                    update_cur = conn.cursor()
                    update_cur.execute("UPDATE schedules SET audio_en_path = %s, audio_hi_path = %s WHERE id = %s", (audio_en, audio_hi, schedule_id))
                    conn.commit()
                    print(f"[PRE-GEN] Advance audio ready for schedule ID {schedule_id}")
        conn.close()

    def _check_schedules(self):
        now = datetime.now()
        current_time = now.strftime('%H:%M')
        current_date = now.strftime('%Y-%m-%d')
        current_day = now.strftime('%A')
        minute_key = f"{current_date} {current_time}"

        conn = database.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM schedules WHERE is_active = 1 AND schedule_time = %s", (current_time,))

        for row in cur.fetchall():
            if row['last_triggered'] == minute_key: continue 
                
            if row['announcement_type'] == 'daily':
                self._trigger(conn, row, minute_key, deactivate=False)
            elif row['announcement_type'] == 'onetime' and row['schedule_date'] == current_date:
                self._trigger(conn, row, minute_key, deactivate=True)
            elif row['announcement_type'] == 'weekly' and row['schedule_day'] == current_day:
                self._trigger(conn, row, minute_key, deactivate=False)

        conn.close()

    def _trigger(self, conn, row, minute_key, deactivate):
        cur = conn.cursor()
        cur.execute("UPDATE schedules SET last_triggered = %s WHERE id = %s", (minute_key, row['id']))
        if deactivate:
            cur.execute("UPDATE schedules SET is_active = 0 WHERE id = %s", (row['id'],))
        conn.commit()

        schedule_id, title, language, repeat_count = row['id'], row['title'], row['language'], row['repeat_count'] or 1
        
        audio_urls = []
        if row['audio_en_path'] and os.path.exists(row['audio_en_path']):
            audio_urls.append(f"/static/audio/{os.path.basename(row['audio_en_path'])}")
        if row['audio_hi_path'] and os.path.exists(row['audio_hi_path']):
            audio_urls.append(f"/static/audio/{os.path.basename(row['audio_hi_path'])}")

        status = 'success'
        details = 'Queued to Browser Engine'

        if not audio_urls:
            status = 'failed'
            details = 'Audio files missing'
        else:
            from app import pending_audio_queue, queue_lock
            with queue_lock:
                pending_audio_queue.append({
                    'task_id': f"sched_{schedule_id}_{int(time.time())}",
                    'files': audio_urls,
                    'repeat_count': repeat_count,
                    'log_data': {'schedule_id': schedule_id, 'title': title, 'language': language, 'trigger_type': 'scheduled'}
                })

        log_conn = database.get_connection()
        log_cur = log_conn.cursor()
        log_cur.execute("INSERT INTO logs (schedule_id, title, language, trigger_type, status, details) VALUES (%s, %s, %s, 'scheduled', %s, %s)",
                         (schedule_id, title, language, status, details))
        log_conn.commit()
        log_conn.close()