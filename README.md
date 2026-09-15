# Telegram GIF Manager

A Dockerized Telegram bot for saving, organizing, and finding GIFs without requiring users to share their Telegram credentials.

## Concept

The bot is a collection of separate personal GIF libraries. A user sends one GIF to the bot, chooses an optional category or tag, and the bot stores it locally under that Telegram user's ID. Exact duplicates are detected using a content hash. When a duplicate is saved, the bot sends the already stored GIF instead of creating another copy.

The first version intentionally handles one GIF at a time. It does not scan chat history and does not use a Telegram user account. Bulk import can be considered later as a separate feature.

## User experience

`/start` and `/help` show the complete command list and open the main menu. The menu uses Telegram inline keyboards, so common actions can be selected without typing commands.

Planned commands:

- `/save` - save the next GIF sent to the bot
- `/bulk_save` - save GIFs until `/cancel` or another command
- `/delete` - reply to a saved GIF with this command to delete it
- `/list` - browse saved GIFs
- `/search <term>` - find GIFs by tag or category
- `/tags` - browse and manage tags
- `/random` - receive a random saved GIF
- `/backup` - create a backup document in this bot chat
- `/restore` - restore the backup document this command replies to
- `/cancel` - cancel the current action
- `/help` - show commands and open the main menu

Planned inline-menu actions:

- Save a GIF, add tags, and finish saving
- Browse previous or next GIF
- Edit tags on the currently browsed GIF
- Filter by tag or category
- Send an existing GIF when a duplicate is detected
- Ask before saving a GIF sent without a save command; existing GIFs open in browse mode
- Start backup or restore with a confirmation step
- Open the native Telegram inline GIF picker

## Inline GIF picker

Enable inline mode once in BotFather with `/setinline` for this bot. Users can then press **Use inline picker** in the menu or type `@your_bot` in any chat. The bot returns `InlineQueryResultCachedGif` results using the Telegram `file_id`s already stored for that user, so Telegram renders the results in its native inline grid and no media is downloaded again.

Inline results are private to the requesting allowlisted user. An empty inline query shows that user's saved GIFs; typing a search term currently filters by saved tags. Inline mode does not expose another user's library.

## Persistence and recovery

The bot uses local storage inside the container for fast access:

- SQLite database for GIF metadata, hashes, tags, and backup state
- Local media directory for downloaded GIF files
- Docker volume mounted at `/data`

To survive loss of the Docker volume, `/backup` creates a compressed archive containing the current user's database records and media files. The bot sends that archive as a document directly into the same private chat with that user. No backup channel, group, or other Telegram destination is required. The user can reply to that document with `/restore` after a reinstall or data loss.

The hosted Telegram Bot API limits bot document uploads to 50 MiB. The bot checks the generated archive before uploading and reports its size when the library is too large. The configured 2 GiB GIF limit applies to individual GIFs on local storage; it does not increase Telegram's document upload limit.

Backups larger than 19 MiB are split into numbered parts followed by a small manifest document. The 19 MiB part size supports both Telegram's bot upload and hosted `getFile` download limits. To restore one, reply to the manifest document with `/restore`; the bot downloads and verifies every part, reassembles the archive, and then restores the library.

The bot only needs its bot token to write and read backup documents in the user's private chat. Backups should be treated as sensitive. The bot should reject restore attempts for documents that were not created by the bot or do not belong to the current user.

## Planned architecture

- Python bot using `python-telegram-bot`
- SQLite repository for metadata and deduplication
- Local filesystem storage under `/data/gifs`
- Backup service for archive creation, upload, download, validation, and restore
- Docker image and `docker-compose.yml` with a persistent `/data` volume
- Configuration through environment variables such as `BOT_TOKEN` and storage settings

The bot should validate backup archives before replacing local data and keep the current data until the restore has completed successfully. Backup retention in each bot chat will be configurable.

## Out of scope for the MVP

- Bulk-saving or scanning historical chat messages
- Telegram user credentials, API ID/API hash, or Telethon sessions
- Approximate or perceptual duplicate detection
- Cloud object storage
- Complex multi-user permissions

## Status

Initial implementation is available. Copy `.env.example` to `.env`, set the bot token and one or more Telegram user IDs in `ALLOWED_USER_IDS`, then run `docker compose up --build`.

The bot currently implements the allowlist, help/menu, single-GIF saving, bulk saving until cancellation, exact deduplication, per-user local storage, manual `/backup`, reply-based `/restore`, and the inline GIF picker. Search, tagging, and random retrieval remain on the TODO list.

## Run without Docker

Install the dependencies with `python -m pip install -r requirements.txt`, then set `BOT_TOKEN`, `ALLOWED_USER_IDS`, and optionally `DATA_DIR` in the process environment. From the repository root, either command works:

```powershell
python -m bot.main
python bot/main.py
```

Without Docker, data defaults to `./data` inside the project and can be changed with `DATA_DIR`. Docker overrides this setting and stores data in its persistent `/data` volume. The bot requires a valid token and at least one allowlisted Telegram user ID before it starts polling.

## Build the image for Linux amd64 (telegram-gif-manager.tar already compiled)

docker build --platform linux/amd64 -t telegram-gif-manager .
docker save -o telegram-gif-manager.tar telegram-gif-manager