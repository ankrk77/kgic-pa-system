# 🔔 KGIC Cloud-Automated PA System

A fully automated, cloud-hosted Public Address (PA) and school bell scheduling system built with Python, Flask, and PostgreSQL. Engineered specifically to provide a wireless, remote-controlled broadcasting solution without tying down the administrator's primary PC.

## 🚀 Overview
Traditional PA systems require the host PC to be physically wired to the audio amplifier. This project solves that by implementing a **Producer-Consumer Polling Architecture**. 
* **The Producer (Cloud):** A Flask web dashboard where administrators securely schedule announcements. It uses Microsoft Edge Neural TTS to generate human-like voice audio and stores it as binary data (`BYTEA`) in a PostgreSQL database.
* **The Consumer (Receiver):** A repurposed, low-cost device (like an old smartphone or PC) connected to the amplifier. A frontend JS engine polls the server every 5 seconds to catch and play the queued audio precisely on time.

## 🛠️ Tech Stack
* **Backend:** Python 3.10, Flask (REST API & Routing)
* **Database:** PostgreSQL (Neon Cloud) with `psycopg2-binary`
* **Audio Engine:** Microsoft Edge Neural TTS (`edge-tts`)
* **Frontend:** HTML5, CSS3, JavaScript, Jinja2
* **Hosting/Deployment:** Render (Gunicorn WSGI)

## ✨ Key Features
- **Remote Scheduling Dashboard:** Secure login for admins to set daily, weekly, or one-time bells from any network.
- **High-Quality Bilingual TTS:** Natural sounding AI voices in Hindi, English, or a mix of both.
- **Database-Backed Audio Storage:** Overcomes ephemeral filesystem limitations of free cloud-hosting by storing generated audio directly in PostgreSQL.
- **Live Override (Speak-Now):** Instantly generate and broadcast emergency announcements outside the regular schedule.
- **Black Box System Logs:** Real-time logging of all scheduled and manual triggers accurately synchronized to Indian Standard Time (IST).
- **WakeLock API Integration:** Prevents the consumer device's browser from sleeping to ensure uninterrupted audio playback.

## 🧠 Technical Challenges Solved
1. **The Wipeout Bug:** Render's free tier wipes local `.mp3` files daily. *Solution:* Shifted to in-memory generation and saved audio bytes directly to the Database.
2. **Gunicorn Worker Timeout (500 Error):** Multiple workers clashing over database connections on startup. *Solution:* Implemented Lazy Initialization (`@app.before_request`).
3. **Ghost Import Bug:** Circular imports causing duplicate system instances. *Solution:* Transitioned to Object Passing (`queue` and `queue_lock`) in the Scheduler initialization.

## ⚙️ Local Setup & Installation

1. **Clone the repository:**
    git clone https://github.com/ankrk77/kgic-pa-system.git
    cd kgic-pa-system

2. **Create a virtual environment:**
    python -m venv venv
    source venv/bin/activate

3. **Install dependencies:**
    pip install -r requirements.txt

4. **Environment Variables (.env):**
    Create a .env file in the root directory and add:
    DATABASE_URL=your_neon_postgres_url
    SECRET_KEY=your_flask_secret_key

5. **Run the Application:**
    python app.py

## 👨‍💻 Author
**Ankit Yadav**
Software Developer | Backend Engineering
