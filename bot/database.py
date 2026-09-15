from pathlib import Path
import sqlite3
from typing import Iterable


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
                tags TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, sha256)
            );
            """
        )
        self.connection.commit()

    def find_by_hash(self, user_id: int, sha256: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM gifs WHERE user_id = ? AND sha256 = ?", (user_id, sha256)
        ).fetchone()

    def add_gif(self, user_id: int, sha256: str, file_path: str, file_id: str, tags: str = "") -> None:
        self.connection.execute(
            "INSERT INTO gifs (user_id, sha256, file_path, file_id, tags) VALUES (?, ?, ?, ?, ?)",
            (user_id, sha256, file_path, file_id, tags),
        )
        self.connection.commit()

    def list_gifs(self, user_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM gifs WHERE user_id = ? ORDER BY created_at DESC, id DESC", (user_id,)
        ))

    def find_by_id(self, user_id: int, gif_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM gifs WHERE user_id = ? AND id = ?", (user_id, gif_id)
        ).fetchone()

    def find_by_file_id(self, user_id: int, file_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM gifs WHERE user_id = ? AND file_id = ?", (user_id, file_id)
        ).fetchone()

    def update_tags(self, user_id: int, gif_id: int, tags: str) -> None:
        self.connection.execute(
            "UPDATE gifs SET tags = ? WHERE user_id = ? AND id = ?", (tags, user_id, gif_id)
        )
        self.connection.commit()

    def list_tags(self, user_id: int) -> list[str]:
        tags: set[str] = set()
        for record in self.list_gifs(user_id):
            tags.update(tag.strip() for tag in record["tags"].split(",") if tag.strip())
        return sorted(tags, key=str.casefold)

    def delete_gif(self, user_id: int, gif_id: int) -> None:
        self.connection.execute(
            "DELETE FROM gifs WHERE user_id = ? AND id = ?", (user_id, gif_id)
        )
        self.connection.commit()

    def replace_user_data(self, user_id: int, records: Iterable[dict[str, object]]) -> None:
        self.connection.execute("DELETE FROM gifs WHERE user_id = ?", (user_id,))
        self.connection.executemany(
            "INSERT INTO gifs (user_id, sha256, file_path, file_id, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(
                user_id,
                record["sha256"],
                record["file_path"],
                record["file_id"],
                record.get("tags", ""),
                record.get("created_at", ""),
            ) for record in records],
        )
        self.connection.commit()
