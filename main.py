import asyncio
import base64
import inspect
import os
import re
import struct
import subprocess
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SetTypingRequest, GetHistoryRequest
from telethon.tl.types import (
    SendMessageRecordAudioAction, SendMessageUploadAudioAction,
    SendMessageRecordVideoAction, SendMessageUploadVideoAction,
    Message, UpdatePhoneCall, PhoneCallRequested, PhoneCallAccepted,
    PhoneCallWaiting, PhoneCallDiscarded
)
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from aiohttp import web
try:
    import pyrogram.errors as pyrogram_errors

    # Compatibility fix for some PyTgCalls + Pyrogram combinations.
    # Newer/forked Pyrogram builds may not expose GroupcallForbidden, while
    # some PyTgCalls versions import it directly during startup.
    if not hasattr(pyrogram_errors, "GroupcallForbidden"):
        pyrogram_errors.GroupcallForbidden = getattr(
            pyrogram_errors,
            "BadRequest",
            getattr(pyrogram_errors, "RPCError", Exception)
        )

    if not hasattr(pyrogram_errors, "GroupCallForbidden"):
        pyrogram_errors.GroupCallForbidden = pyrogram_errors.GroupcallForbidden
except Exception:
    # v5 uses Telethon as the MTProto backend. Pyrogram is only patched here
    # when it exists because some PyTgCalls builds import the Pyrogram bridge
    # during module loading.
    pass

try:
    from pyrogram.storage.storage import SESSION_STRING_FORMAT as PYROGRAM_SESSION_STRING_FORMAT
except Exception:
    try:
        from pyrogram.storage import Storage
        PYROGRAM_SESSION_STRING_FORMAT = getattr(Storage, "SESSION_STRING_FORMAT", ">BI?256sQ?")
    except Exception:
        PYROGRAM_SESSION_STRING_FORMAT = ">BI?256sQ?"

from pytgcalls import PyTgCalls
try:
    from pytgcalls.types import Call
except Exception:
    Call = None
try:
    from pytgcalls.types import MediaStream
except Exception:
    MediaStream = None
try:
    from pytgcalls.types import RawCallUpdate
except Exception:
    RawCallUpdate = None
try:
    from pytgcalls.types import ChatUpdate
except Exception:
    ChatUpdate = None
try:
    from pytgcalls.types import CallConfig
except Exception:
    CallConfig = None

BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
if not BOT_TOKEN or not DATABASE_URL or not API_ID or not API_HASH:
    raise Exception("BOT_TOKEN, DATABASE_URL, API_ID, API_HASH are required")

AUTO_VIDEO_CACHE_DIR = Path(__file__).with_name("auto_video_cache")
AUTO_VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_PATH = "ffmpeg"
FFPROBE_PATH = "ffprobe"

API_ID_STATE, API_HASH_STATE, PHONE_STATE, CODE_STATE, PASSWORD_STATE, TARGET_CHAT_STATE = range(6)
REPLY_METHOD_STATE, REPLY_SELECT_CHAT_STATE, REPLY_SELECT_MSG_STATE, REPLY_LINK_STATE = range(6, 10)
AUTO_VIDEO_STATE = 10

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
            if 'auto_video_bytes' not in columns:
                await conn.execute("ALTER TABLE user_data ADD COLUMN auto_video_bytes BYTEA DEFAULT NULL")
            if 'auto_video_filename' not in columns:
                await conn.execute("ALTER TABLE user_data ADD COLUMN auto_video_filename TEXT DEFAULT NULL")
        await conn.commit()

async def get_user_data(user_id):
    async with await get_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT * FROM user_data WHERE user_id = %s', (user_id,))
            return await cur.fetchone()


def _auto_video_cache_path(user_id, filename=None):
    suffix = Path(filename).suffix if filename else ".mp4"
    if not suffix:
        suffix = ".mp4"
    return str(AUTO_VIDEO_CACHE_DIR / f"auto_video_{int(user_id)}{suffix}")


async def resolve_auto_video_path(user_id):
    data = await get_user_data(user_id)
    if not data or not data.get("auto_video_enabled"):
        return None

    path = data.get("auto_video_path")
    if path and os.path.exists(path):
        return str(path)

    blob = data.get("auto_video_bytes")
    if not blob:
        return None

    filename = data.get("auto_video_filename") or "auto_video.mp4"
    path = _auto_video_cache_path(user_id, filename)
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(bytes(blob))
        await save_user_data(user_id, auto_video_path=path)
        return path
    except Exception as e:
        print(f"Could not materialize auto video for user {user_id}: {e}", flush=True)
        return None


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

# ========== ویدیو کال ==========
call_clients = {}
call_mtproto_clients = {}
active_call_answers = set()

async def enable_auto_video(user_id, video_path, video_bytes, video_filename=None):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE user_data
                SET auto_video_enabled = TRUE,
                    auto_video_path = %s,
                    auto_video_bytes = %s,
                    auto_video_filename = %s
                WHERE user_id = %s
                """,
                (video_path, video_bytes, video_filename, user_id),
            )
            await conn.commit()

async def disable_auto_video(user_id):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                'UPDATE user_data SET auto_video_enabled = FALSE, auto_video_path = NULL, auto_video_bytes = NULL, auto_video_filename = NULL WHERE user_id = %s',
                (user_id,),
            )
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
    """Start a persistent user MTProto client for incoming call updates.

    v5 uses Telethon directly for PyTgCalls instead of converting the Telethon
    session to Pyrogram. This is more reliable for this project because login
    already creates a Telethon StringSession, and PyTgCalls supports Telethon
    MTProto clients.
    """
    telethon_client = TelegramClient(
        StringSession(session_string),
        int(api_id),
        api_hash,
        connection_retries=None,
        auto_reconnect=True,
    )
    await telethon_client.connect()

    me = await telethon_client.get_me()
    if not me:
        await telethon_client.disconnect()
        raise Exception("Telethon session is not authorized. Please login again.")

    print(f"Telethon user client connected for owner {user_id}; logged in as {me.id}", flush=True)

    import pytgcalls
    import inspect
    print("PYTGCALLS FILE:", inspect.getfile(pytgcalls), flush=True)
    print("PYTGCALLS VERSION:", getattr(pytgcalls, "__version__", "unknown"), flush=True)
    print("BEFORE PyTgCalls()", flush=True)
    try:
        call_client = PyTgCalls(telethon_client)
        print("AFTER PyTgCalls()", flush=True)
    except Exception:
        import traceback
        print("===== PYTGCALLS TRACEBACK =====", flush=True)
        print(traceback.format_exc(), flush=True)
        raise

    async def raw_phone_update_logger(update):
        try:
            update_name = update.__class__.__name__
            if "PhoneCall" in update_name:
                print(f"TELETHON RAW PHONE UPDATE for owner {user_id}: {update!r}", flush=True)

            if isinstance(update, UpdatePhoneCall):
                phone_call = getattr(update, "phone_call", None)
                print(
                    f"TELETHON UpdatePhoneCall for owner {user_id}: "
                    f"{phone_call.__class__.__name__ if phone_call else None} {phone_call!r}",
                    flush=True,
                )

                if isinstance(phone_call, PhoneCallRequested):
                    caller_id = getattr(phone_call, "admin_id", None)
                    print(
                        f"Incoming PhoneCallRequested detected for owner {user_id}; caller={caller_id}. "
                        f"Waiting for PyTgCalls INCOMING_CALL event to answer.",
                        flush=True,
                    )
        except Exception as e:
            print(f"Telethon raw phone update logger error for owner {user_id}: {e}", flush=True)

    # Register our raw logger after PyTgCalls is created. PyTgCalls registers
    # its own raw handler during construction; registering after it lets the
    # internal phone-call cache be filled before we call play().
    telethon_client.add_event_handler(raw_phone_update_logger, events.Raw)

    print("BEFORE call_client.start()", flush=True)
    await maybe_await(call_client.start())
    print("AFTER call_client.start()", flush=True)

    async def raw_incoming_call_answerer(event):
        try:
            raw_update = getattr(event, "update", event)
            if not isinstance(raw_update, UpdatePhoneCall):
                return
            phone_call = getattr(raw_update, "phone_call", None)
            if not isinstance(phone_call, PhoneCallRequested):
                return
            caller_id = getattr(phone_call, "admin_id", None)
            if caller_id is None:
                return
            print(
                f"Telethon raw incoming call detected for owner {user_id}; caller={caller_id}",
                flush=True,
            )
            await answer_incoming_private_call(user_id, int(caller_id), "telethon_raw")
        except Exception as e:
            print(f"Telethon raw incoming call handler error for owner {user_id}: {e}", flush=True)

    telethon_client.add_event_handler(raw_incoming_call_answerer, events.Raw)

    call_mtproto_clients[user_id] = telethon_client
    print(f"PyTgCalls started with Telethon backend for owner {user_id}", flush=True)
    return call_client

async def get_video_duration(video_path):
    """گرفتن مدت زمان ویدیو با ffprobe"""
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

async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value

async def play_media_in_call(call_client, chat_id, video_path, *, incoming_private_call=False):
    """Play media in a call using one attempt for incoming private calls.

    For incoming private calls, PyTgCalls creates an internal p2p config after
    RawCallUpdate(Type.REQUESTED), then play(user_id, stream, CallConfig()) must
    be called once. Retrying with a second stream can consume/drop that internal
    state and make PyTgCalls start an outgoing RequestCallRequest instead.
    """
    config = None
    if incoming_private_call and CallConfig is not None:
        try:
            config = CallConfig()
        except Exception as e:
            print(f"Could not create CallConfig, continuing without it: {e}", flush=True)
            config = None

    async def call_play(stream_value):
        try:
            if config is not None:
                return await maybe_await(call_client.play(chat_id, stream_value, config))
            return await maybe_await(call_client.play(chat_id, stream_value))
        except TypeError:
            if config is not None:
                return await maybe_await(call_client.play(chat_id, stream_value, config=config))
            raise

    # For incoming private calls, do NOT retry. Use raw path first because the
    # current py-tgcalls examples accept direct file/URL paths and one failed
    # p2p attempt can invalidate the stored incoming-call state.
    if incoming_private_call:
        print(f"Calling PyTgCalls.play once for incoming private call: chat_id={chat_id}, path={video_path}", flush=True)
        return await call_play(video_path)

    last_error = None
    if MediaStream is not None:
        try:
            return await call_play(MediaStream(video_path))
        except Exception as e:
            last_error = e
            print(f"MediaStream play failed for non-private call, retrying with raw path: {repr(e)}", flush=True)

    try:
        return await call_play(video_path)
    except Exception as e:
        if last_error:
            raise Exception(f"play failed with MediaStream ({repr(last_error)}) and raw path ({repr(e)})")
        raise

async def leave_call_safe(call_client, chat_id):
    for method_name in ("leave_call", "stop", "close"):
        method = getattr(call_client, method_name, None)
        if method:
            try:
                if method_name == "stop":
                    return await maybe_await(method())
                return await maybe_await(method(chat_id))
            except TypeError:
                try:
                    return await maybe_await(method())
                except Exception:
                    pass
            except Exception:
                pass

def get_update_chat_id(update):
    for attr in ("chat_id", "user_id", "peer_id"):
        value = getattr(update, attr, None)
        if value is not None:
            return value

    nested_attrs = ("call", "phone_call", "update", "raw_update")
    for parent_attr in nested_attrs:
        parent = getattr(update, parent_attr, None)
        if parent is None:
            continue
        for attr in ("chat_id", "user_id", "peer_id", "participant_id", "admin_id"):
            value = getattr(parent, attr, None)
            if value is not None:
                return value

    return None

def is_incoming_private_call_update(update):
    """Return True only for incoming private-call request updates.

    New py-tgcalls sends RawCallUpdate(Type.REQUESTED) when a private call is
    ringing. The previous v3 code treated generic call-like updates as a call
    to answer and then tried answer_call(); current py-tgcalls expects play().
    """
    if update is None:
        return False


    if ChatUpdate is not None:
        try:
            if isinstance(update, ChatUpdate):
                status = getattr(update, "status", None)
                incoming = getattr(getattr(ChatUpdate, "Status", None), "INCOMING_CALL", None)
                if incoming is not None:
                    try:
                        return bool(status & incoming)
                    except Exception:
                        return status == incoming
        except TypeError:
            pass

    if RawCallUpdate is not None:
        try:
            if isinstance(update, RawCallUpdate):
                status = getattr(update, "status", None)
                requested = getattr(getattr(RawCallUpdate, "Type", None), "REQUESTED", None)
                if requested is None:
                    print(f"RawCallUpdate received without REQUESTED enum info: {update}")
                    return True
                try:
                    return bool(status & requested)
                except Exception:
                    return status == requested
        except TypeError:
            pass

    name = update.__class__.__name__.lower()
    if "rawcallupdate" in name or ("call" in name and "group" not in name and "participant" not in name):
        status = getattr(update, "status", None)
        status_name = str(status).lower()
        if "request" in status_name or "requested" in status_name:
            return True
        # Fallback for versions where status is an int flag and class name is already private-call-specific.
        if status is not None and "rawcallupdate" in name:
            return True

    for attr in ("call", "phone_call"):
        value = getattr(update, attr, None)
        if value is not None:
            value_name = value.__class__.__name__.lower()
            if "requested" in value_name or ("call" in value_name and "group" not in value_name):
                return True

    return False

def register_incoming_call_handler(call_client, user_id):
    async def on_incoming_call(*args):
        update = args[-1] if args else None
        print(f"PyTgCalls update received for user {user_id}: {update!r}", flush=True)

        if not is_incoming_private_call_update(update):
            return

        chat_id = get_update_chat_id(update)
        if chat_id is None:
            print(f"Incoming private call update without user_id/chat_id: {update!r}", flush=True)
            return

        await answer_incoming_private_call(user_id, int(chat_id), "pytgcalls_update")

    if hasattr(call_client, "on_call"):
        call_client.on_call()(on_incoming_call)
        print(f"Registered incoming call handler via on_call for user {user_id}", flush=True)
        return

    if hasattr(call_client, "on_update"):
        call_client.on_update()(on_incoming_call)
        print(f"Registered incoming call handler via on_update for user {user_id}", flush=True)
        return

    raise Exception(
        "نسخه PyTgCalls نصب‌شده نه on_call دارد و نه on_update. "
        "پکیج py-tgcalls را آپدیت کنید."
    )

async def answer_incoming_private_call(user_id, caller_id, source="unknown"):
    key = (int(user_id), int(caller_id))
    if key in active_call_answers:
        print(f"Duplicate incoming-call answer ignored from {source}: owner={user_id}, caller={caller_id}", flush=True)
        return

    active_call_answers.add(key)
    try:
        call_client = call_clients.get(user_id)

        if not call_client:
            print(f"Incoming call from {caller_id}, but PyTgCalls client is missing for owner {user_id}", flush=True)
            return

        vid_path = await resolve_auto_video_path(user_id)
        if not vid_path or not os.path.exists(vid_path):
            print(f"Incoming call from {caller_id}, but video is unavailable for owner {user_id}", flush=True)
            return

        print(f"Answering incoming private call from {caller_id} for owner {user_id} using {source}: {vid_path}", flush=True)
        await answer_call(caller_id, call_client, vid_path)
    except Exception as e:
        print(f"Error in answer_incoming_private_call owner={user_id}, caller={caller_id}, source={source}: {e}", flush=True)
    finally:
        active_call_answers.discard(key)

async def answer_call(chat_id, call_client, video_path):
    try:
        # In current py-tgcalls, private incoming calls are accepted by play(user_id, stream)
        # after RawCallUpdate(Type.REQUESTED). There is no separate answer_call() method in
        # the main API, and calling a non-existent answer_call was why v3 looked enabled but
        # did not actually answer the ringing call.
        try:
            p2p = getattr(call_client, '_p2p_configs', {})
            p2p_data = p2p.get(chat_id) if isinstance(p2p, dict) else None
            print(
                f"Before play: chat_id={chat_id}, has_p2p_config={p2p_data is not None}, "
                f"outgoing={getattr(p2p_data, 'outgoing', None)}",
                flush=True,
            )
        except Exception as debug_e:
            print(f"Could not inspect p2p config before play: {debug_e}", flush=True)

        await play_media_in_call(call_client, chat_id, video_path, incoming_private_call=True)

        duration = await get_video_duration(video_path)
        await asyncio.sleep(min(duration, 65))
        await leave_call_safe(call_client, chat_id)
    except Exception as e:
        print(f"Error answering call for {chat_id}: {e}")

async def restore_auto_video_calls():
    async with await get_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT user_id, api_id, api_hash, session_string, auto_video_enabled FROM user_data WHERE auto_video_enabled = TRUE AND session_string IS NOT NULL')
            active_users = await cur.fetchall()
            for user in active_users:
                user_id = user['user_id']
                api_id_val = user['api_id']
                api_hash_val = user['api_hash']
                session_str = user['session_string']

                video_path = await resolve_auto_video_path(user_id)
                if not video_path or not os.path.exists(video_path):
                    print(f"Skipping restore for owner {user_id}: auto video missing", flush=True)
                    continue

                call_client = await setup_pytgcalls(user_id, session_str, api_id_val, api_hash_val)
                if call_client:
                    call_clients[user_id] = call_client
                    register_incoming_call_handler(call_client, user_id)
                    print(f"Restored auto-video call receiver for owner {user_id}", flush=True)

# ========== تبدیل ویدیو به مربع با استفاده از asyncio.create_subprocess_exec ==========
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

    # The comma inside min(iw,ih) must be escaped for FFmpeg's filter parser.
    # Without escaping, FFmpeg 7 may split the expression as a new filter and exit with code 8.
    video_filter = f'crop=min(iw\\,ih):min(iw\\,ih),scale={target_size}:{target_size},setsar=1,fps=30'

    cmd = [
        FFMPEG_PATH,
        '-hide_banner',
        '-y',
        '-i', input_abs,
        '-map', '0:v:0',
        '-map', '0:a?',
        '-vf', video_filter,
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-crf', '23',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-movflags', '+faststart',
        '-shortest',
        output_abs
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

async def detect_content_crop_ffmpeg(input_path, sample_seconds=6):
    """Detect and remove baked-in black bars before making a video note.

    Returns an FFmpeg crop expression such as crop=720:720:0:280, or None.
    This is only a best-effort helper; the final video-note conversion still
    uses scale+crop=increase, so it never pads with black borders.
    """
    input_abs = os.path.abspath(input_path)
    cmd = [
        FFMPEG_PATH,
        '-hide_banner',
        '-ss', '0',
        '-t', str(sample_seconds),
        '-i', input_abs,
        '-vf', 'cropdetect=limit=24:round=2:reset=1',
        '-an',
        '-f', 'null',
        '-'
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        text = stderr.decode('utf-8', errors='ignore')
        crops = re.findall(r'crop=(\d+:\d+:\d+:\d+)', text)
        if not crops:
            return None

        # Use the last stable crop detected by FFmpeg. Earlier frames can be
        # blank/fading and may produce inaccurate crop values.
        crop_value = crops[-1]
        w, h, x, y = [int(part) for part in crop_value.split(':')]
        if w <= 0 or h <= 0:
            return None
        return f'crop={w}:{h}:{x}:{y}'
    except Exception as e:
        print(f"cropdetect failed, continuing without black-bar pre-crop: {e}", flush=True)
        return None

async def convert_to_video_note_ffmpeg(input_path, output_path, target_size=480):
    """Convert any video to a clean Telegram round video note.

    Important details:
    - No padding is used, so FFmpeg never creates black borders.
    - scale=...:force_original_aspect_ratio=increase fills the square.
    - crop=target:target cuts the overflow from the center.
    - cropdetect is used first to remove black bars already baked into the file.
    """
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

    pre_crop = await detect_content_crop_ffmpeg(input_abs)
    filters = []
    if pre_crop:
        filters.append(pre_crop)

    # This is the key part for video notes: fill the whole square, then crop.
    # Do not use pad here; padding is what creates black side bars.
    filters.extend([
        f'scale={target_size}:{target_size}:force_original_aspect_ratio=increase',
        f'crop={target_size}:{target_size}',
        'setsar=1',
        'fps=30'
    ])
    video_filter = ','.join(filters)

    cmd = [
        FFMPEG_PATH,
        '-hide_banner',
        '-y',
        '-i', input_abs,
        '-map', '0:v:0',
        '-map', '0:a?',
        '-vf', video_filter,
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-crf', '23',
        '-pix_fmt', 'yuv420p',
        '-profile:v', 'baseline',
        '-level', '3.1',
        '-c:a', 'aac',
        '-b:a', '96k',
        '-ac', '2',
        '-ar', '44100',
        '-movflags', '+faststart',
        '-shortest',
        output_abs
    ]

    print(f"Running video-note ffmpeg command: {' '.join(cmd)}", flush=True)
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error_text = stderr.decode('utf-8', errors='ignore')
        raise Exception(f"ffmpeg video-note error (code {process.returncode}): {error_text[:800]}")

    if not os.path.exists(output_abs):
        raise Exception("Video-note output file was not created.")

    return output_abs

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
        [InlineKeyboardButton("📹 تنظیم ویدیو کال", callback_data="set_auto_video")],
        [InlineKeyboardButton("🚫 لغو ویدیو کال", callback_data="disable_auto_video")],
        [InlineKeyboardButton("📋 وضعیت", callback_data="status")],
        [InlineKeyboardButton("🚪 خروج", callback_data="logout")]
    ]
    markup = InlineKeyboardMarkup(buttons)
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, parse_mode='Markdown', reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=markup)

# ---------- لاگین ----------
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
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await disable_auto_video(user_id)
    if user_id in call_clients:
        try:
            await maybe_await(call_clients[user_id].stop())
            del call_clients[user_id]
        except:
            pass
    if user_id in call_mtproto_clients:
        try:
            await call_mtproto_clients[user_id].disconnect()
            del call_mtproto_clients[user_id]
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
            await maybe_await(call_clients[user_id].stop())
            del call_clients[user_id]
        except:
            pass
    if user_id in call_mtproto_clients:
        try:
            await call_mtproto_clients[user_id].disconnect()
            del call_mtproto_clients[user_id]
        except:
            pass
    await query.edit_message_text("✅ از اکانت خارج شدید.")

# ---------- تنظیم ویدیو کال ----------
async def set_auto_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await query.edit_message_text("❌ ابتدا لاگین کنید.")
        return ConversationHandler.END
    await query.edit_message_text("📹 لطفاً ویدیویی که می‌خواهید در تماس‌های ویدیویی پخش شود را ارسال کنید.\nویدیو باید کوتاه باشد (حداکثر 60 ثانیه).\nبرای لغو /cancel")
    return AUTO_VIDEO_STATE

async def handle_auto_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("🔄 در حال پردازش ویدیو برای ویدیو کال...")
    try:
        data = await get_user_data(user_id)
        if not data or not data['session_string']:
            await status_msg.edit_text("❌ لاگین نیستید.")
            return ConversationHandler.END
        if not update.message.video:
            await status_msg.edit_text("❌ لطفاً یک فایل ویدیویی ارسال کنید.")
            return AUTO_VIDEO_STATE
        duration = getattr(update.message.video, 'duration', 0)
        if duration > 60:
            await status_msg.edit_text("❌ ویدیو نباید بیشتر از 60 ثانیه باشد.")
            return AUTO_VIDEO_STATE

        await status_msg.edit_text("📥 در حال دانلود ویدیو...")
        file = await update.message.video.get_file()
        file_path = str(await file.download_to_drive())
        if not os.path.exists(file_path):
            await status_msg.edit_text("❌ خطا در دانلود فایل.")
            return AUTO_VIDEO_STATE

        await status_msg.edit_text("🔄 در حال تبدیل ویدیو به مربع (بدون حاشیه)...")
        square_path = file_path + "_square.mp4"
        try:
            final_file_path = await convert_to_square_ffmpeg(file_path, square_path)
        except Exception as e:
            error_detail = str(e)
            print(f"ffmpeg error detail: {error_detail}")  # لاگ در رندر
            await status_msg.edit_text(f"⚠️ خطا در تبدیل: {error_detail[:200]}\nاستفاده از ویدیوی اصلی...")
            final_file_path = file_path

        with open(final_file_path, "rb") as f:
            video_bytes = f.read()

        cache_path = _auto_video_cache_path(user_id, os.path.basename(final_file_path))
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as cache_file:
            cache_file.write(video_bytes)

        await enable_auto_video(user_id, cache_path, video_bytes, os.path.basename(final_file_path))

        if user_id not in call_clients:
            call_client = await setup_pytgcalls(user_id, data['session_string'], data['api_id'], data['api_hash'])
            if call_client:
                call_clients[user_id] = call_client
                register_incoming_call_handler(call_client, user_id)
                print(f"Auto-video call receiver is ready for owner {user_id}", flush=True)

        await status_msg.edit_text("✅ ویدیو با موفقیت تنظیم شد.\nاز این به بعد هر تماس ویدیویی به شما، با این ویدیو پاسخ داده می‌شود.")
        if os.path.exists(file_path) and file_path != final_file_path:
            os.remove(file_path)
        await main_menu(update, context)
        return ConversationHandler.END
    except Exception as e:
        error_full = str(e)
        print(f"Full error in handle_auto_video_file: {error_full}")  # لاگ در رندر
        await status_msg.edit_text(f"❌ خطا: {error_full[:300]}")
        return AUTO_VIDEO_STATE

# ---------- هندلر فایل عمومی ----------
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
            file = await (msg.audio or msg.voice).get_file()
        else:
            file = await (msg.video or msg.video_note).get_file()
        file_path = str(await file.download_to_drive())
        final_file_path = file_path
        if is_video:
            await status_msg.edit_text("🔄 در حال تبدیل ویدیو به ویدیو نوت دایره‌ای بدون حاشیه سیاه...")
            square_path = file_path + "_video_note.mp4"
            try:
                final_file_path = await convert_to_video_note_ffmpeg(file_path, square_path, target_size=480)
            except Exception as e:
                print(f"video-note conversion error: {e}", flush=True)
                await status_msg.edit_text(
                    f"❌ خطا در تبدیل ویدیو نوت: {str(e)[:300]}\n"
                    "ویدیوی اصلی ارسال نشد چون ممکن است دایره‌ای/بدون حاشیه درست نشود."
                )
                if os.path.exists(file_path):
                    os.remove(file_path)
                return
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
