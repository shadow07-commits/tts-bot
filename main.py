import os
import telebot
from flask import Flask
import threading
import queue
import sqlite3
import time
import io
import re
from langdetect import detect
from PyPDF2 import PdfReader
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from pydub import AudioSegment
import riva.client

# ================= Configuration =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
NVIDIA_API_KEY = os.getenv("API_TOKEN", "YOUR_NVIDIA_API_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "123456789")) # Replace with your Telegram ID
PORT = int(os.getenv("PORT", 5000))

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
tts_queue = queue.Queue()

# ================= Database Setup =================
def init_db():
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    # Users Table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, is_premium INTEGER, premium_expiry TEXT,
        voice TEXT, lang TEXT, speed REAL, pitch TEXT, gender TEXT, output_format TEXT,
        daily_usage INTEGER, total_usage INTEGER, refers INTEGER
    )''')
    # Admins Table
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)''')
    conn.commit()
    conn.close()

init_db()

# Helper DB Functions
def get_user(user_id):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = c.fetchone()
    if not user:
        c.execute("INSERT INTO users VALUES (?, 0, NULL, 'Magpie-Multilingual.EN-US.Aria', 'auto', 1.0, 'normal', 'female', 'audio', 0, 0, 0)", (user_id,))
        conn.commit()
        user = (user_id, 0, None, 'Magpie-Multilingual.EN-US.Aria', 'auto', 1.0, 'normal', 'female', 'audio', 0, 0, 0)
    conn.close()
    return user

def is_admin(user_id):
    if user_id == OWNER_ID: return True
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT * FROM admins WHERE user_id=?", (user_id,))
    admin = c.fetchone()
    conn.close()
    return bool(admin)

# ================= NVIDIA Riva TTS Engine =================
def generate_tts_riva(text, voice_name, lang_code):
    try:
        # Auth with NVIDIA Cloud Functions (NVCF)
        auth = riva.client.Auth(
            ssl_cert=None, use_ssl=True,
            uri="grpc.nvcf.nvidia.com:443",
            metadata_args=[
                ("function-id", "877104f7-e885-42b9-8de8-f6e4c6303969"),
                ("authorization", f"Bearer {NVIDIA_API_KEY}")
            ]
        )
        tts_service = riva.client.SpeechSynthesisService(auth)
        req = riva.client.SynthesizeSpeechRequest()
        req.text = text
        req.language_code = lang_code
        req.voice_name = voice_name
        
        resp = tts_service.synthesize(req)
        return resp.audio
    except Exception as e:
        print(f"Riva Error: {e}")
        return None

# ================= File Parsing =================
def extract_text_from_txt(file_bytes):
    return file_bytes.decode('utf-8')

def extract_text_from_pdf(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() + "\n"
    return text

def extract_text_from_epub(file_bytes):
    with open("temp.epub", "wb") as f: f.write(file_bytes)
    book = epub.read_epub("temp.epub")
    text = ""
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_body_content(), 'html.parser')
        text += soup.get_text() + "\n"
    os.remove("temp.epub")
    return text

# ================= Queue Worker (Chunks & Merge) =================
def tts_worker():
    while True:
        task = tts_queue.get()
        message, text, user_settings = task
        user_id = message.chat.id
        msg_id = bot.send_message(user_id, "⏳ Process start ho raha hai... (0%)").message_id
        
        try:
            # Auto Lang Detection
            lang = user_settings[4]
            if lang == 'auto':
                detected = detect(text)
                lang_code = "hi-IN" if detected == 'hi' else "en-US"
            else:
                lang_code = lang

            voice = user_settings[3]
            output_format = user_settings[8]

            # Chunking logic (NVIDIA API has text limits)
            chunks = [text[i:i+500] for i in range(0, len(text), 500)]
            total_chunks = len(chunks)
            merged_audio = AudioSegment.empty()

            for idx, chunk in enumerate(chunks):
                audio_bytes = generate_tts_riva(chunk, voice, lang_code)
                if audio_bytes:
                    chunk_audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format="wav")
                    merged_audio += chunk_audio
                
                # Update Progress Bar
                progress = int(((idx + 1) / total_chunks) * 100)
                if progress % 25 == 0 or progress == 100:
                    bot.edit_message_text(f"⏳ Generating Audio... ({progress}%) 📊", user_id, msg_id)

            # Export Final Audio
            output_io = io.BytesIO()
            export_format = "mp3" if output_format == "audio" else "ogg"
            merged_audio.export(output_io, format=export_format)
            output_io.seek(0)
            output_io.name = f"output.{export_format}"

            bot.edit_message_text("✅ Audio ready! Uploading...", user_id, msg_id)
            bot.send_chat_action(user_id, 'upload_voice' if output_format == "voice" else 'upload_document')

            if output_format == "voice":
                bot.send_voice(user_id, output_io, caption="Generated by @YourBotName")
            else:
                bot.send_audio(user_id, output_io, caption="Generated by @YourBotName")
                
            bot.delete_message(user_id, msg_id)
            
        except Exception as e:
            bot.edit_message_text(f"❌ Error aagaya: {str(e)}", user_id, msg_id)
        
        tts_queue.task_done()

# Start Background Worker
threading.Thread(target=tts_worker, daemon=True).start()

# ================= USER COMMANDS =================

@bot.message_handler(commands=['start'])
def send_welcome(message):
    get_user(message.chat.id) # Init user in DB
    welcome_text = (
        "👋 Welcome to the Ultimate TTS Bot!\n\n"
        "Send me any text, TXT, PDF, or EPUB file and I will convert it into high-quality Audio.\n\n"
        "⚡ Features: Hindi/English Support, Fast Streaming, Bookmarks & more.\n"
        "Use /help to see all commands."
    )
    bot.send_message(message.chat.id, welcome_text)

@bot.message_handler(commands=['help'])
def help_cmd(message):
    help_text = (
        "📋 **Command List**\n\n"
        "👤 **User Commands:**\n"
        "/start - Start Bot\n"
        "/myplan - Check Limits\n"
        "/voice - Select Voice\n"
        "/lang - Set Language\n"
        "/output - Audio vs Voice Note\n"
        "/settings - My Settings\n\n"
        "💎 **Premium Features:**\n"
        "/pitch - Change Pitch\n"
        "/batch - Multiple Files\n"
    )
    if is_admin(message.chat.id):
        help_text += "\n👑 **Admin Commands:**\n/ban, /broadcast, /status, /addpremium"
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['voice'])
def select_voice(message):
    user = get_user(message.chat.id)
    # Using Inline Keyboard for Voices
    markup = telebot.types.InlineKeyboardMarkup()
    v1 = telebot.types.InlineKeyboardButton("English Female (Aria)", callback_data="voice_Magpie-Multilingual.EN-US.Aria")
    v2 = telebot.types.InlineKeyboardButton("Hindi Male", callback_data="voice_hi-IN-male")
    markup.add(v1)
    markup.add(v2)
    bot.send_message(message.chat.id, "🎚 Select your preferred voice:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('voice_'))
def voice_callback(call):
    new_voice = call.data.split('_')[1]
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("UPDATE users SET voice=? WHERE user_id=?", (new_voice, call.message.chat.id))
    conn.commit()
    conn.close()
    bot.answer_callback_query(call.id, f"Voice updated to {new_voice}!")
    bot.edit_message_text(f"✅ Voice set to: {new_voice}", call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=['output'])
def set_output(message):
    user = get_user(message.chat.id)
    is_premium = user[1]
    
    if not is_premium:
        bot.send_message(message.chat.id, "🔒 Output change (Voice Note) is a Premium feature. Upgrade to use!")
        return

    markup = telebot.types.InlineKeyboardMarkup()
    btn1 = telebot.types.InlineKeyboardButton("MP3 Audio", callback_data="out_audio")
    btn2 = telebot.types.InlineKeyboardButton("Telegram Voice Note", callback_data="out_voice")
    markup.add(btn1, btn2)
    bot.send_message(message.chat.id, "Choose Output Format:", reply_markup=markup)

# ================= OWNER / ADMIN COMMANDS =================

@bot.message_handler(commands=['addpremium'])
def add_premium(message):
    if message.chat.id != OWNER_ID:
        return bot.reply_to(message, "❌ Only Owner can use this.")
    try:
        args = message.text.split()
        target_id = int(args[1])
        days = int(args[2])
        # Update DB logic here (Simplified for snippet)
        conn = sqlite3.connect("bot_database.db")
        conn.execute("UPDATE users SET is_premium=1 WHERE user_id=?", (target_id,))
        conn.commit()
        bot.reply_to(message, f"✅ Added premium to {target_id} for {days} days.")
        bot.send_message(target_id, "🎉 Congratulations! You are now a Premium user!")
    except:
        bot.reply_to(message, "Usage: /addpremium <user_id> <days>")

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if not is_admin(message.chat.id): return
    msg = message.text.replace("/broadcast ", "")
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    bot.reply_to(message, f"Sending to {len(users)} users...")
    success = 0
    for u in users:
        try:
            bot.send_message(u[0], msg)
            success += 1
        except: pass
    bot.reply_to(message, f"✅ Broadcast Complete! Sent to {success} users.")

# ================= MESSAGE & FILE HANDLERS =================

@bot.message_handler(content_types=['text'])
def handle_text(message):
    if message.text.startswith('/'): return
    user_settings = get_user(message.chat.id)
    
    # Add to Queue
    tts_queue.put((message, message.text, user_settings))
    bot.reply_to(message, "🔁 Added to Queue. Position: " + str(tts_queue.qsize()))

@bot.message_handler(content_types=['document'])
def handle_docs(message):
    user_settings = get_user(message.chat.id)
    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    
    ext = message.document.file_name.split('.')[-1].lower()
    text = ""
    
    bot.reply_to(message, "📥 Downloading and extracting text...")
    try:
        if ext == 'txt':
            text = extract_text_from_txt(downloaded_file)
        elif ext == 'pdf':
            text = extract_text_from_pdf(downloaded_file)
        elif ext == 'epub':
            text = extract_text_from_epub(downloaded_file)
        else:
            return bot.reply_to(message, "❌ Unsupported file format. Send TXT, PDF, or EPUB.")
            
        if len(text.strip()) == 0:
            return bot.reply_to(message, "❌ File seems empty or could not read text.")
            
        tts_queue.put((message, text, user_settings))
        bot.reply_to(message, f"🔁 Text Extracted ({len(text)} chars). Added to Queue. Position: {tts_queue.qsize()}")
        
    except Exception as e:
        bot.reply_to(message, f"❌ Error parsing file: {e}")

# ================= FLASK WEB SERVER FOR RENDER =================
@app.route('/')
def home():
    return "TTS Bot is running successfully on Render!"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    # Start Web Server in parallel thread
    t = threading.Thread(target=run_flask)
    t.start()
    
    print("🤖 Bot Started!")
    # Start Telegram Bot Polling
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
