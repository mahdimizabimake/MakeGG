import asyncio
import base64
import os
import re
import struct
import subprocess
import psycopg
from psycopg.rows import dict_row
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SetTypingRequest, GetHistoryRequest
from telethon.tl.types import (
    SendMessageRecordAudioAction, SendMessageUploadAudioAction,
    SendMessageRecordVideoAction, SendMessageUploadVideoAction,
    Message
)
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from aiohttp import web
from pyrogram import Client as PyroClient
try:
    from pyrogram.storage import Storage
    PYROGRAM_SESSION_STRING_FORMAT = getattr(Storage, "SESSION_STRING_FORMAT", ">BI?256sQ?")
except Exception:
    PYROGRAM_SESSION_STRING_FORMAT = ">BI?256sQ?"
from pytgcalls import PyTgCalls
from pytgcalls.types import Call, MediaStream

BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
if not BOT_TOKEN or not DATABASE_URL or not API_ID or not API_HASH:
    raise Exception("BOT_TOKEN, DATABASE_URL, API_ID, API_HASH are required")

FFMPEG_PATH = "ffmpeg"
FFPROBE_PATH = "ffprobe"

API_ID_STATE, API_HASH_STATE, PHONE_STATE, CODE_STATE, PASSWORD_STATE, TARGET_CHAT_STATE = range(6)
REPLY_METHOD_STATE, REPLY_SELECT_CHAT_STATE, REPLY_SELECT_MSG_STATE, REPLY_LINK_STATE = range(6, 10)
AUTO_VIDEO_STATE = 10

# ========== Ø¯ÛŒØªØ§Ø¨ÛŒØ³ ==========
async def get_conn():
    return await psycopg.AsyncConnection.connect(DATABASE_URL)

async def init_db():
    async with await get_conn() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_data (
                user_id BIGINT PRIMARY KEY,
                api_id INTEGER,
                api_hash TEXT,
                session_string TEXT,
                target_chat_id BIGINT,
                target_chat_title TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        async with conn.cursor() as cur:
            await cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='user_data'")
            columns = [row[0] for row in await cur.fetchall()]
            if 'reply_msg_id' not in columns:
                await conn.execute("ALTER TABLE user_data ADD COLUMN reply_msg_id INTEGER DEFAULT NULL")
            if 'reply_active' not in columns:
                await conn.execute("ALTER TABLE user_data ADD COLUMN reply_active BOOLEAN DEFAULT FALSE")
            if 'reply_chat_id' not in columns:
                await conn.execute("ALTER TABLE user_data ADD COLUMN reply_chat_id BIGINT DEFAULT NULL")
            if 'auto_video_enabled' not in columns:
                await conn.execute("ALTER TABLE user_data ADD COLUMN auto_video_enabled BOOLEAN DEFAULT FALSE")
            if 'auto_video_path' not in columns:
                await conn.execute("ALTER TABLE user_data ADD COLUMN auto_video_path TEXT DEFAULT NULL")
        await conn.commit()

async def get_user_data(user_id):
    async with await get_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT * FROM user_data WHERE user_id = %s', (user_id,))
            return await cur.fetchone()

async def save_user_data(user_id, api_id=None, api_hash=None, session_string=None, target_chat_id=None, target_chat_title=None):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT 1 FROM user_data WHERE user_id = %s', (user_id,))
            exists = await cur.fetchone()
            if exists:
                await cur.execute('''
                    UPDATE user_data SET
                        api_id = COALESCE(%s, api_id),
                        api_hash = COALESCE(%s, api_hash),
                        session_string = COALESCE(%s, session_string),
                        target_chat_id = COALESCE(%s, target_chat_id),
                        target_chat_title = COALESCE(%s, target_chat_title)
                    WHERE user_id = %s
                ''', (api_id, api_hash, session_string, target_chat_id, target_chat_title, user_id))
            else:
                await cur.execute('''
                    INSERT INTO user_data (user_id, api_id, api_hash, session_string, target_chat_id, target_chat_title)
                    VALUES (%s, %s, %s, %s, %s, %s)
                ''', (user_id, api_id, api_hash, session_string, target_chat_id, target_chat_title))
            await conn.commit()

async def update_target_chat(user_id, chat_id, chat_title):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute('UPDATE user_data SET target_chat_id = %s, target_chat_title = %s WHERE user_id = %s', (chat_id, chat_title, user_id))
            await conn.commit()

async def set_reply(user_id, chat_id, msg_id, active=True):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute('UPDATE user_data SET reply_chat_id = %s, reply_msg_id = %s, reply_active = %s WHERE user_id = %s', (chat_id, msg_id, active, user_id))
            await conn.commit()

async def clear_reply(user_id):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute('UPDATE user_data SET reply_chat_id = NULL, reply_msg_id = NULL, reply_active = FALSE WHERE user_id = %s', (user_id,))
            await conn.commit()

# ========== ÙˆÛŒØ¯ÛŒÙˆ Ú©Ø§Ù„ ==========
call_clients = {}

async def enable_auto_video(user_id, video_path):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute('UPDATE user_data SET auto_video_enabled = TRUE, auto_video_path = %s WHERE user_id = %s', (video_path, user_id))
            await conn.commit()

async def disable_auto_video(user_id):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute('UPDATE user_data SET auto_video_enabled = FALSE, auto_video_path = NULL WHERE user_id = %s', (user_id,))
            await conn.commit()

async def telethon_to_pyrogram_session(session_string, api_id, api_hash):
    """
    Telethon StringSession and Pyrogram session strings are not compatible.
    PyTgCalls uses Pyrogram here, so the saved Telethon session must be
    converted to Pyrogram's 271-byte packed session format first.
    """
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        me = await client.get_me()
        if not me:
            raise Exception("Telethon session is not authorized. Please login again.")
        if not client.session.auth_key:
            raise Exception("Telethon auth key is missing. Please login again.")

        packed = struct.pack(
            PYROGRAM_SESSION_STRING_FORMAT,
            client.session.dc_id,
            int(api_id),
            False,
            client.session.auth_key.key,
            int(me.id),
            False
        )
        return base64.urlsafe_b64encode(packed).decode().rstrip("=")
    finally:
        await client.disconnect()

async def setup_pytgcalls(user_id, session_string, api_id, api_hash):
    pyro_session_string = await telethon_to_pyrogram_session(session_string, api_id, api_hash)

    pyro_client = PyroClient(
        name=f"user_{user_id}",
        api_id=int(api_id),
        api_hash=api_hash,
        session_string=pyro_session_string,
        in_memory=True
    )
    await pyro_client.start()

    call_client = PyTgCalls(pyro_client)
    await call_client.start()
    return call_client

async def get_video_duration(video_path):
    """Ú¯Ø±ÙØªÙ† Ù…Ø¯Øª Ø²Ù…Ø§Ù† ÙˆÛŒØ¯ÛŒÙˆ Ø¨Ø§ ffprobe"""
    cmd = [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        print(f"ffprobe error: {stderr.decode()}")
        return 30
    try:
        return float(stdout.decode().strip())
    except:
        return 30

async def answer_call(chat_id, call_client, video_path):
    try:
        await call_client.answer_call(chat_id)
        await call_client.play(chat_id, MediaStream(video_path))
        duration = await get_video_duration(video_path)
        await asyncio.sleep(duration)
        try:
            await call_client.leave_call(chat_id)
        except:
            pass
    except Exception as e:
        print(f"Error answering call for {chat_id}: {e}")

async def restore_auto_video_calls():
    async with await get_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT user_id, api_id, api_hash, session_string, auto_video_path FROM user_data WHERE auto_video_enabled = TRUE AND session_string IS NOT NULL')
            active_users = await cur.fetchall()
            for user in active_users:
                user_id = user['user_id']
                api_id_val = user['api_id']
                api_hash_val = user['api_hash']
                session_str = user['session_string']
                video_path = user['auto_video_path']
                if video_path and os.path.exists(video_path):
                    call_client = await setup_pytgcalls(user_id, session_str, api_id_val, api_hash_val)
                    if call_client:
                        call_clients[user_id] = call_client
                        @call_client.on_call()
                        async def on_incoming_call(call: Call, bound_user_id=user_id):
                            data = await get_user_data(bound_user_id)
                            vid_path = data.get('auto_video_path') if data else None
                            if vid_path and os.path.exists(vid_path):
                                await answer_call(call.chat_id, call_clients[bound_user_id], vid_path)

# ========== ØªØ¨Ø¯ÛŒÙ„ ÙˆÛŒØ¯ÛŒÙˆ Ø¨Ù‡ Ù…Ø±Ø¨Ø¹ Ø¨Ø§ Ø§Ø³ØªÙØ§Ø¯Ù‡ Ø§Ø² asyncio.create_subprocess_exec ==========
async def convert_to_square_ffmpeg(input_path, output_path, target_size=480):
    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_PATH, '-version',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()
    except Exception as e:
        raise Exception(f"ffmpeg not found or not executable: {e}")

    input_abs = os.path.abspath(input_path)
    output_abs = os.path.abspath(output_path)

    cmd = [
        FFMPEG_PATH, '-i', input_abs,
        '-vf', f'crop=min(iw,ih):min(iw,ih),scale={target_size}:{target_size}',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-movflags', '+faststart',
        '-y', output_abs
    ]

    print(f"Running ffmpeg command: {' '.join(cmd)}")
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error_text = stderr.decode('utf-8', errors='ignore')
        raise Exception(f"ffmpeg error (code {process.returncode}): {error_text[:500]}")

    if not os.path.exists(output_abs):
        raise Exception("Output file was not created.")

    return output_abs

# ========== ØªÙˆØ§Ø¨Ø¹ Ú©Ù…Ú©ÛŒ Telethon ==========
async def get_input_entity_safe(client, identifier):
    try:
        return await client.get_input_entity(identifier)
    except ValueError:
        await client.get_dialogs()
        return await client.get_input_entity(identifier)

async def get_entity_safe(client, identifier):
    try:
        return await client.get_entity(identifier)
    except ValueError:
        await client.get_dialogs()
        return await client.get_entity(identifier)

async def ensure_session_active(client):
    try:
        await client.get_me()
        return True
    except:
        return False

async def send_action_with_duration(client, chat, action_type, duration_sec):
    try:
        action = SendMessageRecordAudioAction() if action_type == 'voice' else SendMessageRecordVideoAction()
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < duration_sec:
            await client(SetTypingRequest(peer=chat, action=action))
            await asyncio.sleep(4.5)
        upload_action = SendMessageUploadAudioAction(progress=0) if action_type == 'voice' else SendMessageUploadVideoAction(progress=0)
        await client(SetTypingRequest(peer=chat, action=upload_action))
        await asyncio.sleep(1)
    except Exception as e:
        print(f"Action error: {e}")

async def send_as_voice_note(client, chat, file_path, duration, reply_to=None):
    await send_action_with_duration(client, chat, 'voice', duration)
    await client.send_file(chat, file_path, voice_note=True, reply_to=reply_to)

async def send_as_video_note(client, chat, file_path, duration, reply_to=None):
    await send_action_with_duration(client, chat, 'video', duration)
    await client.send_file(chat, file_path, video_note=True, force_document=False, reply_to=reply_to)

async def get_last_dialogs(user_id):
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        return None, "Ù„Ø§Ú¯ÛŒÙ† Ù†Ø´Ø¯Ù‡â€ŒØ§ÛŒØ¯"
    client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
    await client.connect()
    try:
        await client.get_me()
        dialogs = await client.get_dialogs(limit=10)
        result = [(d.id, d.title or d.name or (d.entity.first_name if d.entity else str(d.id))) for d in dialogs]
        return result, None
    except Exception as e:
        return None, f"Ø®Ø·Ø§: {str(e)}"
    finally:
        await client.disconnect()

async def get_last_messages(user_id, chat_id, limit=10):
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        return None, "Ù„Ø§Ú¯ÛŒÙ† Ù†Ø´Ø¯Ù‡â€ŒØ§ÛŒØ¯"
    client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
    await client.connect()
    try:
        entity = await get_entity_safe(client, chat_id)
        history = await client(GetHistoryRequest(
            peer=entity,
            limit=limit,
            offset_id=0,
            offset_date=None,
            add_offset=0,
            max_id=0,
            min_id=0,
            hash=0
        ))
        messages = []
        for msg in history.messages:
            if isinstance(msg, Message):
                text = msg.text or msg.message or ""
                if not text:
                    text = "ðŸ“· Ø±Ø³Ø§Ù†Ù‡" if msg.media else "Ù¾ÛŒØ§Ù… Ø®Ø§Ù„ÛŒ"
                short_text = text[:20] + "..." if len(text) > 20 else text
                messages.append((msg.id, short_text))
        return messages, None
    except Exception as e:
        return None, f"Ø®Ø·Ø§: {str(e)}"
    finally:
        await client.disconnect()

async def parse_message_link(link):
    pattern = r'https?://t\.me/(?:c/)?(\d+|[a-zA-Z][\w]+)/(\d+)'
    match = re.search(pattern, link)
    if not match:
        return None, None
    chat_part = match.group(1)
    msg_id = int(match.group(2))
    if chat_part.isdigit():
        chat_id = int(f"-100{chat_part}")
    else:
        chat_id = chat_part
    return chat_id, msg_id

# ========== Ù…Ù†ÙˆÛŒ Ø§ØµÙ„ÛŒ ==========
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = await get_user_data(user_id)
    text = "ðŸŽ› **Ù¾Ù†Ù„ Ù…Ø¯ÛŒØ±ÛŒØª**\n\n"
    if data and data['session_string']:
        text += "âœ… Ù„Ø§Ú¯ÛŒÙ† Ù‡Ø³ØªÛŒØ¯\n"
        if data['target_chat_id']:
            text += f"ðŸŽ¯ Ú†Øª Ù‡Ø¯Ù: `{data['target_chat_title'] or data['target_chat_id']}`\n"
        else:
            text += "âŒ Ú†Øª Ù‡Ø¯Ù ØªÙ†Ø¸ÛŒÙ… Ù†Ø´Ø¯Ù‡\n"
        if data.get('reply_active') and data.get('reply_msg_id'):
            text += f"ðŸ” Ø±ÛŒÙ¾Ù„ÛŒ ÙØ¹Ø§Ù„ Ø¨Ù‡ Ù¾ÛŒØ§Ù… ID `{data['reply_msg_id']}`\n"
        else:
            text += "ðŸ” Ø±ÛŒÙ¾Ù„ÛŒ: ØºÛŒØ±ÙØ¹Ø§Ù„\n"
        if data.get('auto_video_enabled') and data.get('auto_video_path'):
            text += "ðŸ“¹ ÙˆÛŒØ¯ÛŒÙˆ Ú©Ø§Ù„: **ÙØ¹Ø§Ù„**\n"
        else:
            text += "ðŸ“¹ ÙˆÛŒØ¯ÛŒÙˆ Ú©Ø§Ù„: **ØºÛŒØ±ÙØ¹Ø§Ù„**\n"
    else:
        text += "âŒ Ù„Ø§Ú¯ÛŒÙ† Ù†ÛŒØ³ØªÛŒØ¯\n"
    buttons = [
        [InlineKeyboardButton("ðŸ” Ù„Ø§Ú¯ÛŒÙ†", callback_data="login")],
        [InlineKeyboardButton("ðŸŽ¯ ØªÙ†Ø¸ÛŒÙ… Ú†Øª Ù‡Ø¯Ù", callback_data="set_target")],
        [InlineKeyboardButton("ðŸ” ØªÙ†Ø¸ÛŒÙ… Ø±ÛŒÙ¾Ù„ÛŒ", callback_data="set_reply")],
        [InlineKeyboardButton("âŒ Ù„ØºÙˆ Ø±ÛŒÙ¾Ù„ÛŒ", callback_data="clear_reply")],
        [InlineKeyboardButton("ðŸ“¹ ØªÙ†Ø¸ÛŒÙ… ÙˆÛŒØ¯ÛŒÙˆ Ú©Ø§Ù„", callback_data="set_auto_video")],
        [InlineKeyboardButton("ðŸš« Ù„ØºÙˆ ÙˆÛŒØ¯ÛŒÙˆ Ú©Ø§Ù„", callback_data="disable_auto_video")],
        [InlineKeyboardButton("ðŸ“‹ ÙˆØ¶Ø¹ÛŒØª", callback_data="status")],
        [InlineKeyboardButton("ðŸšª Ø®Ø±ÙˆØ¬", callback_data="logout")]
    ]
    markup = InlineKeyboardMarkup(buttons)
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, parse_mode='Markdown', reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=markup)

# ---------- Ù„Ø§Ú¯ÛŒÙ† ----------
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("api_id Ø±Ø§ ÙˆØ§Ø±Ø¯ Ú©Ù†ÛŒØ¯:")
    return API_ID_STATE

async def login_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['api_id'] = int(update.message.text.strip())
        await update.message.reply_text("api_hash Ø±Ø§ ÙˆØ§Ø±Ø¯ Ú©Ù†ÛŒØ¯:")
        return API_HASH_STATE
    except ValueError:
        await update.message.reply_text("api_id Ø¨Ø§ÛŒØ¯ Ø¹Ø¯Ø¯ Ø¨Ø§Ø´Ø¯. Ø¯ÙˆØ¨Ø§Ø±Ù‡:")
        return API_ID_STATE

async def login_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text("Ø´Ù…Ø§Ø±Ù‡ ØªÙ„ÙÙ† (Ø¨Ø§ Ú©Ø¯ Ú©Ø´ÙˆØ±):")
    return PHONE_STATE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    client = TelegramClient(StringSession(), context.user_data['api_id'], context.user_data['api_hash'])
    await client.connect()
    try:
        await client.send_code_request(phone)
        context.user_data['temp_client'] = client
        await update.message.reply_text("Ú©Ø¯ ØªØ£ÛŒÛŒØ¯ +1 Ø±Ø§ ÙˆØ§Ø±Ø¯ Ú©Ù†ÛŒØ¯ (Ù…Ø«Ù„Ø§Ù‹ 12345->12346):")
        return CODE_STATE
    except Exception as e:
        await update.message.reply_text(f"Ø®Ø·Ø§: {e}")
        return ConversationHandler.END

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        real_code = int(update.message.text.strip()) - 1
        if real_code < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Ø¹Ø¯Ø¯ Ù…Ø¹ØªØ¨Ø± ÙˆØ§Ø±Ø¯ Ú©Ù†ÛŒØ¯:")
        return CODE_STATE
    client = context.user_data.get('temp_client')
    if not client:
        await update.message.reply_text("Ù†Ø´Ø³Øª Ù…Ù†Ù‚Ø¶ÛŒ")
        return ConversationHandler.END
    try:
        await client.sign_in(context.user_data['phone'], str(real_code))
        session_string = client.session.save()
        await save_user_data(update.effective_user.id,
            api_id=context.user_data['api_id'],
            api_hash=context.user_data['api_hash'],
            session_string=session_string)
        await client.disconnect()
        await update.message.reply_text("âœ… Ù„Ø§Ú¯ÛŒÙ† Ù…ÙˆÙÙ‚")
        await main_menu(update, context)
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("Ø±Ù…Ø² Ø¯Ùˆ Ù…Ø±Ø­Ù„Ù‡â€ŒØ§ÛŒ Ø±Ø§ ÙˆØ§Ø±Ø¯ Ú©Ù†ÛŒØ¯:")
        return PASSWORD_STATE
    except Exception as e:
        await update.message.reply_text(f"Ø®Ø·Ø§: {e}")
        return ConversationHandler.END

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
    client = context.user_data.get('temp_client')
    if not client:
        await update.message.reply_text("Ù†Ø´Ø³Øª Ù…Ù†Ù‚Ø¶ÛŒ")
        return ConversationHandler.END
    try:
        await client.sign_in(password=pwd)
        session_string = client.session.save()
        await save_user_data(update.effective_user.id,
            api_id=context.user_data['api_id'],
            api_hash=context.user_data['api_hash'],
            session_string=session_string)
        await client.disconnect()
        await update.message.reply_text("âœ… Ù„Ø§Ú¯ÛŒÙ† Ù…ÙˆÙÙ‚")
        await main_menu(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"Ø±Ù…Ø² Ø§Ø´ØªØ¨Ø§Ù‡: {e}")
        return PASSWORD_STATE

# ---------- ØªÙ†Ø¸ÛŒÙ… Ú†Øª Ù‡Ø¯Ù ----------
async def set_target_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("ðŸ”„ Ø¯Ø± Ø­Ø§Ù„ Ø¯Ø±ÛŒØ§ÙØª Ù„ÛŒØ³Øª Ú†Øªâ€ŒÙ‡Ø§ÛŒ Ø§Ø®ÛŒØ±...")
    dialogs, error = await get_last_dialogs(query.from_user.id)
    if error:
        await query.edit_message_text(f"âŒ {error}\n\nÙ„Ø·ÙØ§Ù‹ Ø§Ø² Ú¯Ø²ÛŒÙ†Ù‡ Â«ÙˆØ±ÙˆØ¯ Ø¯Ø³ØªÛŒÂ» Ø§Ø³ØªÙØ§Ø¯Ù‡ Ú©Ù†ÛŒØ¯.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("âœï¸ ÙˆØ±ÙˆØ¯ Ø¯Ø³ØªÛŒ", callback_data="manual_input")], [InlineKeyboardButton("ðŸ”™ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ Ù…Ù†Ùˆ", callback_data="back_main")]]))
        return TARGET_CHAT_STATE
    if not dialogs:
        await query.edit_message_text("âš ï¸ Ù‡ÛŒÚ† Ú†ØªÛŒ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("âœï¸ ÙˆØ±ÙˆØ¯ Ø¯Ø³ØªÛŒ", callback_data="manual_input")], [InlineKeyboardButton("ðŸ”™ Ø¨Ø§Ø²Ú¯Ø´Øª Ø¨Ù‡ Ù…Ù†Ùˆ", callback_data="back_main")]]))
        return TARGET_CHAT_STATE
    buttons = []
    for chat_id, title in dialogs:
        short_title = title[:40] + "..." if len(title) > 40 else title
        buttons.append([InlineKeyboardButton(f"ðŸ“Œ {short_title}", callback_data=f"chat_{chat_id}")])
    buttons.append([InlineKeyboardButton("âœï¸ ÙˆØ±ÙˆØ¯ Ø¯Ø³ØªÛŒ", callback_data="manual_input")])
    buttons.append([InlineKeyboardButton("ðŸ”™ Ø¨Ø§Ø²Ú¯Ø´Øª", callback_data="back_main")])
    await query.edit_message_text("ðŸŽ¯ Ø§Ù†ØªØ®Ø§Ø¨ Ú†Øª Ù‡Ø¯Ù:", reply_markup=InlineKeyboardMarkup(buttons))
    return TARGET_CHAT_STATE

async def set_target_chat_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if data == "manual_input":
        await query.edit_message_text("ÛŒÙˆØ²Ø±Ù†ÛŒÙ… ÛŒØ§ Ø¢ÛŒØ¯ÛŒ Ø¹Ø¯Ø¯ÛŒ Ú†Øª Ø±Ø§ ÙˆØ§Ø±Ø¯ Ú©Ù†ÛŒØ¯:\n(Ø¨Ø±Ø§ÛŒ Ù„ØºÙˆ /cancel)")
        return TARGET_CHAT_STATE
    elif data == "back_main":
        await main_menu(update, context)
        return ConversationHandler.END
    elif data.startswith("chat_"):
        try:
            chat_id = int(data.split("_")[1])
            user_data = await get_user_data(user_id)
            if not user_data or not user_data['session_string']:
                await query.edit_message_text("âŒ Ù†Ø´Ø³Øª Ù…Ø¹ØªØ¨Ø± Ù†ÛŒØ³Øª. Ù„Ø·ÙØ§Ù‹ Ù„Ø§Ú¯ÛŒÙ† Ú©Ù†ÛŒØ¯.")
                return ConversationHandler.END
            client = TelegramClient(StringSession(user_data['session_string']), user_data['api_id'], user_data['api_hash'])
            await client.connect()
            try:
                entity = await get_entity_safe(client, chat_id)
                chat_title = entity.title or entity.first_name or str(chat_id)
            finally:
                await client.disconnect()
            await update_target_chat(user_id, chat_id, chat_title)
            await query.edit_message_text(f"âœ… Ú†Øª Ù‡Ø¯Ù ØªÙ†Ø¸ÛŒÙ… Ø´Ø¯: `{chat_title}`")
            await asyncio.sleep(1)
            await main_menu(update, context)
            return ConversationHandler.END
        except Exception as e:
            await query.edit_message_text(f"âŒ Ø®Ø·Ø§: {str(e)}")
            return ConversationHandler.END
    await query.edit_message_text("âŒ Ø¯Ø³ØªÙˆØ± Ù†Ø§Ù…Ø¹ØªØ¨Ø±.")
    return ConversationHandler.END

async def set_target_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_input = update.message.text.strip()
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await update.message.reply_text("âŒ Ø§Ø¨ØªØ¯Ø§ Ù„Ø§Ú¯ÛŒÙ† Ú©Ù†ÛŒØ¯.")
        return ConversationHandler.END
    client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
    await client.connect()
    try:
        await client.get_dialogs()
        if chat_input.lstrip('-').isdigit():
            chat_id = int(chat_input)
            entity = await get_entity_safe(client, chat_id)
        else:
            entity = await get_entity_safe(client, chat_input)
        chat_title = getattr(entity, 'title', None) or entity.first_name or str(entity.id)
        await update_target_chat(user_id, entity.id, chat_title)
        await update.message.reply_text(f"âœ… Ú†Øª Ù‡Ø¯Ù ØªÙ†Ø¸ÛŒÙ… Ø´Ø¯: `{chat_title}`")
    except Exception as e:
        await update.message.reply_text(f"âŒ Ø®Ø·Ø§: {str(e)}\n\nÙ†Ú©ØªÙ‡: Ø§Ú¯Ø± Ø§Ø² Ø¢ÛŒØ¯ÛŒ Ø¹Ø¯Ø¯ÛŒ Ø§Ø³ØªÙØ§Ø¯Ù‡ Ù…ÛŒâ€ŒÚ©Ù†ÛŒØ¯ØŒ Ù…Ø·Ù…Ø¦Ù† Ø´ÙˆÛŒØ¯ Ù‚Ø¨Ù„Ø§Ù‹ Ø¨Ø§ Ø¢Ù† Ú©Ø§Ø±Ø¨Ø± Ú†Øª Ø¯Ø§Ø´ØªÙ‡â€ŒØ§ÛŒØ¯.")
    finally:
        await client.disconnect()
    return ConversationHandler.END

# ---------- ØªÙ†Ø¸ÛŒÙ… Ø±ÛŒÙ¾Ù„ÛŒ ----------
async def set_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await query.edit_message_text("âŒ Ø§Ø¨ØªØ¯Ø§ Ù„Ø§Ú¯ÛŒÙ† Ú©Ù†ÛŒØ¯.")
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton("ðŸ”— Ø¨Ø§ Ù„ÛŒÙ†Ú© Ù¾ÛŒØ§Ù…", callback_data="reply_link")],
        [InlineKeyboardButton("ðŸ“‹ Ø§Ù†ØªØ®Ø§Ø¨ Ø§Ø² Ú†Øªâ€ŒÙ‡Ø§", callback_data="reply_from_chat")],
        [InlineKeyboardButton("ðŸ”™ Ø§Ù†ØµØ±Ø§Ù", callback_data="back_main")]
    ]
    await query.edit_message_text("ðŸ” **ØªÙ†Ø¸ÛŒÙ… Ø±ÛŒÙ¾Ù„ÛŒ ÛŒÚ©Ø¨Ø§Ø± Ù…ØµØ±Ù**\n\nÙ„Ø·ÙØ§Ù‹ Ø±ÙˆØ´ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown')
    return REPLY_METHOD_STATE

async def reply_method_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if data == "back_main":
        await main_menu(update, context)
        return ConversationHandler.END
    elif data == "reply_link":
        await query.edit_message_text("Ù„Ø·ÙØ§Ù‹ Ù„ÛŒÙ†Ú© Ù¾ÛŒØ§Ù… Ù…ÙˆØ±Ø¯ Ù†Ø¸Ø± Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯:\n(Ù…Ø«Ø§Ù„: https://t.me/username/123 ÛŒØ§ https://t.me/c/123456789/100)\nØ¨Ø±Ø§ÛŒ Ù„ØºÙˆ /cancel")
        return REPLY_LINK_STATE
    elif data == "reply_from_chat":
        await query.edit_message_text("ðŸ”„ Ø¯Ø± Ø­Ø§Ù„ Ø¯Ø±ÛŒØ§ÙØª Ù„ÛŒØ³Øª Ú†Øªâ€ŒÙ‡Ø§ÛŒ Ø§Ø®ÛŒØ±...")
        dialogs, error = await get_last_dialogs(user_id)
        if error:
            await query.edit_message_text(f"âŒ {error}")
            return ConversationHandler.END
        if not dialogs:
            await query.edit_message_text("âš ï¸ Ù‡ÛŒÚ† Ú†ØªÛŒ Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯.")
            return ConversationHandler.END
        buttons = []
        for chat_id, title in dialogs:
            short_title = title[:40] + "..." if len(title) > 40 else title
            buttons.append([InlineKeyboardButton(f"ðŸ“Œ {short_title}", callback_data=f"reply_chat_{chat_id}")])
        buttons.append([InlineKeyboardButton("ðŸ”™ Ø§Ù†ØµØ±Ø§Ù", callback_data="back_main")])
        await query.edit_message_text("ðŸ” Ù…Ø±Ø­Ù„Ù‡ 1: Ú†Øª Ù…ÙˆØ±Ø¯ Ù†Ø¸Ø± Ø¨Ø±Ø§ÛŒ Ø±ÛŒÙ¾Ù„ÛŒ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯:", reply_markup=InlineKeyboardMarkup(buttons))
        return REPLY_SELECT_CHAT_STATE
    else:
        await query.edit_message_text("âŒ Ú¯Ø²ÛŒÙ†Ù‡ Ù†Ø§Ù…Ø¹ØªØ¨Ø±.")
        return ConversationHandler.END

async def reply_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = update.message.text.strip()
    chat_id, msg_id = await parse_message_link(link)
    if not chat_id or not msg_id:
        await update.message.reply_text("âŒ Ù„ÛŒÙ†Ú© Ù…Ø¹ØªØ¨Ø± Ù†ÛŒØ³Øª. Ù„Ø·ÙØ§Ù‹ Ù„ÛŒÙ†Ú© Ù¾ÛŒØ§Ù… ØªÙ„Ú¯Ø±Ø§Ù… Ø±Ø§ Ø¨ÙØ±Ø³ØªÛŒØ¯.")
        return REPLY_LINK_STATE
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await update.message.reply_text("âŒ Ø§Ø¨ØªØ¯Ø§ Ù„Ø§Ú¯ÛŒÙ† Ú©Ù†ÛŒØ¯.")
        return ConversationHandler.END
    client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
    await client.connect()
    try:
        if isinstance(chat_id, str):
            entity = await get_entity_safe(client, chat_id)
            chat_id = entity.id
        await set_reply(user_id, chat_id, msg_id, active=True)
        await update.message.reply_text(f"âœ… Ø±ÛŒÙ¾Ù„ÛŒ Ø¨Ø§ Ù…ÙˆÙÙ‚ÛŒØª ØªÙ†Ø¸ÛŒÙ… Ø´Ø¯.\nÚ†Øª: `{chat_id}`\nÙ¾ÛŒØ§Ù… ID: `{msg_id}`\n\nØ§Ú©Ù†ÙˆÙ† ÛŒÚ© ÙØ§ÛŒÙ„ ØµÙˆØªÛŒ ÛŒØ§ ÙˆÛŒØ¯ÛŒÙˆÛŒÛŒ Ø¨ÙØ±Ø³ØªÛŒØ¯ ØªØ§ Ø¨Ù‡ Ø¹Ù†ÙˆØ§Ù† Ø±ÛŒÙ¾Ù„ÛŒ Ø¨Ù‡ Ø¢Ù† Ù¾ÛŒØ§Ù… Ø§Ø±Ø³Ø§Ù„ Ø´ÙˆØ¯ (ÙÙ‚Ø· ÛŒÚ©Ø¨Ø§Ø±).")
        await main_menu(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"âŒ Ø®Ø·Ø§: {str(e)}")
        return ConversationHandler.END
    finally:
        await client.disconnect()

async def reply_select_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if data == "back_main":
        await main_menu(update, context)
        return ConversationHandler.END
    elif data.startswith("reply_chat_"):
        chat_id = int(data.split("_")[2])
        context.user_data['reply_chat_id'] = chat_id
        await query.edit_message_text(f"ðŸ”„ Ø¯Ø± Ø­Ø§Ù„ Ø¯Ø±ÛŒØ§ÙØª Ù¾ÛŒØ§Ù…â€ŒÙ‡Ø§ÛŒ Ú†Øª...")
        messages, error = await get_last_messages(user_id, chat_id, limit=10)
        if error:
            await query.edit_message_text(f"âŒ {error}")
            return ConversationHandler.END
        if not messages:
            await query.edit_message_text("âš ï¸ Ù‡ÛŒÚ† Ù¾ÛŒØ§Ù…ÛŒ Ø¯Ø± Ø§ÛŒÙ† Ú†Øª ÛŒØ§ÙØª Ù†Ø´Ø¯.")
            return ConversationHandler.END
        buttons = []
        for msg_id, short_text in messages:
            buttons.append([InlineKeyboardButton(f"ðŸ“ {short_text}", callback_data=f"reply_msg_{msg_id}")])
        buttons.append([InlineKeyboardButton("ðŸ”™ Ø§Ù†ØµØ±Ø§Ù", callback_data="back_main")])
        await query.edit_message_text(f"ðŸ” Ù…Ø±Ø­Ù„Ù‡ 2: Ù¾ÛŒØ§Ù… Ù…ÙˆØ±Ø¯ Ù†Ø¸Ø± Ø¨Ø±Ø§ÛŒ Ø±ÛŒÙ¾Ù„ÛŒ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯:", reply_markup=InlineKeyboardMarkup(buttons))
        return REPLY_SELECT_MSG_STATE
    else:
        await query.edit_message_text("âŒ Ú¯Ø²ÛŒÙ†Ù‡ Ù†Ø§Ù…Ø¹ØªØ¨Ø±.")
        return ConversationHandler.END

async def reply_select_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if data == "back_main":
        await main_menu(update, context)
        return ConversationHandler.END
    elif data.startswith("reply_msg_"):
        msg_id = int(data.split("_")[2])
        chat_id = context.user_data.get('reply_chat_id')
        if not chat_id:
            await query.edit_message_text("âŒ Ø®Ø·Ø§: Ú†Øª Ø§Ù†ØªØ®Ø§Ø¨ Ù†Ø´Ø¯Ù‡.")
            return ConversationHandler.END
        await set_reply(user_id, chat_id, msg_id, active=True)
        await query.edit_message_text(f"âœ… Ø±ÛŒÙ¾Ù„ÛŒ Ø¨Ø§ Ù…ÙˆÙÙ‚ÛŒØª ØªÙ†Ø¸ÛŒÙ… Ø´Ø¯.\nÚ†Øª: `{chat_id}`\nÙ¾ÛŒØ§Ù… ID: `{msg_id}`\n\nØ§Ú©Ù†ÙˆÙ† ÛŒÚ© ÙØ§ÛŒÙ„ ØµÙˆØªÛŒ ÛŒØ§ ÙˆÛŒØ¯ÛŒÙˆÛŒÛŒ Ø¨ÙØ±Ø³ØªÛŒØ¯ ØªØ§ Ø¨Ù‡ Ø¹Ù†ÙˆØ§Ù† Ø±ÛŒÙ¾Ù„ÛŒ Ø¨Ù‡ Ø¢Ù† Ù¾ÛŒØ§Ù… Ø§Ø±Ø³Ø§Ù„ Ø´ÙˆØ¯ (ÙÙ‚Ø· ÛŒÚ©Ø¨Ø§Ø±).")
        await asyncio.sleep(2)
        await main_menu(update, context)
        return ConversationHandler.END
    else:
        await query.edit_message_text("âŒ Ú¯Ø²ÛŒÙ†Ù‡ Ù†Ø§Ù…Ø¹ØªØ¨Ø±.")
        return ConversationHandler.END

# ---------- ÙˆØ¶Ø¹ÛŒØª Ùˆ Ø®Ø±ÙˆØ¬ ----------
async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = await get_user_data(query.from_user.id)
    if not data or not data['session_string']:
        text = "âŒ Ù„Ø§Ú¯ÛŒÙ† Ù†ÛŒØ³ØªÛŒØ¯"
    else:
        text = f"âœ… Ù„Ø§Ú¯ÛŒÙ† Ù‡Ø³ØªÛŒØ¯\nðŸŽ¯ Ú†Øª Ù‡Ø¯Ù: `{data['target_chat_title'] or data['target_chat_id'] if data['target_chat_id'] else 'ØªÙ†Ø¸ÛŒÙ… Ù†Ø´Ø¯Ù‡'}`\nðŸ” Ø±ÛŒÙ¾Ù„ÛŒ: {'ÙØ¹Ø§Ù„' if data['reply_active'] else 'ØºÛŒØ±ÙØ¹Ø§Ù„'}\nðŸ“¹ ÙˆÛŒØ¯ÛŒÙˆ Ú©Ø§Ù„: {'ÙØ¹Ø§Ù„' if data.get('auto_video_enabled') else 'ØºÛŒØ±ÙØ¹Ø§Ù„'}"
    await query.edit_message_text(text, parse_mode='Markdown')

async def clear_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await clear_reply(user_id)
    await query.edit_message_text("âœ… Ø±ÛŒÙ¾Ù„ÛŒ ØºÛŒØ±ÙØ¹Ø§Ù„ Ø´Ø¯.")
    await asyncio.sleep(1)
    await main_menu(update, context)

async def disable_auto_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await disable_auto_video(user_id)
    if user_id in call_clients:
        try:
            await call_clients[user_id].stop()
            del call_clients[user_id]
        except:
            pass
    await query.edit_message_text("âœ… Ù‚Ø§Ø¨Ù„ÛŒØª ÙˆÛŒØ¯ÛŒÙˆ Ú©Ø§Ù„ ØºÛŒØ±ÙØ¹Ø§Ù„ Ø´Ø¯.")
    await asyncio.sleep(1)
    await main_menu(update, context)

async def logout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await save_user_data(user_id, session_string="")
    if user_id in call_clients:
        try:
            await call_clients[user_id].stop()
            del call_clients[user_id]
        except:
            pass
    await query.edit_message_text("âœ… Ø§Ø² Ø§Ú©Ø§Ù†Øª Ø®Ø§Ø±Ø¬ Ø´Ø¯ÛŒØ¯.")

# ---------- ØªÙ†Ø¸ÛŒÙ… ÙˆÛŒØ¯ÛŒÙˆ Ú©Ø§Ù„ ----------
async def set_auto_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await query.edit_message_text("âŒ Ø§Ø¨ØªØ¯Ø§ Ù„Ø§Ú¯ÛŒÙ† Ú©Ù†ÛŒØ¯.")
        return ConversationHandler.END
    await query.edit_message_text("ðŸ“¹ Ù„Ø·ÙØ§Ù‹ ÙˆÛŒØ¯ÛŒÙˆÛŒÛŒ Ú©Ù‡ Ù…ÛŒâ€ŒØ®ÙˆØ§Ù‡ÛŒØ¯ Ø¯Ø± ØªÙ…Ø§Ø³â€ŒÙ‡Ø§ÛŒ ÙˆÛŒØ¯ÛŒÙˆÛŒÛŒ Ù¾Ø®Ø´ Ø´ÙˆØ¯ Ø±Ø§ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.\nÙˆÛŒØ¯ÛŒÙˆ Ø¨Ø§ÛŒØ¯ Ú©ÙˆØªØ§Ù‡ Ø¨Ø§Ø´Ø¯ (Ø­Ø¯Ø§Ú©Ø«Ø± 60 Ø«Ø§Ù†ÛŒÙ‡).\nØ¨Ø±Ø§ÛŒ Ù„ØºÙˆ /cancel")
    return AUTO_VIDEO_STATE

async def handle_auto_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("ðŸ”„ Ø¯Ø± Ø­Ø§Ù„ Ù¾Ø±Ø¯Ø§Ø²Ø´ ÙˆÛŒØ¯ÛŒÙˆ Ø¨Ø±Ø§ÛŒ ÙˆÛŒØ¯ÛŒÙˆ Ú©Ø§Ù„...")
    try:
        data = await get_user_data(user_id)
        if not data or not data['session_string']:
            await status_msg.edit_text("âŒ Ù„Ø§Ú¯ÛŒÙ† Ù†ÛŒØ³ØªÛŒØ¯.")
            return ConversationHandler.END
        if not update.message.video:
            await status_msg.edit_text("âŒ Ù„Ø·ÙØ§Ù‹ ÛŒÚ© ÙØ§ÛŒÙ„ ÙˆÛŒØ¯ÛŒÙˆÛŒÛŒ Ø§Ø±Ø³Ø§Ù„ Ú©Ù†ÛŒØ¯.")
            return AUTO_VIDEO_STATE
        duration = getattr(update.message.video, 'duration', 0)
        if duration > 60:
            await status_msg.edit_text("âŒ ÙˆÛŒØ¯ÛŒÙˆ Ù†Ø¨Ø§ÛŒØ¯ Ø¨ÛŒØ´ØªØ± Ø§Ø² 60 Ø«Ø§Ù†ÛŒÙ‡ Ø¨Ø§Ø´Ø¯.")
            return AUTO_VIDEO_STATE

        await status_msg.edit_text("ðŸ“¥ Ø¯Ø± Ø­Ø§Ù„ Ø¯Ø§Ù†Ù„ÙˆØ¯ ÙˆÛŒØ¯ÛŒÙˆ...")
        file = await update.message.video.get_file()
        file_path = str(await file.download_to_drive())
        if not os.path.exists(file_path):
            await status_msg.edit_text("âŒ Ø®Ø·Ø§ Ø¯Ø± Ø¯Ø§Ù†Ù„ÙˆØ¯ ÙØ§ÛŒÙ„.")
            return AUTO_VIDEO_STATE

        await status_msg.edit_text("ðŸ”„ Ø¯Ø± Ø­Ø§Ù„ ØªØ¨Ø¯ÛŒÙ„ ÙˆÛŒØ¯ÛŒÙˆ Ø¨Ù‡ Ù…Ø±Ø¨Ø¹ (Ø¨Ø¯ÙˆÙ† Ø­Ø§Ø´ÛŒÙ‡)...")
        square_path = file_path + "_square.mp4"
        try:
            final_file_path = await convert_to_square_ffmpeg(file_path, square_path)
        except Exception as e:
            error_detail = str(e)
            print(f"ffmpeg error detail: {error_detail}")  # Ù„Ø§Ú¯ Ø¯Ø± Ø±Ù†Ø¯Ø±
            await status_msg.edit_text(f"âš ï¸ Ø®Ø·Ø§ Ø¯Ø± ØªØ¨Ø¯ÛŒÙ„: {error_detail[:200]}\nØ§Ø³ØªÙØ§Ø¯Ù‡ Ø§Ø² ÙˆÛŒØ¯ÛŒÙˆÛŒ Ø§ØµÙ„ÛŒ...")
            final_file_path = file_path

        await enable_auto_video(user_id, final_file_path)

        if user_id not in call_clients:
            call_client = await setup_pytgcalls(user_id, data['session_string'], data['api_id'], data['api_hash'])
            if call_client:
                call_clients[user_id] = call_client
                @call_client.on_call()
                async def on_incoming_call(call: Call, bound_user_id=user_id):
                    current_data = await get_user_data(bound_user_id)
                    vid_path = current_data.get('auto_video_path') if current_data else None
                    if vid_path and os.path.exists(vid_path):
                        await answer_call(call.chat_id, call_clients[bound_user_id], vid_path)

        await status_msg.edit_text("âœ… ÙˆÛŒØ¯ÛŒÙˆ Ø¨Ø§ Ù…ÙˆÙÙ‚ÛŒØª ØªÙ†Ø¸ÛŒÙ… Ø´Ø¯.\nØ§Ø² Ø§ÛŒÙ† Ø¨Ù‡ Ø¨Ø¹Ø¯ Ù‡Ø± ØªÙ…Ø§Ø³ ÙˆÛŒØ¯ÛŒÙˆÛŒÛŒ Ø¨Ù‡ Ø´Ù…Ø§ØŒ Ø¨Ø§ Ø§ÛŒÙ† ÙˆÛŒØ¯ÛŒÙˆ Ù¾Ø§Ø³Ø® Ø¯Ø§Ø¯Ù‡ Ù…ÛŒâ€ŒØ´ÙˆØ¯.")
        if os.path.exists(file_path) and file_path != final_file_path:
            os.remove(file_path)
        await main_menu(update, context)
        return ConversationHandler.END
    except Exception as e:
        error_full = str(e)
        print(f"Full error in handle_auto_video_file: {error_full}")  # Ù„Ø§Ú¯ Ø¯Ø± Ø±Ù†Ø¯Ø±
        await status_msg.edit_text(f"âŒ Ø®Ø·Ø§: {error_full[:300]}")
        return AUTO_VIDEO_STATE

# ---------- Ù‡Ù†Ø¯Ù„Ø± ÙØ§ÛŒÙ„ Ø¹Ù…ÙˆÙ…ÛŒ ----------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("ðŸ”„ Ø¯Ø± Ø­Ø§Ù„ Ù¾Ø±Ø¯Ø§Ø²Ø´...")
    try:
        data = await get_user_data(user_id)
        if not data or not data['session_string']:
            await status_msg.edit_text("âŒ Ù„Ø§Ú¯ÛŒÙ† Ù†ÛŒØ³ØªÛŒØ¯. Ø§Ø² Ù…Ù†Ùˆ Ù„Ø§Ú¯ÛŒÙ† Ú©Ù†ÛŒØ¯.")
            return
        if data['reply_active'] and data['reply_chat_id'] and data['reply_msg_id']:
            target_chat_id = data['reply_chat_id']
            reply_to = data['reply_msg_id']
        else:
            if not data['target_chat_id']:
                await status_msg.edit_text("âŒ Ú†Øª Ù‡Ø¯Ù ØªÙ†Ø¸ÛŒÙ… Ù†Ø´Ø¯Ù‡ Ùˆ Ø±ÛŒÙ¾Ù„ÛŒ ÙØ¹Ø§Ù„ÛŒ Ù‡Ù… Ù†Ø¯Ø§Ø±ÛŒØ¯.")
                return
            target_chat_id = data['target_chat_id']
            reply_to = None
        msg = update.message
        is_audio = bool(msg.audio or msg.voice)
        is_video = bool(msg.video or msg.video_note)
        if not is_audio and not is_video:
            await status_msg.edit_text("âŒ ÙÙ‚Ø· ÙØ§ÛŒÙ„ ØµÙˆØªÛŒ ÛŒØ§ ÙˆÛŒØ¯ÛŒÙˆÛŒÛŒ Ù¾Ø´ØªÛŒØ¨Ø§Ù†ÛŒ Ù…ÛŒâ€ŒØ´ÙˆØ¯.")
            return
        duration = 3
        if is_audio:
            duration = getattr(msg.audio or msg.voice, 'duration', 3)
        else:
            duration = getattr(msg.video or msg.video_note, 'duration', 3)
        if not duration or duration <= 0:
            duration = 3
        await status_msg.edit_text("ðŸ“¥ Ø¯Ø± Ø­Ø§Ù„ Ø¯Ø§Ù†Ù„ÙˆØ¯ ÙØ§ÛŒÙ„...")
        if is_audio:
            file = await (msg.audio or msg.voice).get_file()
        else:
            file = await (msg.video or msg.video_note).get_file()
        file_path = str(await file.download_to_drive())
        final_file_path = file_path
        if is_video:
            await status_msg.edit_text("ðŸ”„ Ø¯Ø± Ø­Ø§Ù„ ØªØ¨Ø¯ÛŒÙ„ ÙˆÛŒØ¯ÛŒÙˆ Ø¨Ù‡ Ù…Ø±Ø¨Ø¹ (Ø¨Ø¯ÙˆÙ† Ø­Ø§Ø´ÛŒÙ‡)...")
            square_path = file_path + "_square.mp4"
            try:
                final_file_path = await convert_to_square_ffmpeg(file_path, square_path)
            except Exception as e:
                await status_msg.edit_text(f"âš ï¸ Ø®Ø·Ø§ Ø¯Ø± ØªØ¨Ø¯ÛŒÙ„: {str(e)[:200]}. Ø§Ø±Ø³Ø§Ù„ ÙˆÛŒØ¯ÛŒÙˆÛŒ Ø§ØµÙ„ÛŒ...")
                final_file_path = file_path
        client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
        await client.connect()
        if not await ensure_session_active(client):
            await status_msg.edit_text("âŒ Ù†Ø´Ø³Øª Ù…Ù†Ù‚Ø¶ÛŒ Ø´Ø¯Ù‡. Ù„Ø·ÙØ§Ù‹ Ø¯ÙˆØ¨Ø§Ø±Ù‡ Ù„Ø§Ú¯ÛŒÙ† Ú©Ù†ÛŒØ¯.")
            await client.disconnect()
            return
        target_entity = await get_input_entity_safe(client, target_chat_id)
        await status_msg.edit_text(f"ðŸŽ¬ {'Ø¯Ø± Ø­Ø§Ù„ Ø§Ø±Ø³Ø§Ù„ ÙˆÛŒØ³' if is_audio else 'Ø¯Ø± Ø­Ø§Ù„ Ø§Ø±Ø³Ø§Ù„ ÙˆÛŒØ¯ÛŒÙˆ (Ø¯Ø§ÛŒØ±Ù‡â€ŒØ§ÛŒ)'}...")
        if is_audio:
            await send_as_voice_note(client, target_entity, final_file_path, duration, reply_to=reply_to)
        else:
            await send_as_video_note(client, target_entity, final_file_path, duration, reply_to=reply_to)
        if reply_to:
            await status_msg.edit_text(f"âœ… {'ÙˆÛŒØ³' if is_audio else 'ÙˆÛŒØ¯ÛŒÙˆ'} Ù†ÙˆØª Ø¨Ù‡ Ø¹Ù†ÙˆØ§Ù† Ø±ÛŒÙ¾Ù„ÛŒ Ø§Ø±Ø³Ø§Ù„ Ø´Ø¯.")
            await clear_reply(user_id)
        else:
            await status_msg.edit_text(f"âœ… {'ÙˆÛŒØ³' if is_audio else 'ÙˆÛŒØ¯ÛŒÙˆ'} Ù†ÙˆØª Ø§Ø±Ø³Ø§Ù„ Ø´Ø¯.")
        await client.disconnect()
        if os.path.exists(file_path):
            os.remove(file_path)
        if is_video and os.path.exists(final_file_path) and final_file_path != file_path:
            os.remove(final_file_path)
    except FloodWaitError as e:
        await status_msg.edit_text(f"â³ Ù…Ø­Ø¯ÙˆØ¯ÛŒØª ØªÙ„Ú¯Ø±Ø§Ù…: {e.seconds} Ø«Ø§Ù†ÛŒÙ‡ ØµØ¨Ø± Ú©Ù†ÛŒØ¯")
    except Exception as e:
        await status_msg.edit_text(f"âŒ Ø®Ø·Ø§: {str(e)}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ù„ØºÙˆ Ø´Ø¯.")
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await main_menu(update, context)

# ========== ÙˆØ¨ Ø³Ø±ÙˆØ± ==========
async def health_check(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()
    await asyncio.Event().wait()

async def main():
    await init_db()
    await restore_auto_video_calls()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(login_start, pattern='^login$')],
        states={
            API_ID_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_api_id)],
            API_HASH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_api_hash)],
            PHONE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            CODE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_code)],
            PASSWORD_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_target_start, pattern='^set_target$')],
        states={
            TARGET_CHAT_STATE: [
                CallbackQueryHandler(set_target_chat_button, pattern='^(chat_|manual_input|back_main)'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_target_manual)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_reply_start, pattern='^set_reply$')],
        states={
            REPLY_METHOD_STATE: [CallbackQueryHandler(reply_method_choice, pattern='^(reply_link|reply_from_chat|back_main)$')],
            REPLY_LINK_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reply_link_handler)],
            REPLY_SELECT_CHAT_STATE: [CallbackQueryHandler(reply_select_chat, pattern='^(reply_chat_|back_main)')],
            REPLY_SELECT_MSG_STATE: [CallbackQueryHandler(reply_select_msg, pattern='^(reply_msg_|back_main)')],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    auto_video_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_auto_video_start, pattern='^set_auto_video$')],
        states={
            AUTO_VIDEO_STATE: [MessageHandler(filters.VIDEO, handle_auto_video_file)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(auto_video_conv)
    application.add_handler(CallbackQueryHandler(status_callback, pattern='^status$'))
    application.add_handler(CallbackQueryHandler(clear_reply_callback, pattern='^clear_reply$'))
    application.add_handler(CallbackQueryHandler(disable_auto_video_callback, pattern='^disable_auto_video$'))
    application.add_handler(CallbackQueryHandler(logout_callback, pattern='^logout$'))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.AUDIO | filters.VIDEO | filters.VOICE | filters.VIDEO_NOTE, handle_file))
    await application.initialize()
    await application.start()
    asyncio.create_task(application.updater.start_polling())
    await run_web()

if __name__ == '__main__':
    asyncio.run(main())
