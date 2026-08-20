from dotenv import load_dotenv
load_dotenv()
import os
import hmac
import uuid
import time
import threading
from functools import wraps
from datetime import datetime
import psycopg2
from flask import (
    Flask, render_template, request, jsonify, redirect, url_for, flash,
    session, Response, abort
)
import database
import tts_engine
from scheduler import AnnouncementScheduler

app = Flask(__name__)
app.secret_key = os.environ.get('KGIC_SECRET_KEY', 'kgic-pa-system-secret-key')

COLLEGE_NAME = "Kanchan Girls Inter College"
COLLEGE_SHORT = "KGIC"

# --- Aug 2026 fix: authentication -------------------------------------------
# Previously the dashboard and every /api/* route were completely open to
# anyone with the link — no login, no password. Anyone could view, delete,
# or edit schedules, or trigger a live broadcast on the school speaker.
# ADMIN_USERNAME / ADMIN_PASSWORD should be set as environment variables in
# your hosting dashboard (Render/Railway "Environment" tab). The defaults
# below are ONLY a fallback for local testing — change them before deploying.
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'kgic-change-this-password')

pending_audio_queue = []
queue_lock = threading.Lock()

# Ephemeral, in-memory cache for one-off "Instant Manual Broadcast" audio.
# These clips are meant to be played once and don't need to survive a
# server restart the way scheduled announcements do, so they don't need a
# database row — just enough lifetime to be polled and played by the
# Browser Engine.
manual_audio_cache = {}  # key -> (bytes, created_at)
MANUAL_AUDIO_TTL_SECONDS = 900  # 15 minutes


def _prune_manual_audio_cache():
    cutoff = time.time() - MANUAL_AUDIO_TTL_SECONDS
    stale = [k for k, (_, created_at) in manual_audio_cache.items() if created_at < cutoff]
    for k in stale:
        manual_audio_cache.pop(k, None)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/') or request.path.startswith('/audio/') or request.path.startswith('/manual_audio/'):
                return jsonify({'error': 'Not authenticated. Please log in again.'}), 401
            return redirect(url_for('login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


database.init_db()
scheduler = AnnouncementScheduler()
scheduler.start()


# --- Auth routes -------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        # hmac.compare_digest avoids leaking timing information about how
        # much of the password matched.
        valid = hmac.compare_digest(username, ADMIN_USERNAME) & hmac.compare_digest(password, ADMIN_PASSWORD)
        if valid:
            session['logged_in'] = True
            session.permanent = True
            next_url = request.args.get('next') or url_for('dashboard')
            return redirect(next_url)
        flash('Galat username ya password.', 'error')
    return render_template('login.html', college_name=COLLEGE_NAME, college_short=COLLEGE_SHORT)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --- Health check for uptime pings ------------------------------------------
# Render's free tier suspends the service after ~15 minutes without an
# incoming HTTP request. Point a free external monitor (UptimeRobot,
# cron-job.org, etc.) at this URL every 5-10 minutes to keep the service
# (and therefore the background scheduler) awake. Deliberately does no DB
# work so it stays fast and cheap to call.
@app.route('/healthz')
def healthz():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})


@app.route('/')
@login_required
def dashboard():
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, title, text_en, text_hi, language, announcement_type, schedule_time, schedule_date, schedule_day, repeat_count, is_active, last_triggered, (audio_en_data IS NOT NULL) AS has_en_audio, (audio_hi_data IS NOT NULL) AS has_hi_audio FROM schedules ORDER BY is_active DESC, schedule_time ASC")
    schedules = cur.fetchall()
    cur.execute("SELECT * FROM logs ORDER BY triggered_at DESC LIMIT 30")
    logs = cur.fetchall()
    conn.close()
    return render_template('index.html', college_name=COLLEGE_NAME, college_short=COLLEGE_SHORT, schedules=schedules, logs=logs, today=datetime.now().strftime('%Y-%m-%d'))


@app.route('/api/schedules', methods=['POST'])
@login_required
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

    # Aug 2026 fix: audio bytes go straight into Postgres (BYTEA), never to
    # local disk, so they survive Render's ephemeral filesystem resets.
    audio_en_bytes, audio_hi_bytes = None, None
    if language in ('en', 'both') and text_en:
        audio_en_bytes = tts_engine.generate_audio_bytes(text_en, 'en')
    if language in ('hi', 'both') and text_hi:
        audio_hi_bytes = tts_engine.generate_audio_bytes(text_hi, 'hi')

    if (language in ('en', 'both') and not audio_en_bytes) or (language in ('hi', 'both') and not audio_hi_bytes):
        flash(f'Audio generation failed for one or more languages. Check internet connection.', 'error')

    cur.execute(
        "UPDATE schedules SET audio_en_data = %s, audio_hi_data = %s WHERE id = %s",
        (psycopg2.Binary(audio_en_bytes) if audio_en_bytes else None,
         psycopg2.Binary(audio_hi_bytes) if audio_hi_bytes else None,
         schedule_id)
    )
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))


@app.route('/api/schedules/<int:schedule_id>/toggle', methods=['POST'])
@login_required
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
@login_required
def delete_schedule(schedule_id):
    conn = database.get_connection()
    cur = conn.cursor()
    # Audio lives in the same row (BYTEA columns), so deleting the row is
    # enough — no separate file cleanup needed anymore.
    cur.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/schedules/<int:schedule_id>/test', methods=['POST'])
@login_required
def test_schedule(schedule_id):
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, (audio_en_data IS NOT NULL) AS has_en, (audio_hi_data IS NOT NULL) AS has_hi FROM schedules WHERE id = %s", (schedule_id,))
    row = cur.fetchone()
    conn.close()
    if not row: return jsonify({'error': 'Schedule not found'}), 404

    valid_urls = []
    if row['has_en']: valid_urls.append(f"/audio/{schedule_id}/en")
    if row['has_hi']: valid_urls.append(f"/audio/{schedule_id}/hi")

    if not valid_urls: return jsonify({'error': 'Audio files not found. Try editing to regenerate.'}), 404

    with queue_lock:
        pending_audio_queue.append({'task_id': f"test_{schedule_id}_{int(time.time())}", 'files': valid_urls, 'repeat_count': 1, 'log_data': None})
    return jsonify({'success': True, 'message': 'Test queued for the frontend engine.'})


@app.route('/api/speak_now', methods=['POST'])
@login_required
def speak_now():
    f = request.form
    language = f.get('language')
    text_en = f.get('text_en', '').strip()
    text_hi = f.get('text_hi', '').strip()

    try: repeat_count = int(f.get('repeat_count', 1))
    except (TypeError, ValueError): repeat_count = 1

    if language not in ('en', 'hi', 'both'): return jsonify({'error': 'Invalid language selection.'}), 400

    unique = uuid.uuid4().hex[:8]
    audio_urls = []
    now = time.time()

    if language in ('en', 'both') and text_en:
        b = tts_engine.generate_audio_bytes(text_en, 'en')
        if b:
            key = f"manual_{unique}_en"
            manual_audio_cache[key] = (b, now)
            audio_urls.append(f"/manual_audio/{key}")
    if language in ('hi', 'both') and text_hi:
        b = tts_engine.generate_audio_bytes(text_hi, 'hi')
        if b:
            key = f"manual_{unique}_hi"
            manual_audio_cache[key] = (b, now)
            audio_urls.append(f"/manual_audio/{key}")

    _prune_manual_audio_cache()

    if not audio_urls: return jsonify({'error': 'Could not generate audio. Check internet connection.'}), 500

    with queue_lock:
        pending_audio_queue.append({'task_id': f"manual_{unique}", 'files': audio_urls, 'repeat_count': repeat_count, 'log_data': {'schedule_id': None, 'title': 'Manual Emergency Broadcast', 'language': language, 'trigger_type': 'manual'}})

    return jsonify({'success': True, 'message': 'Broadcasting queued.'})


# --- Audio serving (DB-backed, survives disk wipes) -------------------------
@app.route('/audio/<int:schedule_id>/<lang>')
@login_required
def serve_schedule_audio(schedule_id, lang):
    if lang not in ('en', 'hi'):
        abort(404)
    col = 'audio_en_data' if lang == 'en' else 'audio_hi_data'
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT {col} AS data FROM schedules WHERE id = %s", (schedule_id,))
    row = cur.fetchone()
    conn.close()
    if not row or row['data'] is None:
        abort(404)
    return Response(bytes(row['data']), mimetype='audio/mpeg')


@app.route('/manual_audio/<key>')
@login_required
def serve_manual_audio(key):
    entry = manual_audio_cache.get(key)
    if not entry:
        abort(404)
    data, _ = entry
    return Response(data, mimetype='audio/mpeg')


@app.route('/api/get_pending_audio')
@login_required
def get_pending_audio():
    with queue_lock:
        if pending_audio_queue:
            task = pending_audio_queue.pop(0)
            return jsonify({'has_audio': True, **task})
        return jsonify({'has_audio': False})


@app.route('/api/mark_audio_done', methods=['POST'])
@login_required
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
@login_required
def get_logs():
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM logs ORDER BY triggered_at DESC LIMIT 30")
    logs = cur.fetchall()
    conn.close()
    return jsonify([dict(row) for row in logs])


@app.route('/api/logs/clear', methods=['POST'])
@login_required
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

        needs_en = lang in ('en', 'both') and row['audio_en_data'] is None
        needs_hi = lang in ('hi', 'both') and row['audio_hi_data'] is None

        if needs_en or needs_hi:
            audio_en_bytes = tts_engine.generate_audio_bytes(row['text_en'], 'en') if needs_en else None
            audio_hi_bytes = tts_engine.generate_audio_bytes(row['text_hi'], 'hi') if needs_hi else None

            update_cur = conn.cursor()
            if needs_en and needs_hi:
                update_cur.execute("UPDATE schedules SET audio_en_data = %s, audio_hi_data = %s WHERE id = %s",
                    (psycopg2.Binary(audio_en_bytes) if audio_en_bytes else None,
                     psycopg2.Binary(audio_hi_bytes) if audio_hi_bytes else None, schedule_id))
            elif needs_en:
                update_cur.execute("UPDATE schedules SET audio_en_data = %s WHERE id = %s",
                    (psycopg2.Binary(audio_en_bytes) if audio_en_bytes else None, schedule_id))
            elif needs_hi:
                update_cur.execute("UPDATE schedules SET audio_hi_data = %s WHERE id = %s",
                    (psycopg2.Binary(audio_hi_bytes) if audio_hi_bytes else None, schedule_id))
            conn.commit()
    conn.close()
    print("[SYSTEM] Startup audio verification complete.")


threading.Thread(target=generate_missing_audio_on_startup, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)