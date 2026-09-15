import hashlib
import json
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
from telegram.error import TimedOut
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
MAX_INLINE_RESULTS = 50
BACKUP_PART_SIZE = 19 * 1024 * 1024


def allowed(update: Update, config: Config) -> bool:
    user = update.effective_user
    return user is not None and user.id in config.allowed_user_ids


async def deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("This bot is private and your account is not allowlisted.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled update error", exc_info=context.error)


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Save GIF", callback_data="save"), InlineKeyboardButton("Browse", callback_data="list")],
        [InlineKeyboardButton("Bulk save", callback_data="bulk_save")],
        [InlineKeyboardButton("Random GIF", callback_data="random"), InlineKeyboardButton("Backup", callback_data="backup")],
        [InlineKeyboardButton("Use inline picker", switch_inline_query_current_chat="")],
    ])


def browse_menu(index: int, total: int, gif_id: int) -> InlineKeyboardMarkup:
    previous_index = (index - 1) % total
    next_index = (index + 1) % total
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Previous", callback_data=f"browse:{previous_index}"),
            InlineKeyboardButton(f"{index + 1} / {total}", callback_data="browse:noop"),
            InlineKeyboardButton("Next", callback_data=f"browse:{next_index}"),
        ],
        [InlineKeyboardButton("Edit tags", callback_data=f"edit_tags:{gif_id}")],
        [InlineKeyboardButton("Back to menu", callback_data="menu")],
    ])


def browse_caption(index: int, total: int, record: object) -> str:
    tags = record["tags"] or "none"
    return f"GIF {index + 1} / {total}\nTags: {tags}\nSHA-256: {record['sha256'][:12]}..."


def pending_gif_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Save GIF", callback_data="pending:save"),
        InlineKeyboardButton("Discard", callback_data="pending:discard"),
    ]])


async def show_browsed_gif(
    update: Update, context: ContextTypes.DEFAULT_TYPE, gif_id: int, edit: bool = False
) -> None:
    user_id = update.effective_user.id
    records = context.application.bot_data["database"].list_gifs(user_id)
    index = next((index for index, record in enumerate(records) if record["id"] == gif_id), None)
    if index is None:
        await update.effective_message.reply_text("That GIF is no longer in your library.")
        return
    record = records[index]
    caption = browse_caption(index, len(records), record)
    if edit:
        await update.callback_query.edit_message_media(
            media=InputMediaAnimation(media=record["file_id"], caption=caption),
            reply_markup=browse_menu(index, len(records), record["id"]),
        )
    elif context.user_data.get("browse_message_id"):
        await context.bot.edit_message_media(
            chat_id=context.user_data["browse_chat_id"],
            message_id=context.user_data["browse_message_id"],
            media=InputMediaAnimation(media=record["file_id"], caption=caption),
            reply_markup=browse_menu(index, len(records), record["id"]),
        )
    else:
            sent_message = await update.effective_message.reply_animation(
            record["file_id"], caption=caption,
            reply_markup=browse_menu(index, len(records), record["id"]),
        )
            context.user_data["browse_chat_id"] = sent_message.chat_id
            context.user_data["browse_message_id"] = sent_message.message_id


def tag_menu(gif_id: int, current_tags: str, available_tags: list[str]) -> InlineKeyboardMarkup:
    selected = {tag.strip().casefold() for tag in current_tags.split(",") if tag.strip()}
    buttons = [
        InlineKeyboardButton(
            f"{'[x]' if tag.casefold() in selected else '[ ]'} {tag}",
            callback_data=f"tag:{gif_id}:{index}",
        )
        for index, tag in enumerate(available_tags)
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("Done", callback_data=f"tags_done:{gif_id}")])
    return InlineKeyboardMarkup(rows)


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
    context.user_data.pop("save_mode", None)
    await update.effective_message.reply_text(
        "Telegram GIF Manager\n\n"
        "/save - save the next GIF\n/list - browse your GIFs\n/random - get a random GIF\n"
        "/bulk_save - save GIFs until /cancel or another command\n"
        "/backup - back up your data here\n/restore - reply to a bot backup with this command\n"
        "/cancel - cancel saving\n/delete - reply to a saved GIF to delete it\n/help - show this menu",
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
    records = records[:MAX_INLINE_RESULTS]
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
    context.user_data["save_mode"] = "single"
    await update.effective_message.reply_text("Send one GIF now. Use /cancel to stop.")
    return None


async def bulk_save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        await deny(update)
        return
    context.user_data["save_mode"] = "bulk"
    await update.effective_message.reply_text("Send GIFs now. I will keep saving them until /cancel or another command.")


async def receive_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    save_mode = context.user_data.get("save_mode")
    if not allowed(update, config):
        await deny(update)
        return None
    message = update.effective_message
    media = message.animation or (message.document if message.document and message.document.mime_type == "image/gif" else None)
    if media is None:
        if not save_mode:
            return None
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
        if not save_mode:
            await show_browsed_gif(update, context, existing["id"])
            return None
        if save_mode == "single":
            context.user_data.pop("save_mode", None)
        await message.reply_animation(existing["file_id"], caption="Already saved: this GIF is a duplicate.")
        return None
    if not save_mode:
        pending_path = user_dir / f".pending-{message.message_id}-{digest}.gif"
        temporary_path.replace(pending_path)
        context.user_data["pending_gif"] = {
            "path": str(pending_path),
            "sha256": digest,
            "file_id": media.file_id,
        }
        await message.reply_animation(
            media.file_id,
            caption="This GIF is not saved yet. Would you like to save it?",
            reply_markup=pending_gif_menu(),
        )
        return None
    destination = user_dir / f"{digest}.gif"
    temporary_path.replace(destination)
    database.add_gif(user_id, digest, str(destination.relative_to(data_dir)), media.file_id)
    if save_mode == "single":
        context.user_data.pop("save_mode", None)
        await message.reply_text("GIF saved.")
    else:
        await message.reply_text("GIF saved. Send another GIF or /cancel.")
    return None


async def receive_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config) or "editing_gif_id" not in context.user_data:
        return
    tags = ", ".join(
        tag.strip() for tag in update.effective_message.text.split(",") if tag.strip()
    )
    if tags == "-":
        tags = ""
    if len(tags) > 500:
        await update.effective_message.reply_text("Those tags are too long. Please try again (maximum 500 characters).")
        return
    gif_id = context.user_data.pop("editing_gif_id")
    context.application.bot_data["database"].update_tags(update.effective_user.id, gif_id, tags)
    await show_browsed_gif(update, context, gif_id)


async def delete_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        return await deny(update)
    reply = update.effective_message.reply_to_message
    media = reply.animation if reply else None
    if not reply or not reply.from_user or not reply.from_user.is_bot or not media:
        await update.effective_message.reply_text("Reply to a GIF sent by this bot with /delete.")
        return
    database: Database = context.application.bot_data["database"]
    record = database.find_by_file_id(update.effective_user.id, media.file_id)
    if not record:
        await update.effective_message.reply_text("That GIF is not in your saved library.")
        return
    file_path = config.data_dir / record["file_path"]
    database.delete_gif(update.effective_user.id, record["id"])
    file_path.unlink(missing_ok=True)
    await update.effective_message.reply_text("GIF deleted.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    context.user_data.pop("save_mode", None)
    if update.effective_message:
        await update.effective_message.reply_text("Cancelled.")
    return None


async def list_gifs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        return await deny(update)
    context.user_data.pop("save_mode", None)
    records = context.application.bot_data["database"].list_gifs(update.effective_user.id)
    if not records:
        await update.effective_message.reply_text("You have no saved GIFs yet.", reply_markup=menu())
        return
    record = records[0]
    caption = browse_caption(0, len(records), record)
    if update.callback_query:
        context.user_data["browse_chat_id"] = update.callback_query.message.chat_id
        context.user_data["browse_message_id"] = update.callback_query.message.message_id
        await update.callback_query.edit_message_media(
            media=InputMediaAnimation(media=record["file_id"], caption=caption),
            reply_markup=browse_menu(0, len(records), record["id"]),
        )
    else:
        sent_message = await update.effective_message.reply_animation(
            record["file_id"], caption=caption, reply_markup=browse_menu(0, len(records), record["id"])
        )
        context.user_data["browse_chat_id"] = sent_message.chat_id
        context.user_data["browse_message_id"] = sent_message.message_id


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
            caption=browse_caption(index, len(records), record),
        ),
        reply_markup=browse_menu(index, len(records), record["id"]),
    )


async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        return await deny(update)
    context.user_data.pop("save_mode", None)
    await update.effective_message.reply_text("Backup started. I am preparing your GIF library now.")
    archive, digest = create_backup(context.application.bot_data["database"], config.data_dir, update.effective_user.id)
    try:
        archive_size = archive.stat().st_size
        part_count = (archive_size + BACKUP_PART_SIZE - 1) // BACKUP_PART_SIZE
        if part_count == 1:
            with archive.open("rb") as backup_file:
                await update.effective_message.reply_document(backup_file, filename=archive.name, caption=f"GIF Manager backup | user:{update.effective_user.id} | sha256:{digest}")
            return

        parts = []
        with archive.open("rb") as backup_file:
            for part_number in range(1, part_count + 1):
                part_path = config.data_dir / f"{archive.stem}.part{part_number:03}.zip"
                part_path.write_bytes(backup_file.read(BACKUP_PART_SIZE))
                parts.append(part_path)
        uploaded_parts = []
        for part_number, part_path in enumerate(parts, start=1):
            with part_path.open("rb") as part_file:
                sent = await update.effective_message.reply_document(
                    part_file,
                    filename=part_path.name,
                    caption=f"GIF Manager backup part {part_number}/{part_count} | user:{update.effective_user.id}",
                )
            uploaded_parts.append({
                "file_id": sent.document.file_id,
                "sha256": hashlib.sha256(part_path.read_bytes()).hexdigest(),
            })
        manifest = {
            "format": 2,
            "user_id": update.effective_user.id,
            "archive_sha256": digest,
            "parts": uploaded_parts,
        }
        manifest_path = config.data_dir / f"{archive.stem}.manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with manifest_path.open("rb") as manifest_file:
            await update.effective_message.reply_document(
                manifest_file,
                filename=manifest_path.name,
                caption=f"GIF Manager backup manifest | user:{update.effective_user.id} | parts:{part_count} | Reply to this file with /restore",
            )
        await update.effective_message.reply_text(f"Backup complete: {part_count} files plus a manifest. Reply to the manifest with /restore.")
    except TimedOut:
        logger.warning("Backup upload timed out for user %s", update.effective_user.id)
        await update.effective_message.reply_text("The backup upload timed out. Please try /backup again.")
    finally:
        archive.unlink(missing_ok=True)
        (config.data_dir / f"restore-{update.effective_user.id}.manifest.json").unlink(missing_ok=True)
        (config.data_dir / f"restore-{update.effective_user.id}.part").unlink(missing_ok=True)
        for part_path in config.data_dir.glob(f"{archive.stem}.part*.zip"):
            part_path.unlink(missing_ok=True)
        (config.data_dir / f"{archive.stem}.manifest.json").unlink(missing_ok=True)


async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        return await deny(update)
        context.user_data.pop("save_mode", None)
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
    try:
        expected_digest = None
        if reply.caption.startswith("GIF Manager backup manifest"):
            manifest_path = config.data_dir / f"restore-{update.effective_user.id}.manifest.json"
            await telegram_file.download_to_drive(manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != 2 or manifest.get("user_id") != update.effective_user.id:
                raise ValueError("Backup manifest belongs to another user")
            parts = manifest.get("parts", [])
            if not isinstance(parts, list) or not parts or len(parts) > 1000:
                raise ValueError("Invalid backup manifest")
            with archive.open("wb") as assembled:
                for part in parts:
                    part_file = await context.bot.get_file(part["file_id"])
                    part_path = config.data_dir / f"restore-{update.effective_user.id}.part"
                    await part_file.download_to_drive(part_path)
                    if hashlib.sha256(part_path.read_bytes()).hexdigest() != part["sha256"]:
                        raise ValueError("Backup part checksum does not match")
                    with part_path.open("rb") as source:
                        assembled.write(source.read())
                    part_path.unlink(missing_ok=True)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest.get("archive_sha256"):
                raise ValueError("Reassembled backup checksum does not match")
            expected_digest = manifest["archive_sha256"]
            manifest_path.unlink(missing_ok=True)
        else:
            await telegram_file.download_to_drive(archive)
            digest_match = re.search(r"sha256:([0-9a-f]{64})", reply.caption)
            expected_digest = digest_match.group(1) if digest_match else None
        user_match = re.search(r"user:(\d+)", reply.caption)
        if not expected_digest or not user_match or int(user_match.group(1)) != update.effective_user.id:
            raise ValueError("Backup metadata does not match")
        count = restore_backup(context.application.bot_data["database"], config.data_dir, archive, update.effective_user.id, expected_digest)
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
    config: Config = context.application.bot_data["config"]
    if not allowed(update, config):
        await query.answer("Not allowlisted", show_alert=True)
        return
    await query.answer()
    if query.data == "save":
        context.user_data["save_mode"] = "single"
        await query.message.reply_text("Send one GIF now. Use /cancel to stop.")
    elif query.data == "bulk_save":
        context.user_data["save_mode"] = "bulk"
        await query.message.reply_text("Send GIFs now. I will keep saving them until /cancel or another command.")
    elif query.data == "pending:save":
        pending = context.user_data.pop("pending_gif", None)
        if not pending:
            await query.message.edit_caption(caption="This pending GIF has expired.")
            return
        pending_path = Path(pending["path"])
        if not pending_path.is_file():
            await query.message.edit_caption(caption="This pending GIF is no longer available.")
            return
        user_id = update.effective_user.id
        database: Database = context.application.bot_data["database"]
        if database.find_by_hash(user_id, pending["sha256"]):
            pending_path.unlink(missing_ok=True)
            await query.message.edit_caption(caption="This GIF was already saved.")
            return
        destination = pending_path.with_name(pending_path.name.removeprefix(".pending-"))
        database.add_gif(user_id, pending["sha256"], str(destination.relative_to(config.data_dir)), pending["file_id"])
        pending_path.rename(destination)
        await query.message.edit_caption(caption="GIF saved.")
    elif query.data == "pending:discard":
        pending = context.user_data.pop("pending_gif", None)
        if pending:
            Path(pending["path"]).unlink(missing_ok=True)
        await query.message.edit_caption(caption="GIF discarded.")
    elif query.data.startswith("edit_tags:"):
        gif_id = int(query.data.split(":", 1)[1])
        record = context.application.bot_data["database"].find_by_id(update.effective_user.id, gif_id)
        if not record:
            await query.message.reply_text("That GIF is no longer in your library.")
            return
        available_tags = context.application.bot_data["database"].list_tags(update.effective_user.id)
        context.user_data["editing_gif_id"] = gif_id
        context.user_data["browse_chat_id"] = query.message.chat_id
        context.user_data["browse_message_id"] = query.message.message_id
        await query.edit_message_caption(
            caption="Choose tags to add or remove -- or type a comma separated list of tags:",
            reply_markup=tag_menu(gif_id, record["tags"], available_tags),
        )
    elif query.data.startswith("tag:"):
        _, gif_id_text, tag_index_text = query.data.split(":")
        gif_id = int(gif_id_text)
        available_tags = context.application.bot_data["database"].list_tags(update.effective_user.id)
        tag_index = int(tag_index_text)
        if tag_index >= len(available_tags):
            await query.answer("That tag is no longer available.", show_alert=True)
            return
        record = context.application.bot_data["database"].find_by_id(update.effective_user.id, gif_id)
        if not record:
            await query.message.edit_text("That GIF is no longer in your library.")
            return
        tags = [tag.strip() for tag in record["tags"].split(",") if tag.strip()]
        matching_tag = available_tags[tag_index]
        if matching_tag.casefold() in {tag.casefold() for tag in tags}:
            tags = [tag for tag in tags if tag.casefold() != matching_tag.casefold()]
        else:
            tags.append(matching_tag)
        updated_tags = ", ".join(tags)
        context.application.bot_data["database"].update_tags(update.effective_user.id, gif_id, updated_tags)
        await query.edit_message_reply_markup(reply_markup=tag_menu(gif_id, updated_tags, available_tags))
    elif query.data.startswith("tags_done:"):
        gif_id = int(query.data.split(":", 1)[1])
        context.user_data.pop("editing_gif_id", None)
        await show_browsed_gif(update, context, gif_id, edit=True)
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
    application = (
        Application.builder()
        .token(config.token)
        .connect_timeout(30)
        .read_timeout(300)
        .write_timeout(300)
        .pool_timeout(30)
        .build()
    )
    application.bot_data["config"] = config
    application.bot_data["database"] = Database(config.data_dir / "gifs.db")
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("bulk_save", bulk_save_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("delete", delete_gif))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tags))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, receive_gif))
    application.add_handler(CommandHandler("list", list_gifs))
    application.add_handler(CommandHandler("backup", backup))
    application.add_handler(CommandHandler("restore", restore))
    application.add_handler(InlineQueryHandler(inline_gifs))
    application.add_handler(CallbackQueryHandler(button, pattern="^(save|bulk_save|list|backup|random|menu|browse:.*|edit_tags:.*|tag:.*|tags_done:.*|pending:(save|discard))$"))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    config = Config.from_environment()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    build_application(config).run_polling()


if __name__ == "__main__":
    main()
