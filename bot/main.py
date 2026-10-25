import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import random
import re
import shutil
import sys
import zipfile
from collections import deque
from typing import AsyncIterator

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultCachedGif,
    InputMediaAnimation, Message, Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError, TimedOut
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    InlineQueryHandler, MessageHandler, filters,
)

if __package__:
    from .backup import append_file, extract_backup_archive, sha256_file, split_archive, write_backup_archive
    from .config import Config
    from .database import Database, parse_tags
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bot.backup import append_file, extract_backup_archive, sha256_file, split_archive, write_backup_archive
    from bot.config import Config
    from bot.database import Database, parse_tags

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
MAX_PROCESSED_CALLBACKS = 1000
MAX_INLINE_RESULTS = 50
MAX_PENDING_GIFS = 20
BACKUP_PART_SIZE = 19 * 1024 * 1024
TAG_SUGGESTIONS = 8
TAGS_PER_PAGE = 20
MAX_CALLBACK_DATA = 64


def is_admin(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return user_id in context.application.bot_data["config"].admin_user_ids


def is_allowed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return is_admin(user_id, context) or context.application.bot_data["database"].is_allowed_user(user_id)


def allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return user is not None and is_allowed(user.id, context)


async def deny(update: Update) -> None:
    if update.effective_message:
        user = update.effective_user
        user_id_hint = f" Your Telegram user ID is {user.id}; send it to the bot admin to request access." if user else ""
        await update.effective_message.reply_text(f"This bot is private and your account is not allowlisted.{user_id_hint}")


def reset_modes(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("save_mode", None)
    stop_tag_editing(context)


def stop_tag_editing(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("editing_gif_id", "editing_origin", "editing_message"):
        context.user_data.pop(key, None)


def start_tag_editing(context: ContextTypes.DEFAULT_TYPE, gif_id: int, origin: str, message: Message) -> None:
    """Makes the next text message set the tags of this GIF.

    The origin is "b" when the tag menu was opened from the browser and "n" for a newly saved GIF.
    """
    context.user_data["editing_gif_id"] = gif_id
    context.user_data["editing_origin"] = origin
    context.user_data["editing_message"] = (message.chat_id, message.message_id)
    if origin == "b":
        context.user_data["browse_chat_id"] = message.chat_id
        context.user_data["browse_message_id"] = message.message_id


def remove_stale_temporary_files(data_dir: Path) -> None:
    # Pending GIFs live in user_data, which does not survive a restart.
    for pattern in (".pending-*.gif", ".*.part"):
        for path in data_dir.glob(f"gifs/*/{pattern}"):
            path.unlink(missing_ok=True)


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
    return f"GIF {index + 1} / {total}\nTags: {tags}"


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


def saved_caption(tags: list[str]) -> str:
    return f"GIF saved.\nTags: {', '.join(tags) or 'none'}"


def tag_menu(database: Database, user_id: int, gif_id: int, origin: str, view: str) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the tag editor caption and keyboard.

    The view is "s" for the GIF's tags plus suggestions, or a page number of all tags.
    """
    current_tags = database.get_tags(user_id, gif_id)
    available_tags = database.list_tags(user_id)
    rows = []
    if view == "s":
        shown_tags = current_tags + database.suggest_tags(user_id, gif_id, TAG_SUGGESTIONS)
        if available_tags:
            rows.append([InlineKeyboardButton(
                f"Browse all tags ({len(available_tags)})", callback_data=f"tags_view:{gif_id}:{origin}:0"
            )])
    else:
        page_count = max(1, (len(available_tags) + TAGS_PER_PAGE - 1) // TAGS_PER_PAGE)
        page = min(max(int(view), 0), page_count - 1)
        view = str(page)
        shown_tags = available_tags[page * TAGS_PER_PAGE:(page + 1) * TAGS_PER_PAGE]
        if page_count > 1:
            rows.append([
                InlineKeyboardButton("Previous", callback_data=f"tags_view:{gif_id}:{origin}:{(page - 1) % page_count}"),
                InlineKeyboardButton(f"{page + 1} / {page_count}", callback_data="browse:noop"),
                InlineKeyboardButton("Next", callback_data=f"tags_view:{gif_id}:{origin}:{(page + 1) % page_count}"),
            ])
        rows.append([InlineKeyboardButton("Suggestions", callback_data=f"tags_view:{gif_id}:{origin}:s")])

    def tag_button(tag: str) -> InlineKeyboardButton:
        # The tag itself is sent back when it fits, so the button still works after the tag list changes.
        callback_data = f"tag:{gif_id}:{origin}:{view}:={tag}"
        if len(callback_data.encode()) > MAX_CALLBACK_DATA:
            callback_data = f"tag:{gif_id}:{origin}:{view}:#{available_tags.index(tag)}"
        return InlineKeyboardButton(f"{'[x]' if tag in current_tags else '[ ]'} {tag}", callback_data=callback_data)

    buttons = [tag_button(tag) for tag in shown_tags]
    rows[:0] = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("Done", callback_data=f"tags_done:{gif_id}:{origin}")])
    heading = "GIF saved." if origin == "n" else "Edit tags."
    caption = (
        f"{heading}\nTags: {', '.join(current_tags) or 'none'}\n\n"
        "Tap a tag to add or remove it, or type comma separated tags to add. Type -tag to remove a tag."
    )
    return caption, InlineKeyboardMarkup(rows)


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
    if not allowed(update, context):
        return await deny(update)
    reset_modes(context)
    admin_help = (
        "\n\nAdmin commands:\n/allow <user ID> - allow a user\n/revoke <user ID> - revoke a user's access\n"
        "/users - list admins and allowed users"
    ) if is_admin(update.effective_user.id, context) else ""
    await update.effective_message.reply_text(
        "Telegram GIF Manager\n\n"
        "/save - save the next GIF\n/list - browse your GIFs\n/random - get a random GIF\n"
        "/bulk_save - save GIFs until /cancel or another command\n"
        "/backup - back up your data here\n/restore - reply to a bot backup with this command\n"
        "/cancel - cancel saving\n/delete - reply to a saved GIF to delete it\n/help - show this menu" + admin_help,
        reply_markup=menu(),
    )


async def inline_gifs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.inline_query.from_user
    if not is_allowed(user.id, context):
        await update.inline_query.answer([], cache_time=0, is_personal=True)
        return
    records = context.application.bot_data["database"].search_gifs(user.id, update.inline_query.query)
    records = random.sample(records, k=min(len(records), MAX_INLINE_RESULTS))
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
    if not allowed(update, context):
        await deny(update)
        return None
    reset_modes(context)
    context.user_data["save_mode"] = "single"
    await update.effective_message.reply_text("Send one GIF now. Use /cancel to stop.")
    return None


async def bulk_save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    if not allowed(update, context):
        await deny(update)
        return
    reset_modes(context)
    context.user_data["save_mode"] = "bulk"
    await update.effective_message.reply_text("Send GIFs now. I will keep saving them until /cancel or another command.")


async def receive_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    save_mode = context.user_data.get("save_mode")
    if not allowed(update, context):
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
    try:
        telegram_file = await media.get_file()
    except BadRequest as error:
        logger.warning("Could not fetch GIF for user %s: %s", user_id, error)
        await message.reply_text("Telegram does not allow bots to download this GIF. It is probably larger than 20 MB.")
        return None
    temporary_path = user_dir / f".{media.file_unique_id}.part"
    await telegram_file.download_to_drive(temporary_path)
    digest = await asyncio.to_thread(sha256_file, temporary_path)
    database: Database = context.application.bot_data["database"]
    existing = database.find_by_hash(user_id, digest)
    if existing:
        temporary_path.unlink(missing_ok=True)
        if not existing["file_unique_id"]:
            database.set_file_unique_id(user_id, existing["id"], media.file_unique_id)
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
        prompt = await message.reply_animation(
            media.file_id,
            caption="This GIF is not saved yet. Would you like to save it?",
            reply_markup=pending_gif_menu(),
        )
        pending_gifs = context.user_data.setdefault("pending_gifs", {})
        pending_gifs[prompt.message_id] = {
            "path": str(pending_path),
            "sha256": digest,
            "file_id": media.file_id,
            "file_unique_id": media.file_unique_id,
        }
        while len(pending_gifs) > MAX_PENDING_GIFS:
            expired = pending_gifs.pop(next(iter(pending_gifs)))
            Path(expired["path"]).unlink(missing_ok=True)
        return None
    destination = user_dir / f"{digest}.gif"
    temporary_path.replace(destination)
    gif_id = database.add_gif(user_id, digest, str(destination.relative_to(data_dir)), media.file_id, media.file_unique_id)
    if save_mode == "single":
        context.user_data.pop("save_mode", None)
    caption, markup = tag_menu(database, user_id, gif_id, "n", "s")
    sent_message = await message.reply_animation(media.file_id, caption=caption, reply_markup=markup)
    start_tag_editing(context, gif_id, "n", sent_message)
    return None


def apply_typed_tags(current_tags: list[str], text: str) -> list[str]:
    """Typed tags are added to the existing ones; "-tag" removes a tag and a single "-" removes all tags."""
    typed_tags = parse_tags(text)
    if typed_tags == ["-"]:
        return []
    removed_tags = set(parse_tags(tag[1:] for tag in typed_tags if tag.startswith("-")))
    added_tags = [tag for tag in typed_tags if not tag.startswith("-")]
    return [tag for tag in parse_tags(current_tags + added_tags) if tag not in removed_tags]


async def receive_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "editing_gif_id" not in context.user_data:
        await receive_gif(update, context)
        return
    if not claim_update(update, context):
        return
    if not allowed(update, context):
        return
    gif_id = context.user_data["editing_gif_id"]
    database: Database = context.application.bot_data["database"]
    tags = apply_typed_tags(database.get_tags(update.effective_user.id, gif_id), update.effective_message.text)
    if len(", ".join(tags)) > 500:
        await update.effective_message.reply_text("Those tags are too long. Please try again (maximum 500 characters).")
        return
    origin = context.user_data.get("editing_origin", "b")
    editing_message = context.user_data.get("editing_message")
    stop_tag_editing(context)
    database.update_tags(update.effective_user.id, gif_id, tags)
    # The edited GIF message shows the new tags, so the typed message is no longer needed.
    # It is deleted first, so a failing edit below cannot leave it behind.
    try:
        await update.effective_message.delete()
    except TelegramError as error:
        logger.warning("Could not delete tag message for user %s: %s", update.effective_user.id, error)
    if origin == "n" and editing_message:
        chat_id, message_id = editing_message
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id,
                caption=saved_caption(database.get_tags(update.effective_user.id, gif_id)), reply_markup=None,
            )
        except BadRequest as error:
            if "not modified" not in str(error).lower():
                raise
    else:
        await show_browsed_gif(update, context, gif_id)


async def delete_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, context):
        return await deny(update)
    reply = update.effective_message.reply_to_message
    media = reply.animation if reply else None
    if not reply or not reply.from_user or not reply.from_user.is_bot or not media:
        await update.effective_message.reply_text("Reply to a GIF sent by this bot with /delete.")
        return
    database: Database = context.application.bot_data["database"]
    # Older records may not have a file_unique_id yet, so fall back to the file_id.
    record = (
        database.find_by_file_unique_id(update.effective_user.id, media.file_unique_id)
        or database.find_by_file_id(update.effective_user.id, media.file_id)
    )
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
    reset_modes(context)
    if update.effective_message:
        await update.effective_message.reply_text("Cancelled.")
    return None


async def list_gifs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    if not allowed(update, context):
        return await deny(update)
    reset_modes(context)
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


def user_lock(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> asyncio.Lock:
    return context.application.bot_data.setdefault("user_locks", {}).setdefault(user_id, asyncio.Lock())


@asynccontextmanager
async def chat_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: str) -> AsyncIterator[None]:
    """Keeps a chat action visible; Telegram clears each one after about five seconds."""
    async def repeat() -> None:
        while True:
            try:
                await context.bot.send_chat_action(chat_id, action)
            except TelegramError as error:
                logger.debug("Could not send chat action: %s", error)
            await asyncio.sleep(4)

    task = asyncio.create_task(repeat())
    try:
        yield
    finally:
        task.cancel()


async def set_status(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except BadRequest as error:
        logger.debug("Could not update status message: %s", error)


async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, context):
        return await deny(update)
    reset_modes(context)
    user_id = update.effective_user.id
    lock = user_lock(context, user_id)
    if lock.locked():
        await update.effective_message.reply_text("A backup or restore is already running. Please wait until it finishes.")
        return
    async with lock, chat_action(context, update.effective_chat.id, ChatAction.UPLOAD_DOCUMENT):
        status = await update.effective_message.reply_text("Backup started: creating the archive...")
        records = context.application.bot_data["database"].export_user_data(user_id)
        archive, digest = await asyncio.to_thread(write_backup_archive, records, config.data_dir, user_id)
        try:
            archive_size = archive.stat().st_size
            part_count = (archive_size + BACKUP_PART_SIZE - 1) // BACKUP_PART_SIZE
            if part_count == 1:
                await set_status(status, "Backup: uploading the archive...")
                with archive.open("rb") as backup_file:
                    await update.effective_message.reply_document(backup_file, filename=archive.name, caption=f"GIF Manager backup | user:{user_id} | sha256:{digest}")
                await set_status(status, f"Backup complete: {len(records)} GIF(s).")
                return

            await set_status(status, f"Backup: splitting the archive into {part_count} parts...")
            parts = await asyncio.to_thread(split_archive, archive, config.data_dir, BACKUP_PART_SIZE)
            uploaded_parts = []
            for part_number, part_path in enumerate(parts, start=1):
                await set_status(status, f"Backup: uploading part {part_number}/{part_count}...")
                with part_path.open("rb") as part_file:
                    sent = await update.effective_message.reply_document(
                        part_file,
                        filename=part_path.name,
                        caption=f"GIF Manager backup part {part_number}/{part_count} | user:{user_id}",
                    )
                uploaded_parts.append({
                    "file_id": sent.document.file_id,
                    "sha256": await asyncio.to_thread(sha256_file, part_path),
                })
            manifest = {
                "format": 2,
                "user_id": user_id,
                "archive_sha256": digest,
                "parts": uploaded_parts,
            }
            manifest_path = config.data_dir / f"{archive.stem}.manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with manifest_path.open("rb") as manifest_file:
                await update.effective_message.reply_document(
                    manifest_file,
                    filename=manifest_path.name,
                    caption=f"GIF Manager backup manifest | user:{user_id} | parts:{part_count} | Reply to this file with /restore",
                )
            await set_status(status, f"Backup complete: {len(records)} GIF(s) in {part_count} parts.")
            await update.effective_message.reply_text(f"Backup complete: {part_count} files plus a manifest. Reply to the manifest with /restore.")
        except TimedOut:
            logger.warning("Backup upload timed out for user %s", user_id)
            await set_status(status, "Backup failed: the upload timed out. Please try /backup again.")
        except Exception:
            await set_status(status, "Backup failed because of an unexpected error. Please try /backup again.")
            raise
        finally:
            archive.unlink(missing_ok=True)
            for part_path in config.data_dir.glob(f"{archive.stem}.part*.zip"):
                part_path.unlink(missing_ok=True)
            (config.data_dir / f"{archive.stem}.manifest.json").unlink(missing_ok=True)


async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_update(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, context):
        return await deny(update)
    reset_modes(context)
    reply = update.effective_message.reply_to_message
    if not reply or not reply.document or not reply.caption or not reply.caption.startswith("GIF Manager backup"):
        await update.effective_message.reply_text("Reply to a bot-created backup document with /restore.")
        return
    if not reply.from_user or not reply.from_user.is_bot:
        await update.effective_message.reply_text("That document was not created by this bot.")
        return
    if reply.caption.startswith("GIF Manager backup part"):
        await update.effective_message.reply_text(
            "This is only one backup part. Reply to the final backup manifest with /restore."
        )
        return
    user_id = update.effective_user.id
    lock = user_lock(context, user_id)
    if lock.locked():
        await update.effective_message.reply_text("A backup or restore is already running. Please wait until it finishes.")
        return
    archive = config.data_dir / f"restore-{user_id}.zip"
    manifest_path = config.data_dir / f"restore-{user_id}.manifest.json"
    part_path = config.data_dir / f"restore-{user_id}.part"
    async with lock, chat_action(context, update.effective_chat.id, ChatAction.TYPING):
        status = await update.effective_message.reply_text("Restore started: downloading the backup...")
        try:
            telegram_file = await reply.document.get_file()
            expected_digest = None
            if reply.caption.startswith("GIF Manager backup manifest"):
                await telegram_file.download_to_drive(manifest_path)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("format") != 2 or manifest.get("user_id") != user_id:
                    raise ValueError("Backup manifest belongs to another user")
                parts = manifest.get("parts", [])
                if not isinstance(parts, list) or not parts or len(parts) > 1000:
                    raise ValueError("Invalid backup manifest")
                archive.write_bytes(b"")
                for part_number, part in enumerate(parts, start=1):
                    await set_status(status, f"Restore: downloading part {part_number}/{len(parts)}...")
                    part_file = await context.bot.get_file(part["file_id"])
                    await part_file.download_to_drive(part_path)
                    if await asyncio.to_thread(sha256_file, part_path) != part["sha256"]:
                        raise ValueError("Backup part checksum does not match")
                    await asyncio.to_thread(append_file, part_path, archive)
                    part_path.unlink(missing_ok=True)
                await set_status(status, "Restore: verifying the backup...")
                if await asyncio.to_thread(sha256_file, archive) != manifest.get("archive_sha256"):
                    raise ValueError("Reassembled backup checksum does not match")
                expected_digest = manifest["archive_sha256"]
            else:
                await telegram_file.download_to_drive(archive)
                digest_match = re.search(r"sha256:([0-9a-f]{64})", reply.caption)
                expected_digest = digest_match.group(1) if digest_match else None
            user_match = re.search(r"user:(\d+)", reply.caption)
            if not expected_digest or not user_match or int(user_match.group(1)) != user_id:
                raise ValueError("Backup metadata does not match")
            await set_status(status, "Restore: restoring your GIFs...")
            records = await asyncio.to_thread(extract_backup_archive, config.data_dir, archive, user_id, expected_digest)
            context.application.bot_data["database"].replace_user_data(user_id, records)
            await set_status(status, f"Restored {len(records)} GIF(s).")
        except (ValueError, OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
            logger.warning("Restore rejected: %s", error)
            await set_status(status, f"Restore rejected: {error}")
        except TimedOut:
            logger.warning("Restore download timed out for user %s", user_id)
            await set_status(status, "Restore failed: the download timed out. Please try /restore again.")
        except Exception:
            await set_status(status, "Restore failed because of an unexpected error. Please try /restore again.")
            raise
        finally:
            archive.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            part_path.unlink(missing_ok=True)


def user_id_argument(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if len(context.args) != 1:
        return None
    try:
        return int(context.args[0])
    except ValueError:
        return None


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not allowed(update, context):
        await deny(update)
        return False
    if not is_admin(update.effective_user.id, context):
        await update.effective_message.reply_text("Only bot admins can manage users.")
        return False
    return True


async def allow_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_message(update, context) or not await require_admin(update, context):
        return
    user_id = user_id_argument(context)
    if user_id is None:
        await update.effective_message.reply_text("Usage: /allow <Telegram user ID>")
        return
    if is_admin(user_id, context):
        await update.effective_message.reply_text(f"User {user_id} is an admin and is always allowed.")
        return
    database: Database = context.application.bot_data["database"]
    if database.add_allowed_user(user_id, update.effective_user.id):
        await update.effective_message.reply_text(f"User {user_id} can now use the bot.")
    else:
        await update.effective_message.reply_text(f"User {user_id} is already allowed.")


async def revoke_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_message(update, context) or not await require_admin(update, context):
        return
    user_id = user_id_argument(context)
    if user_id is None:
        await update.effective_message.reply_text("Usage: /revoke <Telegram user ID>")
        return
    if is_admin(user_id, context):
        await update.effective_message.reply_text(
            f"User {user_id} is an admin. Remove them from ADMIN_USER_IDS to revoke their access."
        )
        return
    database: Database = context.application.bot_data["database"]
    if database.remove_allowed_user(user_id):
        await update.effective_message.reply_text(f"User {user_id} can no longer use the bot. Their saved GIFs are kept.")
    else:
        await update.effective_message.reply_text(f"User {user_id} was not allowed.")


def format_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def directory_size(path: Path) -> int:
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


async def display_name(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    try:
        chat = await context.bot.get_chat(user_id)
    except TelegramError:
        # Telegram only knows the chat once the user has started the bot.
        return "unknown (has not started the bot yet)"
    name = " ".join(part for part in (chat.first_name, chat.last_name) if part) or "no name"
    return f"{name} (@{chat.username})" if chat.username else name


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not claim_message(update, context) or not await require_admin(update, context):
        return
    config: Config = context.application.bot_data["config"]
    database: Database = context.application.bot_data["database"]
    admin_ids = sorted(config.admin_user_ids)
    allowed_records = [record for record in database.list_allowed_users() if record["user_id"] not in config.admin_user_ids]
    gif_counts = database.gif_counts()
    listed_ids = set(admin_ids) | {record["user_id"] for record in allowed_records}
    revoked_ids = sorted(set(gif_counts) - listed_ids)
    user_ids = admin_ids + [record["user_id"] for record in allowed_records] + revoked_ids

    names = dict(zip(user_ids, await asyncio.gather(*(display_name(context, user_id) for user_id in user_ids))))
    sizes = dict(zip(user_ids, await asyncio.gather(*(
        asyncio.to_thread(directory_size, config.data_dir / "gifs" / str(user_id)) for user_id in user_ids
    ))))
    data_size = await asyncio.to_thread(directory_size, config.data_dir)
    disk = shutil.disk_usage(config.data_dir)

    def describe(user_id: int, details: str = "") -> str:
        return (
            f"- {names[user_id]}\n"
            f"  ID {user_id} | {gif_counts.get(user_id, 0)} GIF(s), {format_size(sizes[user_id])}{details}"
        )

    sections = [
        "Admins:\n" + "\n".join(describe(user_id) for user_id in admin_ids),
        "Allowed users:\n" + ("\n".join(
            describe(record["user_id"], f"\n  added by {record['added_by']} on {record['created_at']}")
            for record in allowed_records
        ) or "none"),
    ]
    if revoked_ids:
        sections.append("Revoked users with stored GIFs:\n" + "\n".join(describe(user_id) for user_id in revoked_ids))
    sections.append(
        f"Storage:\nBot data: {format_size(data_size)}\n"
        f"Free on host: {format_size(disk.free)} of {format_size(disk.total)}"
    )
    await update.effective_message.reply_text("\n\n".join(sections))


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not claim_callback(update, context):
        return
    config: Config = context.application.bot_data["config"]
    if not allowed(update, context):
        await query.answer("Not allowlisted", show_alert=True)
        return
    await query.answer()
    if query.data == "save":
        reset_modes(context)
        context.user_data["save_mode"] = "single"
        await query.message.reply_text("Send one GIF now. Use /cancel to stop.")
    elif query.data == "bulk_save":
        reset_modes(context)
        context.user_data["save_mode"] = "bulk"
        await query.message.reply_text("Send GIFs now. I will keep saving them until /cancel or another command.")
    elif query.data == "pending:save":
        pending = context.user_data.get("pending_gifs", {}).pop(query.message.message_id, None)
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
        destination = pending_path.with_name(f"{pending['sha256']}.gif")
        pending_path.replace(destination)
        gif_id = database.add_gif(
            user_id, pending["sha256"], str(destination.relative_to(config.data_dir)),
            pending["file_id"], pending["file_unique_id"],
        )
        caption, markup = tag_menu(database, user_id, gif_id, "n", "s")
        await query.message.edit_caption(caption=caption, reply_markup=markup)
        start_tag_editing(context, gif_id, "n", query.message)
    elif query.data == "pending:discard":
        pending = context.user_data.get("pending_gifs", {}).pop(query.message.message_id, None)
        if pending:
            Path(pending["path"]).unlink(missing_ok=True)
        await query.message.edit_caption(caption="GIF discarded.")
    elif query.data.startswith(("edit_tags:", "tags_view:", "tag:")):
        user_id = update.effective_user.id
        database: Database = context.application.bot_data["database"]
        parts = query.data.split(":", 4)
        if parts[0] == "edit_tags":
            parts = ["tags_view", parts[1], "b", "s"]
        elif parts[0] == "tag" and len(parts) == 3:
            # Tag menus sent before suggestions were added only contain the tag index.
            parts = ["tag", parts[1], "b", "s", f"#{parts[2]}"]
        gif_id, origin, view = int(parts[1]), parts[2], parts[3]
        if not database.find_by_id(user_id, gif_id):
            await query.edit_message_caption(caption="That GIF is no longer in your library.")
            return
        if parts[0] == "tag":
            reference = parts[4]
            if reference.startswith("="):
                tag = reference[1:]
            else:
                available_tags = database.list_tags(user_id)
                tag_index = int(reference[1:])
                if tag_index >= len(available_tags):
                    await query.answer("That tag is no longer available.", show_alert=True)
                    return
                tag = available_tags[tag_index]
            database.toggle_tag(user_id, gif_id, tag)
        start_tag_editing(context, gif_id, origin, query.message)
        caption, markup = tag_menu(database, user_id, gif_id, origin, view)
        await query.edit_message_caption(caption=caption, reply_markup=markup)
    elif query.data.startswith("tags_done:"):
        parts = query.data.split(":")
        gif_id = int(parts[1])
        origin = parts[2] if len(parts) > 2 else "b"
        if context.user_data.get("editing_gif_id") == gif_id:
            stop_tag_editing(context)
        if origin == "n":
            tags = context.application.bot_data["database"].get_tags(update.effective_user.id, gif_id)
            await query.edit_message_caption(caption=saved_caption(tags), reply_markup=None)
        else:
            await show_browsed_gif(update, context, gif_id, edit=True)
    elif query.data.startswith("browse:"):
        if query.data != "browse:noop":
            await browse_gif(update, context, int(query.data.split(":", 1)[1]))
    elif query.data == "menu":
        await query.message.reply_text("Choose an action:", reply_markup=menu())
    elif query.data == "list":
        await list_gifs(update, context)
    elif query.data == "backup":
        # Run in the background like the /backup command, so other updates are not held up.
        context.application.create_task(backup(update, context), update=update)
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
    application.add_handler(CommandHandler("allow", allow_user))
    application.add_handler(CommandHandler("revoke", revoke_user))
    application.add_handler(CommandHandler("users", list_users))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tags))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, receive_gif))
    application.add_handler(CommandHandler("list", list_gifs))
    # Backup and restore can take minutes, so they run in the background instead of blocking other updates.
    application.add_handler(CommandHandler("backup", backup, block=False))
    application.add_handler(CommandHandler("restore", restore, block=False))
    application.add_handler(InlineQueryHandler(inline_gifs))
    application.add_handler(CallbackQueryHandler(button, pattern="^(save|bulk_save|list|backup|random|menu|browse:.*|edit_tags:.*|tag:.*|tags_view:.*|tags_done:.*|pending:(save|discard))$"))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    config = Config.from_environment()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    remove_stale_temporary_files(config.data_dir)
    build_application(config).run_polling()


if __name__ == "__main__":
    main()
