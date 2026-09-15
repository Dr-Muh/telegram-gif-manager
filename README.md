# Telegram GIF Manager

A Dockerized Telegram bot for saving, organizing, and finding GIFs without requiring users to share their Telegram credentials.

## Concept

The bot is a collection of separate personal GIF libraries. A user sends one GIF to the bot, chooses an optional category or tag, and the bot stores it locally under that Telegram user's ID. Exact duplicates are detected using a content hash. When a duplicate is saved, the bot sends the already stored GIF instead of creating another copy.

The first version intentionally handles one GIF at a time. It does not scan chat history and does not use a Telegram user account. Bulk import can be considered later as a separate feature.

## User experience

`/start` and `/help` show the complete command list and open the main menu. The menu uses Telegram inline keyboards, so common actions can be selected without typing commands.

Planned commands:

- `/save` - save the next GIF sent to the bot
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
- Filter by tag or category
- Send an existing GIF when a duplicate is detected
- Start backup or restore with a confirmation step

## Persistence and recovery

The bot uses local storage inside the container for fast access:

- SQLite database for GIF metadata, hashes, tags, and backup state
- Local media directory for downloaded GIF files
- Docker volume mounted at `/data`

To survive loss of the Docker volume, `/backup` creates a compressed archive containing the current user's database records and media files. The bot sends that archive as a document directly into the same private chat with that user. No backup channel, group, or other Telegram destination is required. The user can reply to that document with `/restore` after a reinstall or data loss.

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

The bot currently implements the allowlist, help/menu, single-GIF saving, exact deduplication, per-user local storage, manual `/backup`, and reply-based `/restore`. Browse, search, tagging, and random retrieval remain on the TODO list.
