import hashlib
import logging
from pathlib import Path
import re
import sys
from collections import deque

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultCachedGif,
    InputMediaAnimation, Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    InlineQueryHandler, MessageHandler, filters,
)

if __package__:
    from .backup import create_backup, restore_backup
    from .config import Config
    from .database import Database
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bot.backup import create_backup, restore_backup
    from bot.config import Config
    from bot.database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
MAX_PROCESSED_CALLBACKS = 1000


def allowed(update: Update, config: Config) -> bool:
    user = update.effective_user
    return user is not None and user.id in config.allowed_user_ids


async def deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("This bot is private and your account is not allowlisted.")


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Save GIF", callback_data="save"), InlineKeyboardButton("Browse", callback_data="list")],
        [InlineKeyboardButton("Random GIF", callback_data="random"), InlineKeyboardButton("Backup", callback_data="backup")],
        [InlineKeyboardButton("Use inline picker", switch_inline_query_current_chat="")],
    ])


def browse_menu(index: int, total: int) -> InlineKeyboardMarkup:
    previous_index = (index - 1) % total
    next_index = (index + 1) % total
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Previous", callback_data=f"browse:{previous_index}"),
            InlineKeyboardButton(f"{index + 1} / {total}", callback_data="browse:noop"),
            InlineKeyboardButton("Next", callback_data=f"browse:{next_index}"),
        ],
        [InlineKeyboardButton("Back to menu", callback_data="menu")],
    ])


def claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    query = update.callback_query
    processed_callbacks = context.application.bot_data.setdefault("processed_callbacks", deque(maxlen=MAX_PROCESSED_CALLBACKS))
    if query.id in processed_callbacks:
        return False
    processed_callbacks.append(query.id)
    return True


def claim_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return False
    message_key = (chat.id, message.message_id)
    processed_messages = context.application.bot_data.setdefault("processed_messages", deque(maxlen=MAX_PROCESSED_CALLBACKS))
    if message_key in processed_messages:
        return False
    processed_messages.append(message_key)
    return True


def claim_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.callback_query:
        return True
    return claim_message(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_message(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        return await deny(update)
    await update.effective_message.reply_text(
        "Telegram GIF Manager\n\n"
        "/save - save the next GIF\n/list - browse your GIFs\n/random - get a random GIF\n"
        "/backup - back up your data here\n/restore - reply to a bot backup with this command\n"
        "/cancel - cancel saving\n/help - show this menu",
        reply_markup=menu(),
    )


async def inline_gifs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    user = update.inline_query.from_user
    if user.id not in config.allowed_user_ids:
        await update.inline_query.answer([], cache_time=0, is_personal=True)
        return
    query = update.inline_query.query.strip().casefold()
    records = context.application.bot_data["database"].list_gifs(user.id)
    if query:
        records = [record for record in records if query in record["tags"].casefold()]
    results = [
        InlineQueryResultCachedGif(
            id=f"gif-{record['id']}",
            gif_file_id=record["file_id"],
            title=f"GIF {index + 1}",
        )
        for index, record in enumerate(records)
    ]
    await update.inline_query.answer(results, cache_time=0, is_personal=True)


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        await deny(update)
        return None
    context.user_data["awaiting_gif"] = True
    await update.effective_message.reply_text("Send one GIF now. Use /cancel to stop.")
    return None


async def receive_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not context.user_data.get("awaiting_gif"):
        return None
    if not allowed(update, config):
        await deny(update)
        return None
    message = update.effective_message
    media = message.animation or (message.document if message.document and message.document.mime_type == "image/gif" else None)
    if media is None:
        await message.reply_text("Please send a GIF animation or a GIF document, or use /cancel.")
        return None
    if media.file_size and media.file_size > config.max_file_size:
        await message.reply_text("That GIF is larger than the configured file-size limit.")
        return None
    user_id = update.effective_user.id
    data_dir: Path = config.data_dir
    user_dir = data_dir / "gifs" / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    telegram_file = await media.get_file()
    temporary_path = user_dir / f".{media.file_unique_id}.part"
    await telegram_file.download_to_drive(temporary_path)
    digest = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
    database: Database = context.application.bot_data["database"]
    existing = database.find_by_hash(user_id, digest)
    if existing:
        temporary_path.unlink(missing_ok=True)
        context.user_data.pop("awaiting_gif", None)
        await message.reply_animation(existing["file_id"], caption="Already saved: this GIF is a duplicate.")
        return None
    destination = user_dir / f"{digest}.gif"
    temporary_path.replace(destination)
    database.add_gif(user_id, digest, str(destination.relative_to(data_dir)), media.file_id)
    context.user_data.pop("awaiting_gif", None)
    await message.reply_text("GIF saved.")
    return None


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    context.user_data.pop("awaiting_gif", None)
    if update.effective_message:
        await update.effective_message.reply_text("Cancelled.")
    return None


async def list_gifs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        return await deny(update)
    records = context.application.bot_data["database"].list_gifs(update.effective_user.id)
    if not records:
        await update.effective_message.reply_text("You have no saved GIFs yet.", reply_markup=menu())
        return
    record = records[0]
    caption = f"GIF 1 / {len(records)}\nSHA-256: {record['sha256'][:12]}..."
    if update.callback_query:
        await update.callback_query.edit_message_media(
            media=InputMediaAnimation(media=record["file_id"], caption=caption),
            reply_markup=browse_menu(0, len(records)),
        )
    else:
        await update.effective_message.reply_animation(
            record["file_id"], caption=caption, reply_markup=browse_menu(0, len(records))
        )


async def browse_gif(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    query = update.callback_query
    records = context.application.bot_data["database"].list_gifs(update.effective_user.id)
    if not records:
        await query.edit_message_text("You have no saved GIFs yet.", reply_markup=menu())
        return
    index %= len(records)
    record = records[index]
    await query.edit_message_media(
        media=InputMediaAnimation(
            media=record["file_id"],
            caption=f"GIF {index + 1} / {len(records)}\nSHA-256: {record['sha256'][:12]}...",
        ),
        reply_markup=browse_menu(index, len(records)),
    )


async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        return await deny(update)
    archive, digest = create_backup(context.application.bot_data["database"], config.data_dir, update.effective_user.id)
    try:
        with archive.open("rb") as backup_file:
            await update.effective_message.reply_document(backup_file, filename=archive.name, caption=f"GIF Manager backup | user:{update.effective_user.id} | sha256:{digest}")
    finally:
        archive.unlink(missing_ok=True)


async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        return await deny(update)
    reply = update.effective_message.reply_to_message
    if not reply or not reply.document or not reply.caption or not reply.caption.startswith("GIF Manager backup"):
        await update.effective_message.reply_text("Reply to a bot-created backup document with /restore.")
        return
    if not reply.from_user or not reply.from_user.is_bot:
        await update.effective_message.reply_text("That document was not created by this bot.")
        return
    archive = config.data_dir / f"restore-{update.effective_user.id}.zip"
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_DOCUMENT)
    telegram_file = await reply.document.get_file()
    await telegram_file.download_to_drive(archive)
    try:
        digest_match = re.search(r"sha256:([0-9a-f]{64})", reply.caption)
        user_match = re.search(r"user:(\d+)", reply.caption)
        if not digest_match or not user_match or int(user_match.group(1)) != update.effective_user.id:
            raise ValueError("Backup metadata does not match")
        count = restore_backup(context.application.bot_data["database"], config.data_dir, archive, update.effective_user.id, digest_match.group(1))
        await update.effective_message.reply_text(f"Restored {count} GIF(s).")
    except (ValueError, OSError, KeyError) as error:
        logger.warning("Restore rejected: %s", error)
        await update.effective_message.reply_text("Restore rejected: the backup is invalid or belongs to another user.")
    finally:
        archive.unlink(missing_ok=True)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not claim_callback(update, context):
        return
    await query.answer()
    if query.data == "save":
        context.user_data["awaiting_gif"] = True
        await query.message.reply_text("Send one GIF now. Use /cancel to stop.")
    elif query.data.startswith("browse:"):
        if query.data != "browse:noop":
            await browse_gif(update, context, int(query.data.split(":", 1)[1]))
    elif query.data == "menu":
        await query.message.reply_text("Choose an action:", reply_markup=menu())
    elif query.data == "list":
        await list_gifs(update, context)
    elif query.data == "backup":
        await backup(update, context)
    else:
        await query.message.reply_text("Random GIF browsing will be added next.")


def build_application(config: Config) -> Application:
    application = Application.builder().token(config.token).build()
    application.bot_data["config"] = config
    application.bot_data["database"] = Database(config.data_dir / "gifs.db")
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, receive_gif))
    application.add_handler(CommandHandler("list", list_gifs))
    application.add_handler(CommandHandler("backup", backup))
    application.add_handler(CommandHandler("restore", restore))
    application.add_handler(InlineQueryHandler(inline_gifs))
    application.add_handler(CallbackQueryHandler(button, pattern="^(save|list|backup|random|menu|browse:.*)$"))
    return application


def main() -> None:
    config = Config.from_environment()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    build_application(config).run_polling()


if __name__ == "__main__":
    main()
