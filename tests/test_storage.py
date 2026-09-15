import json
from pathlib import Path
import zipfile

import pytest

from bot.backup import create_backup, restore_backup
from bot.database import Database


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
