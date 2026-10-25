from pathlib import Path
import sqlite3
from typing import Iterable


# Tags are joined into one display string, so callers can keep using record["tags"].
SELECT_GIFS = """
    SELECT gifs.*, COALESCE((
        SELECT group_concat(tag, ', ') FROM (SELECT tag FROM gif_tags WHERE gif_id = gifs.id ORDER BY tag)
    ), '') AS tags
    FROM gifs
"""


def normalize_tag(tag: str) -> str:
    return " ".join(tag.split()).lower()


def parse_tags(tags: str | Iterable[str]) -> list[str]:
    """Accepts a comma separated string or a list of tags and returns unique, normalized tags."""
    if isinstance(tags, str):
        tags = tags.split(",")
    unique_tags: dict[str, None] = {}
    for tag in tags:
        normalized = normalize_tag(str(tag))
        if normalized:
            unique_tags[normalized] = None
    return list(unique_tags)


def escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS gifs (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, sha256)
            );
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS gif_tags (
                gif_id INTEGER NOT NULL REFERENCES gifs(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                PRIMARY KEY (gif_id, tag)
            );
            CREATE INDEX IF NOT EXISTS gif_tags_tag ON gif_tags(tag);
            """
        )
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(gifs)")}
        if "file_unique_id" not in columns:
            self.connection.execute("ALTER TABLE gifs ADD COLUMN file_unique_id TEXT")
        if "tags" in columns:
            self._migrate_tag_column()
        self.connection.commit()

    def _migrate_tag_column(self) -> None:
        """Moves tags from the old comma separated gifs.tags column into gif_tags."""
        with self.connection:
            for row in list(self.connection.execute("SELECT id, tags FROM gifs WHERE tags != ''")):
                self._insert_tags(row["id"], parse_tags(row["tags"]))
            self.connection.execute("ALTER TABLE gifs DROP COLUMN tags")

    def _insert_tags(self, gif_id: int, tags: Iterable[str]) -> None:
        self.connection.executemany(
            "INSERT OR IGNORE INTO gif_tags (gif_id, tag) VALUES (?, ?)", [(gif_id, tag) for tag in tags]
        )

    def find_by_hash(self, user_id: int, sha256: str) -> sqlite3.Row | None:
        return self.connection.execute(
            f"{SELECT_GIFS} WHERE user_id = ? AND sha256 = ?", (user_id, sha256)
        ).fetchone()

    def add_gif(
        self, user_id: int, sha256: str, file_path: str, file_id: str,
        file_unique_id: str | None = None, tags: str | Iterable[str] = "",
    ) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO gifs (user_id, sha256, file_path, file_id, file_unique_id) VALUES (?, ?, ?, ?, ?)",
                (user_id, sha256, file_path, file_id, file_unique_id),
            )
            self._insert_tags(cursor.lastrowid, parse_tags(tags))
        return cursor.lastrowid

    def list_gifs(self, user_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            f"{SELECT_GIFS} WHERE user_id = ? ORDER BY created_at DESC, id DESC", (user_id,)
        ))

    def search_gifs(self, user_id: int, query: str) -> list[sqlite3.Row]:
        """Every search term must match the start of a word in one of the GIF's tags."""
        conditions = []
        parameters: list[object] = [user_id]
        for term in query.lower().split():
            conditions.append(
                "EXISTS (SELECT 1 FROM gif_tags WHERE gif_id = gifs.id"
                " AND (tag LIKE ? ESCAPE '\\' OR tag LIKE ? ESCAPE '\\'))"
            )
            parameters += [f"{escape_like(term)}%", f"% {escape_like(term)}%"]
        where = "".join(f" AND {condition}" for condition in conditions)
        return list(self.connection.execute(
            f"{SELECT_GIFS} WHERE user_id = ?{where} ORDER BY created_at DESC, id DESC", parameters
        ))

    def find_by_id(self, user_id: int, gif_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            f"{SELECT_GIFS} WHERE user_id = ? AND id = ?", (user_id, gif_id)
        ).fetchone()

    def find_by_file_id(self, user_id: int, file_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            f"{SELECT_GIFS} WHERE user_id = ? AND file_id = ?", (user_id, file_id)
        ).fetchone()

    def find_by_file_unique_id(self, user_id: int, file_unique_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            f"{SELECT_GIFS} WHERE user_id = ? AND file_unique_id = ?", (user_id, file_unique_id)
        ).fetchone()

    def set_file_unique_id(self, user_id: int, gif_id: int, file_unique_id: str) -> None:
        self.connection.execute(
            "UPDATE gifs SET file_unique_id = ? WHERE user_id = ? AND id = ?", (file_unique_id, user_id, gif_id)
        )
        self.connection.commit()

    def get_tags(self, user_id: int, gif_id: int) -> list[str]:
        return [row["tag"] for row in self.connection.execute(
            "SELECT tag FROM gif_tags JOIN gifs ON gifs.id = gif_tags.gif_id"
            " WHERE gifs.user_id = ? AND gifs.id = ? ORDER BY tag",
            (user_id, gif_id),
        )]

    def update_tags(self, user_id: int, gif_id: int, tags: str | Iterable[str]) -> None:
        with self.connection:
            if not self.connection.execute(
                "SELECT 1 FROM gifs WHERE user_id = ? AND id = ?", (user_id, gif_id)
            ).fetchone():
                return
            self.connection.execute("DELETE FROM gif_tags WHERE gif_id = ?", (gif_id,))
            self._insert_tags(gif_id, parse_tags(tags))

    def toggle_tag(self, user_id: int, gif_id: int, tag: str) -> None:
        tags = self.get_tags(user_id, gif_id)
        tag = normalize_tag(tag)
        self.update_tags(user_id, gif_id, [existing for existing in tags if existing != tag] if tag in tags else tags + [tag])

    def list_tags(self, user_id: int) -> list[str]:
        return [tag for tag, _ in self.tag_counts(user_id)]

    def tag_counts(self, user_id: int) -> list[tuple[str, int]]:
        """Returns each tag of the user with the number of GIFs using it, sorted by tag."""
        return [(row["tag"], row["count"]) for row in self.connection.execute(
            "SELECT tag, COUNT(*) AS count FROM gif_tags JOIN gifs ON gifs.id = gif_tags.gif_id"
            " WHERE gifs.user_id = ? GROUP BY tag ORDER BY tag",
            (user_id,),
        )]

    def suggest_tags(self, user_id: int, gif_id: int, limit: int) -> list[str]:
        """Suggests tags the GIF does not have yet.

        Tags that often appear together with the GIF's current tags come first, then the most used tags.
        """
        current_tags = set(self.get_tags(user_id, gif_id))
        together = {row["tag"]: row["count"] for row in self.connection.execute(
            "SELECT other.tag, COUNT(*) AS count FROM gif_tags AS mine"
            " JOIN gifs ON gifs.id = mine.gif_id"
            " JOIN gif_tags AS other ON other.gif_id = mine.gif_id"
            " WHERE gifs.user_id = ? AND mine.gif_id != ?"
            " AND mine.tag IN (SELECT tag FROM gif_tags WHERE gif_id = ?)"
            " GROUP BY other.tag",
            (user_id, gif_id, gif_id),
        )}
        candidates = [(tag, count) for tag, count in self.tag_counts(user_id) if tag not in current_tags]
        candidates.sort(key=lambda candidate: (-together.get(candidate[0], 0), -candidate[1], candidate[0]))
        return [tag for tag, _ in candidates[:limit]]

    def export_user_data(self, user_id: int) -> list[dict[str, object]]:
        """Returns the user's GIF records with tags as a list, as stored in backups."""
        records = []
        for row in self.list_gifs(user_id):
            record = dict(row)
            record["tags"] = parse_tags(record["tags"])
            records.append(record)
        return records

    def delete_gif(self, user_id: int, gif_id: int) -> None:
        # Tags are removed by ON DELETE CASCADE.
        self.connection.execute(
            "DELETE FROM gifs WHERE user_id = ? AND id = ?", (user_id, gif_id)
        )
        self.connection.commit()

    def replace_user_data(self, user_id: int, records: Iterable[dict[str, object]]) -> None:
        """Accepts tags as a list (current backups) or a comma separated string (older backups)."""
        with self.connection:
            self.connection.execute("DELETE FROM gifs WHERE user_id = ?", (user_id,))
            for record in records:
                cursor = self.connection.execute(
                    "INSERT INTO gifs (user_id, sha256, file_path, file_id, file_unique_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        record["sha256"],
                        record["file_path"],
                        record["file_id"],
                        record.get("file_unique_id"),
                        record.get("created_at", ""),
                    ),
                )
                self._insert_tags(cursor.lastrowid, parse_tags(record.get("tags") or ""))

    def gif_counts(self) -> dict[int, int]:
        return {
            row["user_id"]: row["count"]
            for row in self.connection.execute("SELECT user_id, COUNT(*) AS count FROM gifs GROUP BY user_id")
        }

    def is_allowed_user(self, user_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)
        ).fetchone() is not None

    def add_allowed_user(self, user_id: int, added_by: int) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO allowed_users (user_id, added_by) VALUES (?, ?)", (user_id, added_by)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def remove_allowed_user(self, user_id: int) -> bool:
        cursor = self.connection.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
        self.connection.commit()
        return cursor.rowcount > 0

    def list_allowed_users(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM allowed_users ORDER BY created_at, user_id"))
