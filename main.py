import asyncio
import os
import re
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

# تلاش برای import pytgcalls (اختیاری)
try:
    from pytgcalls import PyTgCalls
    from pytgcalls.types import Update as CallUpdate
    from pytgcalls.types import Call
    from pytgcalls.types.input_stream import AudioStream, VideoStream, InputStream
    PYTGCALLS_AVAILABLE = True
except ImportError:
    PYTGCALLS_AVAILABLE = False
    print("⚠️ PyTgCalls not installed. Video call feature disabled.")

BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
if not BOT_TOKEN or not DATABASE_URL:
    raise Exception("BOT_TOKEN and DATABASE_URL required")

API_ID_STATE, API_HASH_STATE, PHONE_STATE, CODE_STATE, PASSWORD_STATE, TARGET_CHAT_STATE = range(6)
REPLY_METHOD_STATE, REPLY_SELECT_CHAT_STATE, REPLY_SELECT_MSG_STATE, REPLY_LINK_STATE = range(6, 10)

# ========== دیتابیس ==========
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

# ========== توابع ویدیو کال (فقط در صورت موجود بودن pytgcalls) ==========
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

if PYTGCALLS_AVAILABLE:
    async def setup_pytgcalls(user_id, session_string, api_id, api_hash):
        telethon_client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await telethon_client.connect()
        if not await ensure_session_active(telethon_client):
            await telethon_client.disconnect()
            return None
        call_client = PyTgCalls(telethon_client)
        await call_client.start()
        return call_client

    async def answer_call(chat_id, call_client, video_path):
        try:
            await call_client.answer_call(chat_id)
            await call_client.play(
                chat_id,
                InputStream(
                    AudioStream(video_path),
                    VideoStream(video_path)
                )
            )
            # گرفتن مدت زمان ویدیو با ffprobe
            result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', video_path], capture_output=True, text=True)
            duration = float(result.stdout.strip())
            await asyncio.sleep(duration)
            await call_client.leave_call(chat_id)
        except Exception as e:
            print(f"Error answering call for {chat_id}: {e}")

    async def restore_auto_video_calls():
        async with await get_conn() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute('SELECT user_id, api_id, api_hash, session_string, auto_video_path FROM user_data WHERE auto_video_enabled = TRUE AND session_string IS NOT NULL')
                active_users = await cur.fetchall()
                for user in active_users:
                    user_id = user['user_id']
                    api_id = user['api_id']
                    api_hash = user['api_hash']
                    session_str = user['session_string']
                    video_path = user['auto_video_path']
                    if video_path and os.path.exists(video_path):
                        call_client = await setup_pytgcalls(user_id, session_str, api_id, api_hash)
                        if call_client:
                            call_clients[user_id] = call_client
                            @call_client.on_call()
                            async def on_incoming_call(call: Call):
                                if call.chat_id == user_id:
                                    current_data = await get_user_data(user_id)
                                    vid_path = current_data.get('auto_video_path')
                                    if vid_path and os.path.exists(vid_path):
                                        await answer_call(call.chat_id, call_client, vid_path)
else:
    # توابع خالی برای زمانی که pytgcalls نصب نیست
    async def setup_pytgcalls(*args, **kwargs):
        return None
    async def answer_call(*args, **kwargs):
        pass
    async def restore_auto_video_calls():
        pass

# ========== تبدیل ویدیو به مربع ==========
async def convert_to_square_ffmpeg(input_path, output_path, target_size=480):
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    except:
        raise Exception("ffmpeg not found")
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', f'crop=min(iw\\,ih):min(iw\\,ih),scale={target_size}:{target_size}',
        '-c:a', 'copy', '-y', output_path
    ]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise Exception(f"ffmpeg error: {stderr.decode()}")
    return output_path

# ========== توابع کمکی Telethon ==========
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
        return None, "لاگین نشده‌اید"
    client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
    await client.connect()
    try:
        await client.get_me()
        dialogs = await client.get_dialogs(limit=10)
        result = [(d.id, d.title or d.name or (d.entity.first_name if d.entity else str(d.id))) for d in dialogs]
        return result, None
    except Exception as e:
        return None, f"خطا: {str(e)}"
    finally:
        await client.disconnect()

async def get_last_messages(user_id, chat_id, limit=10):
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        return None, "لاگین نشده‌اید"
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
                    text = "📷 رسانه" if msg.media else "پیام خالی"
                short_text = text[:20] + "..." if len(text) > 20 else text
                messages.append((msg.id, short_text))
        return messages, None
    except Exception as e:
        return None, f"خطا: {str(e)}"
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

# ========== منوی اصلی ==========
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = await get_user_data(user_id)
    text = "🎛 **پنل مدیریت**\n\n"
    if data and data['session_string']:
        text += "✅ لاگین هستید\n"
        if data['target_chat_id']:
            text += f"🎯 چت هدف: `{data['target_chat_title'] or data['target_chat_id']}`\n"
        else:
            text += "❌ چت هدف تنظیم نشده\n"
        if data.get('reply_active') and data.get('reply_msg_id'):
            text += f"🔁 ریپلی فعال به پیام ID `{data['reply_msg_id']}`\n"
        else:
            text += "🔁 ریپلی: غیرفعال\n"
        if data.get('auto_video_enabled') and data.get('auto_video_path'):
            text += "📹 ویدیو کال: **فعال**\n"
        else:
            text += "📹 ویدیو کال: **غیرفعال**\n"
    else:
        text += "❌ لاگین نیستید\n"
    buttons = [
        [InlineKeyboardButton("🔐 لاگین", callback_data="login")],
        [InlineKeyboardButton("🎯 تنظیم چت هدف", callback_data="set_target")],
        [InlineKeyboardButton("🔁 تنظیم ریپلی", callback_data="set_reply")],
        [InlineKeyboardButton("❌ لغو ریپلی", callback_data="clear_reply")],
    ]
    # اضافه کردن دکمه‌های ویدیو کال فقط در صورت موجود بودن pytgcalls
    if PYTGCALLS_AVAILABLE:
        buttons.append([InlineKeyboardButton("📹 تنظیم ویدیو کال", callback_data="set_auto_video")])
        buttons.append([InlineKeyboardButton("🚫 لغو ویدیو کال", callback_data="disable_auto_video")])
    buttons.append([InlineKeyboardButton("📋 وضعیت", callback_data="status")])
    buttons.append([InlineKeyboardButton("🚪 خروج", callback_data="logout")])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

# ---------- لاگین (بدون تغییر) ----------
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("api_id را وارد کنید:")
    return API_ID_STATE

async def login_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['api_id'] = int(update.message.text.strip())
        await update.message.reply_text("api_hash را وارد کنید:")
        return API_HASH_STATE
    except ValueError:
        await update.message.reply_text("api_id باید عدد باشد. دوباره:")
        return API_ID_STATE

async def login_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text("شماره تلفن (با کد کشور):")
    return PHONE_STATE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    client = TelegramClient(StringSession(), context.user_data['api_id'], context.user_data['api_hash'])
    await client.connect()
    try:
        await client.send_code_request(phone)
        context.user_data['temp_client'] = client
        await update.message.reply_text("کد تأیید +1 را وارد کنید (مثلاً 12345->12346):")
        return CODE_STATE
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")
        return ConversationHandler.END

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        real_code = int(update.message.text.strip()) - 1
        if real_code < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("عدد معتبر وارد کنید:")
        return CODE_STATE
    client = context.user_data.get('temp_client')
    if not client:
        await update.message.reply_text("نشست منقضی")
        return ConversationHandler.END
    try:
        await client.sign_in(context.user_data['phone'], str(real_code))
        session_string = client.session.save()
        await save_user_data(update.effective_user.id,
            api_id=context.user_data['api_id'],
            api_hash=context.user_data['api_hash'],
            session_string=session_string)
        await client.disconnect()
        await update.message.reply_text("✅ لاگین موفق")
        await main_menu(update, context)
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("رمز دو مرحله‌ای را وارد کنید:")
        return PASSWORD_STATE
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")
        return ConversationHandler.END

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
    client = context.user_data.get('temp_client')
    if not client:
        await update.message.reply_text("نشست منقضی")
        return ConversationHandler.END
    try:
        await client.sign_in(password=pwd)
        session_string = client.session.save()
        await save_user_data(update.effective_user.id,
            api_id=context.user_data['api_id'],
            api_hash=context.user_data['api_hash'],
            session_string=session_string)
        await client.disconnect()
        await update.message.reply_text("✅ لاگین موفق")
        await main_menu(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"رمز اشتباه: {e}")
        return PASSWORD_STATE

# ---------- تنظیم چت هدف ----------
async def set_target_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔄 در حال دریافت لیست چت‌های اخیر...")
    dialogs, error = await get_last_dialogs(query.from_user.id)
    if error:
        await query.edit_message_text(f"❌ {error}\n\nلطفاً از گزینه «ورود دستی» استفاده کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ ورود دستی", callback_data="manual_input")], [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_main")]]))
        return TARGET_CHAT_STATE
    if not dialogs:
        await query.edit_message_text("⚠️ هیچ چتی پیدا نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ ورود دستی", callback_data="manual_input")], [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_main")]]))
        return TARGET_CHAT_STATE
    buttons = []
    for chat_id, title in dialogs:
        short_title = title[:40] + "..." if len(title) > 40 else title
        buttons.append([InlineKeyboardButton(f"📌 {short_title}", callback_data=f"chat_{chat_id}")])
    buttons.append([InlineKeyboardButton("✏️ ورود دستی", callback_data="manual_input")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")])
    await query.edit_message_text("🎯 انتخاب چت هدف:", reply_markup=InlineKeyboardMarkup(buttons))
    return TARGET_CHAT_STATE

async def set_target_chat_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if data == "manual_input":
        await query.edit_message_text("یوزرنیم یا آیدی عددی چت را وارد کنید:\n(برای لغو /cancel)")
        return TARGET_CHAT_STATE
    elif data == "back_main":
        await main_menu(update, context)
        return ConversationHandler.END
    elif data.startswith("chat_"):
        try:
            chat_id = int(data.split("_")[1])
            user_data = await get_user_data(user_id)
            if not user_data or not user_data['session_string']:
                await query.edit_message_text("❌ نشست معتبر نیست. لطفاً لاگین کنید.")
                return ConversationHandler.END
            client = TelegramClient(StringSession(user_data['session_string']), user_data['api_id'], user_data['api_hash'])
            await client.connect()
            try:
                entity = await get_entity_safe(client, chat_id)
                chat_title = entity.title or entity.first_name or str(chat_id)
            finally:
                await client.disconnect()
            await update_target_chat(user_id, chat_id, chat_title)
            await query.edit_message_text(f"✅ چت هدف تنظیم شد: `{chat_title}`")
            await asyncio.sleep(1)
            await main_menu(update, context)
            return ConversationHandler.END
        except Exception as e:
            await query.edit_message_text(f"❌ خطا: {str(e)}")
            return ConversationHandler.END
    await query.edit_message_text("❌ دستور نامعتبر.")
    return ConversationHandler.END

async def set_target_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_input = update.message.text.strip()
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await update.message.reply_text("❌ ابتدا لاگین کنید.")
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
        await update.message.reply_text(f"✅ چت هدف تنظیم شد: `{chat_title}`")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {str(e)}\n\nنکته: اگر از آیدی عددی استفاده می‌کنید، مطمئن شوید قبلاً با آن کاربر چت داشته‌اید.")
    finally:
        await client.disconnect()
    return ConversationHandler.END

# ---------- تنظیم ریپلی ----------
async def set_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await query.edit_message_text("❌ ابتدا لاگین کنید.")
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton("🔗 با لینک پیام", callback_data="reply_link")],
        [InlineKeyboardButton("📋 انتخاب از چت‌ها", callback_data="reply_from_chat")],
        [InlineKeyboardButton("🔙 انصراف", callback_data="back_main")]
    ]
    await query.edit_message_text("🔁 **تنظیم ریپلی یکبار مصرف**\n\nلطفاً روش را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown')
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
        await query.edit_message_text("لطفاً لینک پیام مورد نظر را ارسال کنید:\n(مثال: https://t.me/username/123 یا https://t.me/c/123456789/100)\nبرای لغو /cancel")
        return REPLY_LINK_STATE
    elif data == "reply_from_chat":
        await query.edit_message_text("🔄 در حال دریافت لیست چت‌های اخیر...")
        dialogs, error = await get_last_dialogs(user_id)
        if error:
            await query.edit_message_text(f"❌ {error}")
            return ConversationHandler.END
        if not dialogs:
            await query.edit_message_text("⚠️ هیچ چتی پیدا نشد.")
            return ConversationHandler.END
        buttons = []
        for chat_id, title in dialogs:
            short_title = title[:40] + "..." if len(title) > 40 else title
            buttons.append([InlineKeyboardButton(f"📌 {short_title}", callback_data=f"reply_chat_{chat_id}")])
        buttons.append([InlineKeyboardButton("🔙 انصراف", callback_data="back_main")])
        await query.edit_message_text("🔁 مرحله 1: چت مورد نظر برای ریپلی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
        return REPLY_SELECT_CHAT_STATE
    else:
        await query.edit_message_text("❌ گزینه نامعتبر.")
        return ConversationHandler.END

async def reply_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = update.message.text.strip()
    chat_id, msg_id = await parse_message_link(link)
    if not chat_id or not msg_id:
        await update.message.reply_text("❌ لینک معتبر نیست. لطفاً لینک پیام تلگرام را بفرستید.")
        return REPLY_LINK_STATE
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await update.message.reply_text("❌ ابتدا لاگین کنید.")
        return ConversationHandler.END
    client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
    await client.connect()
    try:
        if isinstance(chat_id, str):
            entity = await get_entity_safe(client, chat_id)
            chat_id = entity.id
        await set_reply(user_id, chat_id, msg_id, active=True)
        await update.message.reply_text(f"✅ ریپلی با موفقیت تنظیم شد.\nچت: `{chat_id}`\nپیام ID: `{msg_id}`\n\nاکنون یک فایل صوتی یا ویدیویی بفرستید تا به عنوان ریپلی به آن پیام ارسال شود (فقط یکبار).")
        await main_menu(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {str(e)}")
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
        await query.edit_message_text(f"🔄 در حال دریافت پیام‌های چت...")
        messages, error = await get_last_messages(user_id, chat_id, limit=10)
        if error:
            await query.edit_message_text(f"❌ {error}")
            return ConversationHandler.END
        if not messages:
            await query.edit_message_text("⚠️ هیچ پیامی در این چت یافت نشد.")
            return ConversationHandler.END
        buttons = []
        for msg_id, short_text in messages:
            buttons.append([InlineKeyboardButton(f"📝 {short_text}", callback_data=f"reply_msg_{msg_id}")])
        buttons.append([InlineKeyboardButton("🔙 انصراف", callback_data="back_main")])
        await query.edit_message_text(f"🔁 مرحله 2: پیام مورد نظر برای ریپلی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
        return REPLY_SELECT_MSG_STATE
    else:
        await query.edit_message_text("❌ گزینه نامعتبر.")
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
            await query.edit_message_text("❌ خطا: چت انتخاب نشده.")
            return ConversationHandler.END
        await set_reply(user_id, chat_id, msg_id, active=True)
        await query.edit_message_text(f"✅ ریپلی با موفقیت تنظیم شد.\nچت: `{chat_id}`\nپیام ID: `{msg_id}`\n\nاکنون یک فایل صوتی یا ویدیویی بفرستید تا به عنوان ریپلی به آن پیام ارسال شود (فقط یکبار).")
        await asyncio.sleep(2)
        await main_menu(update, context)
        return ConversationHandler.END
    else:
        await query.edit_message_text("❌ گزینه نامعتبر.")
        return ConversationHandler.END

# ---------- وضعیت و خروج ----------
async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = await get_user_data(query.from_user.id)
    if not data or not data['session_string']:
        text = "❌ لاگین نیستید"
    else:
        text = f"✅ لاگین هستید\n🎯 چت هدف: `{data['target_chat_title'] or data['target_chat_id'] if data['target_chat_id'] else 'تنظیم نشده'}`\n🔁 ریپلی: {'فعال' if data['reply_active'] else 'غیرفعال'}\n📹 ویدیو کال: {'فعال' if data.get('auto_video_enabled') else 'غیرفعال'}"
    await query.edit_message_text(text, parse_mode='Markdown')

async def clear_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await clear_reply(user_id)
    await query.edit_message_text("✅ ریپلی غیرفعال شد.")
    await asyncio.sleep(1)
    await main_menu(update, context)

async def disable_auto_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PYTGCALLS_AVAILABLE:
        await update.callback_query.answer("ویدیو کال در این سرور فعال نیست.", show_alert=True)
        return
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
    await query.edit_message_text("✅ قابلیت ویدیو کال غیرفعال شد.")
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
    await query.edit_message_text("✅ از اکانت خارج شدید.")

# ---------- تنظیم ویدیو کال (فقط در صورت وجود pytgcalls) ----------
if PYTGCALLS_AVAILABLE:
    async def set_auto_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = await get_user_data(user_id)
        if not data or not data['session_string']:
            await query.edit_message_text("❌ ابتدا لاگین کنید.")
            return
        await query.edit_message_text("📹 لطفاً ویدیویی که می‌خواهید در تماس‌های ویدیویی پخش شود را ارسال کنید.\nویدیو باید کوتاه باشد (حداکثر 60 ثانیه).\nبرای لغو /cancel")

    async def handle_auto_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        status_msg = await update.message.reply_text("🔄 در حال پردازش ویدیو...")
        try:
            data = await get_user_data(user_id)
            if not data or not data['session_string']:
                await status_msg.edit_text("❌ لاگین نیستید.")
                return
            msg = update.message
            if not msg.video:
                await status_msg.edit_text("❌ لطفاً یک فایل ویدیویی ارسال کنید.")
                return
            duration = getattr(msg.video, 'duration', 0)
            if duration > 60:
                await status_msg.edit_text("❌ ویدیو نباید بیشتر از 60 ثانیه باشد.")
                return
            await status_msg.edit_text("📥 در حال دانلود ویدیو...")
            file_obj = await msg.video.get_file()
            file_path = str(await file_obj.download_to_drive())
            await status_msg.edit_text("🔄 در حال تبدیل ویدیو به مربع (بدون حاشیه)...")
            square_path = file_path + "_square.mp4"
            try:
                await convert_to_square_ffmpeg(file_path, square_path)
                final_file_path = square_path
            except Exception as e:
                await status_msg.edit_text(f"⚠️ خطا در تبدیل: {str(e)}. ارسال ویدیوی اصلی...")
                final_file_path = file_path
            await enable_auto_video(user_id, final_file_path)
            if user_id not in call_clients:
                call_client = await setup_pytgcalls(user_id, data['session_string'], data['api_id'], data['api_hash'])
                if call_client:
                    call_clients[user_id] = call_client
                    @call_client.on_call()
                    async def on_incoming_call(call: Call):
                        if call.chat_id == user_id:
                            vid_path = (await get_user_data(user_id)).get('auto_video_path')
                            if vid_path and os.path.exists(vid_path):
                                await answer_call(call.chat_id, call_client, vid_path)
            await status_msg.edit_text("✅ تنظیم ویدیو کال با موفقیت انجام شد.\nاز این به بعد هر تماس ویدیویی به شما، با این ویدیو پاسخ داده می‌شود و پس از اتمام قطع می‌گردد.")
            await main_menu(update, context)
        except Exception as e:
            await status_msg.edit_text(f"❌ خطا: {str(e)}")

# ---------- هندلر فایل اصلی ----------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("🔄 در حال پردازش...")
    try:
        data = await get_user_data(user_id)
        if not data or not data['session_string']:
            await status_msg.edit_text("❌ لاگین نیستید. از منو لاگین کنید.")
            return
        if data['reply_active'] and data['reply_chat_id'] and data['reply_msg_id']:
            target_chat_id = data['reply_chat_id']
            reply_to = data['reply_msg_id']
        else:
            if not data['target_chat_id']:
                await status_msg.edit_text("❌ چت هدف تنظیم نشده و ریپلی فعالی هم ندارید.")
                return
            target_chat_id = data['target_chat_id']
            reply_to = None
        msg = update.message
        is_audio = bool(msg.audio or msg.voice)
        is_video = bool(msg.video or msg.video_note)
        if not is_audio and not is_video:
            await status_msg.edit_text("❌ فقط فایل صوتی یا ویدیویی پشتیبانی می‌شود.")
            return
        duration = 3
        if is_audio:
            duration = getattr(msg.audio or msg.voice, 'duration', 3)
        else:
            duration = getattr(msg.video or msg.video_note, 'duration', 3)
        if not duration or duration <= 0:
            duration = 3
        await status_msg.edit_text("📥 در حال دانلود فایل...")
        if is_audio:
            file_obj = await (msg.audio or msg.voice).get_file()
        else:
            file_obj = await (msg.video or msg.video_note).get_file()
        file_path = str(await file_obj.download_to_drive())
        final_file_path = file_path
        if is_video:
            await status_msg.edit_text("🔄 در حال تبدیل ویدیو به مربع (بدون حاشیه)...")
            square_path = file_path + "_square.mp4"
            try:
                await convert_to_square_ffmpeg(file_path, square_path)
                final_file_path = square_path
            except Exception as e:
                await status_msg.edit_text(f"⚠️ خطا در تبدیل: {str(e)}. ارسال ویدیوی اصلی...")
                final_file_path = file_path
        client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
        await client.connect()
        if not await ensure_session_active(client):
            await status_msg.edit_text("❌ نشست منقضی شده. لطفاً دوباره لاگین کنید.")
            await client.disconnect()
            return
        target_entity = await get_input_entity_safe(client, target_chat_id)
        await status_msg.edit_text(f"🎬 {'در حال ارسال ویس' if is_audio else 'در حال ارسال ویدیو (دایره‌ای)'}...")
        if is_audio:
            await send_as_voice_note(client, target_entity, final_file_path, duration, reply_to=reply_to)
        else:
            await send_as_video_note(client, target_entity, final_file_path, duration, reply_to=reply_to)
        if reply_to:
            await status_msg.edit_text(f"✅ {'ویس' if is_audio else 'ویدیو'} نوت به عنوان ریپلی ارسال شد.")
            await clear_reply(user_id)
        else:
            await status_msg.edit_text(f"✅ {'ویس' if is_audio else 'ویدیو'} نوت ارسال شد.")
        await client.disconnect()
        if os.path.exists(file_path):
            os.remove(file_path)
        if is_video and os.path.exists(final_file_path) and final_file_path != file_path:
            os.remove(final_file_path)
    except FloodWaitError as e:
        await status_msg.edit_text(f"⏳ محدودیت تلگرام: {e.seconds} ثانیه صبر کنید")
    except Exception as e:
        await status_msg.edit_text(f"❌ خطا: {str(e)}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await main_menu(update, context)

# ---------- وب سرور ----------
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

# ---------- main ----------
async def main():
    await init_db()
    if PYTGCALLS_AVAILABLE:
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
    if PYTGCALLS_AVAILABLE:
        application.add_handler(ConversationHandler(
            entry_points=[CallbackQueryHandler(set_auto_video_start, pattern='^set_auto_video$')],
            states={},
            fallbacks=[CommandHandler('cancel', cancel)]
        ))
        application.add_handler(MessageHandler(filters.VIDEO, handle_auto_video_file))
    application.add_handler(CallbackQueryHandler(status_callback, pattern='^status$'))
    application.add_handler(CallbackQueryHandler(clear_reply_callback, pattern='^clear_reply$'))
    if PYTGCALLS_AVAILABLE:
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
