import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from bot.backup import append_file, create_backup, restore_backup, sha256_file, split_archive
from bot.database import Database
from bot.main import TAGS_PER_PAGE, remove_stale_temporary_files, tag_menu


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


def test_tags_are_normalized_and_deduplicated(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    gif_id = database.add_gif(10, "a", "gifs/10/a.gif", "file-a", tags=" Funny ,dog,  thumbs   UP, funny,")

    assert database.get_tags(10, gif_id) == ["dog", "funny", "thumbs up"]
    assert database.find_by_id(10, gif_id)["tags"] == "dog, funny, thumbs up"
    database.toggle_tag(10, gif_id, "Dog")
    database.toggle_tag(10, gif_id, "cat")
    assert database.get_tags(10, gif_id) == ["cat", "funny", "thumbs up"]


def test_tags_cannot_be_changed_on_another_users_gif(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    gif_id = database.add_gif(10, "a", "gifs/10/a.gif", "file-a", tags="dog")

    database.update_tags(20, gif_id, "cat")

    assert database.get_tags(10, gif_id) == ["dog"]


def test_tag_counts_are_per_user(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    database.add_gif(10, "a", "gifs/10/a.gif", "file-a", tags="dog, funny")
    database.add_gif(10, "b", "gifs/10/b.gif", "file-b", tags="dog")
    database.add_gif(20, "a", "gifs/20/a.gif", "file-c", tags="cat")

    assert database.tag_counts(10) == [("dog", 2), ("funny", 1)]
    assert database.list_tags(20) == ["cat"]


def test_search_matches_word_prefixes_of_all_terms(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    database.add_gif(10, "a", "gifs/10/a.gif", "file-a", tags="dog, thumbs up")
    database.add_gif(10, "b", "gifs/10/b.gif", "file-b", tags="education")
    database.add_gif(10, "c", "gifs/10/c.gif", "file-c", tags="dog, sad")
    database.add_gif(20, "d", "gifs/20/d.gif", "file-d", tags="dog")

    def search(query: str) -> list[str]:
        return sorted(record["sha256"] for record in database.search_gifs(10, query))

    assert search("") == ["a", "b", "c"]
    assert search("DO") == ["a", "c"]
    assert search("cat") == []
    assert search("up") == ["a"]
    assert search("dog sad") == ["c"]
    assert search("%") == []


def test_deleting_a_gif_removes_its_tags(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    gif_id = database.add_gif(10, "a", "gifs/10/a.gif", "file-a", tags="dog")

    database.delete_gif(10, gif_id)

    assert database.list_tags(10) == []


def test_existing_tag_column_is_migrated_to_tag_table(tmp_path: Path) -> None:
    path = tmp_path / "data" / "gifs.db"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE gifs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, sha256 TEXT NOT NULL,"
        " file_path TEXT NOT NULL, file_id TEXT NOT NULL, file_unique_id TEXT, tags TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, sha256))"
    )
    connection.execute("INSERT INTO gifs (user_id, sha256, file_path, file_id, tags) VALUES (10, 'a', 'gifs/10/a.gif', 'f', 'Dog, funny')")
    connection.execute("INSERT INTO gifs (user_id, sha256, file_path, file_id) VALUES (10, 'b', 'gifs/10/b.gif', 'g')")
    connection.commit()
    connection.close()

    database = Database(path)

    columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(gifs)")}
    assert "tags" not in columns
    assert database.find_by_hash(10, "a")["tags"] == "dog, funny"
    assert database.find_by_hash(10, "b")["tags"] == ""
    database.connection.close()
    assert Database(path).list_tags(10) == ["dog", "funny"]


def test_backup_round_trip_keeps_tags(tmp_path: Path) -> None:
    database, data_dir = make_database(tmp_path)
    user_dir = data_dir / "gifs" / "10"
    user_dir.mkdir(parents=True)
    (user_dir / "a.gif").write_bytes(b"gif-a")
    (user_dir / "b.gif").write_bytes(b"gif-b")
    database.add_gif(10, "a", "gifs/10/a.gif", "file-a", tags="dog, thumbs up")
    database.add_gif(10, "b", "gifs/10/b.gif", "file-b")
    archive, _ = create_backup(database, data_dir, 10)
    with zipfile.ZipFile(archive) as zip_file:
        manifest = json.loads(zip_file.read("manifest.json"))
    database.connection.execute("DELETE FROM gifs")
    database.connection.commit()

    assert manifest["format"] == 2
    assert {record["sha256"]: record["tags"] for record in manifest["records"]} == {"a": ["dog", "thumbs up"], "b": []}
    assert restore_backup(database, data_dir, archive, 10) == 2
    assert database.find_by_hash(10, "a")["tags"] == "dog, thumbs up"
    assert database.find_by_hash(10, "b")["tags"] == ""
    archive.unlink()


def test_restore_reads_tags_from_format_1_backup(tmp_path: Path) -> None:
    database, data_dir = make_database(tmp_path)
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("manifest.json", json.dumps({"format": 1, "user_id": 10, "records": [
            {"sha256": "a", "file_path": "gifs/10/a.gif", "file_id": "file-a", "tags": "Dog, funny", "created_at": "2025-01-01 00:00:00"},
        ]}))
        zip_file.writestr("media/a.gif", b"gif-a")

    assert restore_backup(database, data_dir, archive, 10) == 1
    assert database.find_by_hash(10, "a")["tags"] == "dog, funny"


def test_restore_rejects_invalid_tags(tmp_path: Path) -> None:
    database, data_dir = make_database(tmp_path)
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("manifest.json", json.dumps({"format": 2, "user_id": 10, "records": [
            {"sha256": "a", "file_path": "gifs/10/a.gif", "file_id": "file-a", "tags": [1, 2]},
        ]}))
        zip_file.writestr("media/a.gif", b"gif-a")

    with pytest.raises(ValueError, match="invalid tags"):
        restore_backup(database, data_dir, archive, 10)


def test_suggestions_prefer_tags_used_together(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    database.add_gif(10, "a", "gifs/10/a.gif", "file-a", tags="dog, funny")
    database.add_gif(10, "b", "gifs/10/b.gif", "file-b", tags="dog, funny")
    database.add_gif(10, "c", "gifs/10/c.gif", "file-c", tags="sad")
    database.add_gif(10, "d", "gifs/10/d.gif", "file-d", tags="sad")
    database.add_gif(10, "e", "gifs/10/e.gif", "file-e", tags="sad, cat")
    database.add_gif(20, "f", "gifs/20/f.gif", "file-f", tags="dog, private")
    new_id = database.add_gif(10, "g", "gifs/10/g.gif", "file-g")

    assert database.suggest_tags(10, new_id, 3) == ["sad", "dog", "funny"]
    database.update_tags(10, new_id, "dog")
    assert database.suggest_tags(10, new_id, 3) == ["funny", "sad", "cat"]


def test_tag_menu_shows_current_tags_and_suggestions(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    database.add_gif(10, "a", "gifs/10/a.gif", "file-a", tags="dog, funny")
    gif_id = database.add_gif(10, "b", "gifs/10/b.gif", "file-b", tags="dog")

    caption, markup = tag_menu(database, 10, gif_id, "n", "s")
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert caption.startswith("GIF saved.\nTags: dog")
    assert [button.text for button in buttons] == ["[x] dog", "[ ] funny", "Browse all tags (2)", "Done"]
    assert buttons[1].callback_data == f"tag:{gif_id}:n:s:=funny"
    assert buttons[3].callback_data == f"tags_done:{gif_id}:n"


def test_tag_menu_pages_through_all_tags(tmp_path: Path) -> None:
    database, _ = make_database(tmp_path)
    long_tag = "x" * 60
    tags = [f"tag {index:02}" for index in range(TAGS_PER_PAGE + 4)] + [long_tag]
    gif_id = database.add_gif(10, "a", "gifs/10/a.gif", "file-a", tags=tags)

    _, first_page = tag_menu(database, 10, gif_id, "b", "0")
    _, last_page = tag_menu(database, 10, gif_id, "b", "99")
    first_texts = [button.text for row in first_page.inline_keyboard for button in row]
    last_buttons = [button for row in last_page.inline_keyboard for button in row]

    assert first_texts[:2] == ["[x] tag 00", "[x] tag 01"]
    assert len([text for text in first_texts if text.startswith("[x]")]) == TAGS_PER_PAGE
    assert "1 / 2" in first_texts and "Suggestions" in first_texts
    assert "2 / 2" in [button.text for button in last_buttons]
    long_button = next(button for button in last_buttons if long_tag in button.text)
    assert long_button.callback_data == f"tag:{gif_id}:b:1:#{len(tags) - 1}"
    assert all(len(button.callback_data.encode()) <= 64 for button in last_buttons)
