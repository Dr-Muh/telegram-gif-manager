import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from bot.backup import append_file, create_backup, restore_backup, sha256_file, split_archive
from bot.database import Database
from bot.main import remove_stale_temporary_files


def make_database(tmp_path: Path) -> tuple[Database, Path]:
    data_dir = tmp_path / "data"
    database = Database(data_dir / "gifs.db")
    return database, data_dir


def test_gifs_are_separated_by_user_and_hash(tmp_path: Path) -> None:
    database, data_dir = make_database(tmp_path)
    database.add_gif(10, "same", "gifs/10/same.gif", "file-10")
    database.add_gif(20, "same", "gifs/20/same.gif", "file-20")

    assert len(database.list_gifs(10)) == 1
    assert len(database.list_gifs(20)) == 1
    assert database.find_by_hash(10, "same")["file_id"] == "file-10"


def test_backup_round_trip_restores_only_that_user(tmp_path: Path) -> None:
    database, data_dir = make_database(tmp_path)
    user_dir = data_dir / "gifs" / "10"
    user_dir.mkdir(parents=True)
    (user_dir / "same.gif").write_bytes(b"gif-bytes")
    database.add_gif(10, "same", "gifs/10/same.gif", "file-10")
    archive, _ = create_backup(database, data_dir, 10)
    database.connection.execute("DELETE FROM gifs")
    database.connection.commit()
    (user_dir / "same.gif").unlink()

    assert restore_backup(database, data_dir, archive, 10) == 1
    assert database.list_gifs(10)[0]["sha256"] == "same"
    assert (user_dir / "same.gif").read_bytes() == b"gif-bytes"
    archive.unlink()


def test_restore_rejects_another_users_backup(tmp_path: Path) -> None:
    database, data_dir = make_database(tmp_path)
    archive = tmp_path / "foreign.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("manifest.json", json.dumps({"format": 1, "user_id": 20, "records": []}))

    with pytest.raises(ValueError, match="another user"):
        restore_backup(database, data_dir, archive, 10)


def test_allowed_users_can_be_added_and_removed(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)

    assert not database.is_allowed_user(30)
    assert database.add_allowed_user(30, added_by=1)
    assert not database.add_allowed_user(30, added_by=1)
    assert database.is_allowed_user(30)
    assert [record["user_id"] for record in database.list_allowed_users()] == [30]
    assert database.remove_allowed_user(30)
    assert not database.remove_allowed_user(30)
    assert not database.is_allowed_user(30)


def test_gifs_can_be_found_by_file_unique_id(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    database.add_gif(10, "same", "gifs/10/same.gif", "file-10", "unique-10")
    database.add_gif(10, "other", "gifs/10/other.gif", "file-11")

    assert database.find_by_file_unique_id(10, "unique-10")["sha256"] == "same"
    assert database.find_by_file_unique_id(20, "unique-10") is None
    other = database.find_by_hash(10, "other")
    database.set_file_unique_id(10, other["id"], "unique-11")
    assert database.find_by_file_unique_id(10, "unique-11")["sha256"] == "other"


def test_existing_database_gets_file_unique_id_column(tmp_path: Path) -> None:
    path = tmp_path / "data" / "gifs.db"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE gifs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, sha256 TEXT NOT NULL,"
        " file_path TEXT NOT NULL, file_id TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, sha256))"
    )
    connection.execute("INSERT INTO gifs (user_id, sha256, file_path, file_id) VALUES (10, 'old', 'gifs/10/old.gif', 'f')")
    connection.commit()
    connection.close()

    database = Database(path)

    assert database.find_by_hash(10, "old")["file_unique_id"] is None


def test_stale_temporary_files_are_removed(tmp_path: Path) -> None:
    user_dir = tmp_path / "gifs" / "10"
    user_dir.mkdir(parents=True)
    (user_dir / ".pending-5-abc.gif").write_bytes(b"pending")
    (user_dir / ".unique.part").write_bytes(b"partial")
    (user_dir / "abc.gif").write_bytes(b"saved")

    remove_stale_temporary_files(tmp_path)

    assert [path.name for path in user_dir.iterdir()] == ["abc.gif"]


def test_split_archive_parts_reassemble_to_the_original(tmp_path: Path) -> None:
    archive = tmp_path / "backup.zip"
    archive.write_bytes(bytes(range(256)) * 10)
    parts = split_archive(archive, tmp_path, 1000)
    reassembled = tmp_path / "reassembled.zip"
    reassembled.write_bytes(b"")
    for part in parts:
        append_file(part, reassembled)

    assert len(parts) == 3
    assert sha256_file(reassembled) == sha256_file(archive)


def test_gif_counts_are_grouped_by_user(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    database.add_gif(10, "a", "gifs/10/a.gif", "file-a")
    database.add_gif(10, "b", "gifs/10/b.gif", "file-b")
    database.add_gif(20, "a", "gifs/20/a.gif", "file-c")

    assert database.gif_counts() == {10: 2, 20: 1}
