import asyncio
import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import (
    SendMessageRecordAudioAction, SendMessageUploadAudioAction,
    SendMessageRecordVideoAction, SendMessageUploadVideoAction
)
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from aiohttp import web

BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise Exception("BOT_TOKEN not set")

API_ID_STATE, API_HASH_STATE, PHONE_STATE, CODE_STATE, PASSWORD_STATE, TARGET_CHAT_STATE = range(6)

DB_PATH = "user_data.db"

def init_db_sync():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_data (
            user_id INTEGER PRIMARY KEY,
            api_id INTEGER,
            api_hash TEXT,
            session_string TEXT,
            target_chat_id INTEGER,
            target_chat_title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

async def init_db():
    await asyncio.to_thread(init_db_sync)

async def get_user_data(user_id):
    def _get():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute('SELECT api_id, api_hash, session_string, target_chat_id, target_chat_title FROM user_data WHERE user_id = ?', (user_id,))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_get)

async def save_user_data(user_id, api_id=None, api_hash=None, session_string=None, target_chat_id=None, target_chat_title=None):
    def _save():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM user_data WHERE user_id = ?', (user_id,))
        exists = cur.fetchone()
        if exists:
            cur.execute('''
                UPDATE user_data SET
                    api_id = COALESCE(?, api_id),
                    api_hash = COALESCE(?, api_hash),
                    session_string = COALESCE(?, session_string),
                    target_chat_id = COALESCE(?, target_chat_id),
                    target_chat_title = COALESCE(?, target_chat_title)
                WHERE user_id = ?
            ''', (api_id, api_hash, session_string, target_chat_id, target_chat_title, user_id))
        else:
            cur.execute('''
                INSERT INTO user_data (user_id, api_id, api_hash, session_string, target_chat_id, target_chat_title)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, api_id, api_hash, session_string, target_chat_id, target_chat_title))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_save)

async def update_target_chat(user_id, chat_id, chat_title):
    await save_user_data(user_id, target_chat_id=chat_id, target_chat_title=chat_title)

# ========== توابع Telethon ==========
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

async def send_as_voice_note(client, chat, file_path, duration):
    await send_action_with_duration(client, chat, 'voice', duration)
    await client.send_file(chat, file_path, voice_note=True)

async def send_as_video_note(client, chat, file_path, duration):
    await send_action_with_duration(client, chat, 'video', duration)
    await client.send_file(chat, file_path, video_note=True)

# ========== گرفتن ۱۰ چت آخر کاربر ==========
async def get_last_dialogs(user_id):
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        return None
    session_string = data['session_string']
    api_id = data['api_id']
    api_hash = data['api_hash']
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        dialogs = await client.get_dialogs(limit=10)
        result = []
        for d in dialogs:
            if d.is_user:
                title = d.name or (d.entity.first_name if d.entity else str(d.id))
            else:
                title = d.title or str(d.id)
            result.append((d.id, title))
        return result
    except Exception as e:
        print(f"Error getting dialogs: {e}")
        return []
    finally:
        await client.disconnect()

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
    else:
        text += "❌ لاگین نیستید\n"
    buttons = [
        [InlineKeyboardButton("🔐 لاگین", callback_data="login")],
        [InlineKeyboardButton("🎯 تنظیم چت هدف", callback_data="set_target")],
        [InlineKeyboardButton("📋 وضعیت", callback_data="status")],
        [InlineKeyboardButton("🚪 خروج", callback_data="logout")]
    ]
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

# ---------- لاگین ----------
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("api_id را وارد کنید:")
    return API_ID_STATE

async def login_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        api_id = int(update.message.text.strip())
        context.user_data['api_id'] = api_id
        await update.message.reply_text("api_hash را وارد کنید:")
        return API_HASH_STATE
    except ValueError:
        await update.message.reply_text("api_id باید عدد باشد. دوباره:")
        return API_ID_STATE

async def login_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_hash = update.message.text.strip()
    context.user_data['api_hash'] = api_hash
    await update.message.reply_text("شماره تلفن (با کد کشور):")
    return PHONE_STATE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    api_id = context.user_data['api_id']
    api_hash = context.user_data['api_hash']
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        await client.send_code_request(phone)
        context.user_data['temp_client'] = client
        await update.message.reply_text("کد تأیید +1 را وارد کنید (مثلاً اگر کد 12345 است، 12346 را بفرستید):")
        return CODE_STATE
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")
        return ConversationHandler.END

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_code_plus_one = int(update.message.text.strip())
        real_code = user_code_plus_one - 1
        if real_code < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("عدد معتبر وارد کنید:")
        return CODE_STATE
    client = context.user_data.get('temp_client')
    phone = context.user_data.get('phone')
    if not client:
        await update.message.reply_text("نشست منقضی")
        return ConversationHandler.END
    try:
        await client.sign_in(phone, str(real_code))
        session_string = client.session.save()
        user_id = update.effective_user.id
        await save_user_data(user_id,
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
        user_id = update.effective_user.id
        await save_user_data(user_id,
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
    user_id = query.from_user.id

    await query.edit_message_text("🔄 در حال دریافت لیست چت‌های اخیر...")

    dialogs = await get_last_dialogs(user_id)
    if dialogs is None:
        await query.edit_message_text("❌ ابتدا لاگین کنید.")
        return ConversationHandler.END
    if not dialogs:
        await query.edit_message_text("⚠️ هیچ چتی پیدا نشد. شاید نشست شما منقضی شده است. لطفاً مجدداً لاگین کنید.")
        return ConversationHandler.END

    buttons = []
    for chat_id, title in dialogs:
        short_title = title[:30] + "..." if len(title) > 30 else title
        callback_data = f"select_chat|{chat_id}|{title[:50]}"
        buttons.append([InlineKeyboardButton(f"📌 {short_title}", callback_data=callback_data)])
    buttons.append([InlineKeyboardButton("✏️ ورود دستی", callback_data="manual_input")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_main")])

    await query.edit_message_text(
        "🎯 **انتخاب چت هدف**\n\n"
        "یکی از چت‌های اخیر خود را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode='Markdown'
    )
    return TARGET_CHAT_STATE

async def set_target_chat_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "manual_input":
        await query.edit_message_text(
            "لطفاً **یوزرنیم** (مثل @username) یا **آیدی عددی** چت مورد نظر را وارد کنید.\n"
            "برای لغو: /cancel"
        )
        return TARGET_CHAT_STATE
    elif data == "back_main":
        await main_menu(update, context)
        return ConversationHandler.END
    elif data.startswith("select_chat|"):
        parts = data.split("|", 2)
        if len(parts) < 2:
            await query.edit_message_text("❌ خطا در شناسایی چت (فرمت داده نامعتبر).")
            return ConversationHandler.END
        try:
            chat_id = int(parts[1])
        except ValueError:
            await query.edit_message_text("❌ خطا در شناسایی چت: آیدی عددی معتبر نیست.")
            return ConversationHandler.END
        chat_title = parts[2] if len(parts) > 2 else str(chat_id)
        
        await update_target_chat(user_id, chat_id, chat_title)
        await query.edit_message_text(f"✅ چت هدف تنظیم شد: `{chat_title}` (ID: `{chat_id}`)")
        await asyncio.sleep(2)
        await main_menu(update, context)
        return ConversationHandler.END

    await query.edit_message_text("❌ دستور نامعتبر.")
    return ConversationHandler.END

async def set_target_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_input = update.message.text.strip()
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await update.message.reply_text("❌ شما لاگین نیستید. ابتدا لاگین کنید /start")
        return ConversationHandler.END
    session_string = data['session_string']
    api_id = data['api_id']
    api_hash = data['api_hash']
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        if chat_input.lstrip('-').isdigit():
            entity = await client.get_entity(int(chat_input))
        else:
            entity = await client.get_entity(chat_input)
        chat_id = entity.id
        chat_title = getattr(entity, 'title', None) or entity.first_name or str(entity.id)
        await update_target_chat(user_id, chat_id, chat_title)
        await update.message.reply_text(f"✅ چت هدف تنظیم شد: `{chat_title}` (ID: {chat_id})")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در یافتن چت: {str(e)}")
    finally:
        await client.disconnect()
    return ConversationHandler.END

# ---------- وضعیت و خروج ----------
async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        text = "❌ لاگین نیستید"
    else:
        text = f"✅ لاگین هستید\n"
        if data['target_chat_id']:
            text += f"🎯 چت هدف: `{data['target_chat_title'] or data['target_chat_id']}` (ID: {data['target_chat_id']})"
        else:
            text += "❌ چت هدف تنظیم نشده"
    await query.edit_message_text(text, parse_mode='Markdown')

async def logout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await save_user_data(user_id, session_string="")
    await query.edit_message_text("✅ از اکانت خود خارج شدید.")

# ========== هندلر فایل با پیام وضعیت ==========
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("🔄 در حال بررسی درخواست...")
    try:
        data = await get_user_data(user_id)
        if not data or not data['session_string']:
            await status_msg.edit_text("❌ شما لاگین نیستید. لطفاً از منوی اصلی لاگین کنید.")
            return
        if not data['target_chat_id']:
            await status_msg.edit_text("❌ چت هدف تنظیم نشده. ابتدا از منوی اصلی چت هدف را انتخاب کنید.")
            return
        
        session_string = data['session_string']
        api_id = data['api_id']
        api_hash = data['api_hash']
        target_chat_id = data['target_chat_id']
        
        file = update.message.effective_attachment
        if not file:
            await status_msg.edit_text("❌ فایلی یافت نشد.")
            return
        
        # استخراج مدت زمان
        duration = getattr(file, 'duration', None)
        if not duration and hasattr(file, 'document') and hasattr(file.document, 'attributes'):
            for attr in file.document.attributes:
                if hasattr(attr, 'duration'):
                    duration = attr.duration
                    break
        if not duration:
            duration = 3
        
        # تشخیص نوع فایل
        mime_type = getattr(file, 'mime_type', '')
        is_audio = mime_type.startswith('audio/') or isinstance(file, (filters.AUDIO, filters.VOICE)) or 'Audio' in str(type(file))
        is_video = mime_type.startswith('video/') or isinstance(file, filters.VIDEO) or 'Video' in str(type(file))
        
        if not is_audio and not is_video:
            await status_msg.edit_text("❌ فرمت فایل پشتیبانی نمی‌شود. فقط فایل‌های صوتی و ویدیویی.")
            return
        
        await status_msg.edit_text("📥 در حال دانلود فایل...")
        file_path = await file.get_file().download_to_drive()
        
        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await client.connect()
        try:
            target_entity = await client.get_entity(target_chat_id)
            await status_msg.edit_text(f"🎬 {'در حال ارسال ویس' if is_audio else 'در حال ارسال ویدیو'} با اکشن ضبط...")
            if is_audio:
                await send_as_voice_note(client, target_entity, file_path, duration)
                await status_msg.edit_text(f"✅ ویس نوت با موفقیت ارسال شد (مدت {duration} ثانیه)")
            else:
                await send_as_video_note(client, target_entity, file_path, duration)
                await status_msg.edit_text(f"✅ ویدیو نوت با موفقیت ارسال شد (مدت {duration} ثانیه)")
        except FloodWaitError as e:
            await status_msg.edit_text(f"⏳ محدودیت تلگرام: {e.seconds} ثانیه صبر کنید.")
        except Exception as e:
            await status_msg.edit_text(f"❌ خطا در ارسال: {str(e)}")
        finally:
            await client.disconnect()
            if os.path.exists(file_path):
                os.remove(file_path)
    except Exception as e:
        await status_msg.edit_text(f"❌ خطای غیرمنتظره: {str(e)}")

# ---------- دستورات عمومی ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await main_menu(update, context)

# ========== وب سرور ==========
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
    application = Application.builder().token(BOT_TOKEN).build()
    # لاگین
    login_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(login_start, pattern='^login$')],
        states={
            API_ID_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_api_id)],
            API_HASH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_api_hash)],
            PHONE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            CODE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_code)],
            PASSWORD_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(login_conv)
    # تنظیم چت هدف
    target_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_target_start, pattern='^set_target$')],
        states={
            TARGET_CHAT_STATE: [
                CallbackQueryHandler(set_target_chat_button, pattern='^(select_chat\\||manual_input|back_main)'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_target_manual)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(target_conv)
    application.add_handler(CallbackQueryHandler(status_callback, pattern='^status$'))
    application.add_handler(CallbackQueryHandler(logout_callback, pattern='^logout$'))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.AUDIO | filters.VIDEO | filters.VOICE | filters.VIDEO_NOTE, handle_file))
    
    await application.initialize()
    await application.start()
    asyncio.create_task(application.updater.start_polling())
    await run_web()

if __name__ == '__main__':
    asyncio.run(main())
