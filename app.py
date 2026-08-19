import os
import uuid
import time
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import database
import tts_engine
from scheduler import AnnouncementScheduler

app = Flask(__name__)
app.secret_key = os.environ.get('KGIC_SECRET_KEY', 'kgic-pa-system-secret-key')

COLLEGE_NAME = "Kanchan Girls Inter College"
COLLEGE_SHORT = "KGIC"

pending_audio_queue = []
queue_lock = threading.Lock()

database.init_db()
scheduler = AnnouncementScheduler()
scheduler.start()

@app.route('/')
def dashboard():
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM schedules ORDER BY is_active DESC, schedule_time ASC")
    schedules = cur.fetchall()
    cur.execute("SELECT * FROM logs ORDER BY triggered_at DESC LIMIT 30")
    logs = cur.fetchall()
    conn.close()
    return render_template('index.html', college_name=COLLEGE_NAME, college_short=COLLEGE_SHORT, schedules=schedules, logs=logs, today=datetime.now().strftime('%Y-%m-%d'))

@app.route('/api/schedules', methods=['POST'])
def create_schedule():
    f = request.form
    schedule_id = f.get('schedule_id', '').strip()
    title = f.get('title', '').strip()
    language = f.get('language')
    announcement_type = f.get('announcement_type')
    schedule_time = f.get('schedule_time')
    schedule_date = f.get('schedule_date') or None
    schedule_day = f.get('schedule_day', 'Monday')
    text_en = f.get('text_en', '').strip()
    text_hi = f.get('text_hi', '').strip()

    try: repeat_count = int(f.get('repeat_count', 1))
    except (TypeError, ValueError): repeat_count = 1

    errors = []
    if not title: errors.append("Title is required.")
    if language not in ('en', 'hi', 'both'): errors.append("Please choose a valid language.")
    if announcement_type not in ('daily', 'onetime', 'weekly'): errors.append("Please choose a valid announcement type.")
    if not schedule_time: errors.append("Schedule time is required.")
    if announcement_type == 'onetime' and not schedule_date: errors.append("Date is required for a one-time notice.")
    if language in ('en', 'both') and not text_en: errors.append("English text is required.")
    if language in ('hi', 'both') and not text_hi: errors.append("Hindi text is required.")

    if errors:
        flash(" | ".join(errors), 'error')
        return redirect(url_for('dashboard'))

    conn = database.get_connection()
    cur = conn.cursor()

    if schedule_id:
        cur.execute('''
            UPDATE schedules SET 
                title=%s, text_en=%s, text_hi=%s, language=%s, announcement_type=%s, 
                schedule_time=%s, schedule_date=%s, schedule_day=%s, repeat_count=%s 
            WHERE id=%s
        ''', (title, text_en, text_hi, language, announcement_type, schedule_time, schedule_date, schedule_day, repeat_count, schedule_id))
        flash(f'"{title}" updated successfully.', 'success')
    else:
        # Postgres requires RETURNING id
        cur.execute('''
            INSERT INTO schedules
                (title, text_en, text_hi, language, announcement_type, schedule_time, schedule_date, schedule_day, repeat_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        ''', (title, text_en, text_hi, language, announcement_type, schedule_time, schedule_date, schedule_day, repeat_count))
        schedule_id = cur.fetchone()['id']
        flash(f'"{title}" scheduled successfully.', 'success')

    conn.commit()
    audio_en_path, audio_hi_path = None, None
    if language in ('en', 'both') and text_en:
        audio_en_path = tts_engine.generate_audio(text_en, 'en', f"sched_{schedule_id}_en.mp3")
    if language in ('hi', 'both') and text_hi:
        audio_hi_path = tts_engine.generate_audio(text_hi, 'hi', f"sched_{schedule_id}_hi.mp3")

    if (language in ('en', 'both') and not audio_en_path) or (language in ('hi', 'both') and not audio_hi_path):
        flash(f'Audio generation failed for one or more languages. Check internet connection.', 'error')

    cur.execute("UPDATE schedules SET audio_en_path = %s, audio_hi_path = %s WHERE id = %s", (audio_en_path, audio_hi_path, schedule_id))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/api/schedules/<int:schedule_id>/toggle', methods=['POST'])
def toggle_schedule(schedule_id):
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT is_active FROM schedules WHERE id = %s", (schedule_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return jsonify({'error': 'Schedule not found'}), 404

    new_status = 0 if row['is_active'] else 1
    cur.execute("UPDATE schedules SET is_active = %s WHERE id = %s", (new_status, schedule_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'is_active': new_status})

@app.route('/api/schedules/<int:schedule_id>', methods=['DELETE'])
def delete_schedule(schedule_id):
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM schedules WHERE id = %s", (schedule_id,))
    row = cur.fetchone()
    if row:
        for p in (row['audio_en_path'], row['audio_hi_path']):
            if p and os.path.exists(p):
                try: os.remove(p)
                except OSError: pass
    cur.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

def get_audio_url(absolute_path):
    if not absolute_path: return None
    return f"/static/audio/{os.path.basename(absolute_path)}"

@app.route('/api/schedules/<int:schedule_id>/test', methods=['POST'])
def test_schedule(schedule_id):
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM schedules WHERE id = %s", (schedule_id,))
    row = cur.fetchone()
    conn.close()
    if not row: return jsonify({'error': 'Schedule not found'}), 404

    audio_urls = [get_audio_url(row['audio_en_path']), get_audio_url(row['audio_hi_path'])]
    valid_urls = [u for u in audio_urls if u]

    if not valid_urls: return jsonify({'error': 'Audio files not found. Try editing to regenerate.'}), 404

    with queue_lock:
        pending_audio_queue.append({'task_id': f"test_{schedule_id}_{int(time.time())}", 'files': valid_urls, 'repeat_count': 1, 'log_data': None})
    return jsonify({'success': True, 'message': 'Test queued for the frontend engine.'})

@app.route('/api/speak_now', methods=['POST'])
def speak_now():
    f = request.form
    language = f.get('language')
    text_en = f.get('text_en', '').strip()
    text_hi = f.get('text_hi', '').strip()

    try: repeat_count = int(f.get('repeat_count', 1))
    except (TypeError, ValueError): repeat_count = 1

    if language not in ('en', 'hi', 'both'): return jsonify({'error': 'Invalid language selection.'}), 400

    unique = uuid.uuid4().hex[:8]
    audio_paths = []

    if language in ('en', 'both') and text_en:
        p = tts_engine.generate_audio(text_en, 'en', f"manual_{unique}_en.mp3")
        if p: audio_paths.append(p)
    if language in ('hi', 'both') and text_hi:
        p = tts_engine.generate_audio(text_hi, 'hi', f"manual_{unique}_hi.mp3")
        if p: audio_paths.append(p)

    if not audio_paths: return jsonify({'error': 'Could not generate audio. Check internet connection.'}), 500

    audio_urls = [get_audio_url(p) for p in audio_paths if p]

    with queue_lock:
        pending_audio_queue.append({'task_id': f"manual_{unique}", 'files': audio_urls, 'repeat_count': repeat_count, 'log_data': {'schedule_id': None, 'title': 'Manual Emergency Broadcast', 'language': language, 'trigger_type': 'manual'}})

    return jsonify({'success': True, 'message': 'Broadcasting queued.'})

@app.route('/api/get_pending_audio')
def get_pending_audio():
    with queue_lock:
        if pending_audio_queue:
            task = pending_audio_queue.pop(0)
            return jsonify({'has_audio': True, **task})
        return jsonify({'has_audio': False})

@app.route('/api/mark_audio_done', methods=['POST'])
def mark_audio_done():
    data = request.json
    task_id = data.get('task_id')
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO logs (title, trigger_type, status, details) VALUES (%s, %s, %s, %s)", ("Audio Played Successfully", "manual", "success", f"Task ID: {task_id}"))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/logs')
def get_logs():
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM logs ORDER BY triggered_at DESC LIMIT 30")
    logs = cur.fetchall()
    conn.close()
    return jsonify([dict(row) for row in logs])

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    conn = database.get_connection()
    conn.cursor().execute("DELETE FROM logs")
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'All system logs cleared.'})

def generate_missing_audio_on_startup():
    print("[SYSTEM] Checking for missing audio files on startup...")
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM schedules WHERE is_active = 1")
    for row in cur.fetchall():
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
    conn.close()
    print("[SYSTEM] Startup audio verification complete.")

threading.Thread(target=generate_missing_audio_on_startup, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)