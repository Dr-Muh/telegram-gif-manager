from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

from .database import Database


PREFIX = "telegram-gif-manager-backup"
# Format 2 stores tags as a list; format 1 stored them as a comma separated string.
ARCHIVE_FORMAT = 2
SUPPORTED_ARCHIVE_FORMATS = {1, 2}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(database: Database, data_dir: Path, user_id: int) -> tuple[Path, str]:
    return write_backup_archive(database.export_user_data(user_id), data_dir, user_id)


def write_backup_archive(records: list[dict[str, object]], data_dir: Path, user_id: int) -> tuple[Path, str]:
    """Builds the archive without touching the database, so it can run in a worker thread."""
    file_descriptor, archive_name = tempfile.mkstemp(prefix=f"{PREFIX}-{user_id}-", suffix=".zip")
    os.close(file_descriptor)
    archive = Path(archive_name)
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
            manifest = {
                "format": ARCHIVE_FORMAT,
                "user_id": user_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "records": records,
            }
            zip_file.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
            for record in records:
                source = data_dir / record["file_path"]
                if source.is_file():
                    zip_file.write(source, f"media/{source.name}")
        return archive, sha256_file(archive)
    except Exception:
        archive.unlink(missing_ok=True)
        raise


def restore_backup(database: Database, data_dir: Path, archive: Path, user_id: int, expected_digest: str | None = None) -> int:
    records = extract_backup_archive(data_dir, archive, user_id, expected_digest)
    database.replace_user_data(user_id, records)
    return len(records)


def extract_backup_archive(
    data_dir: Path, archive: Path, user_id: int, expected_digest: str | None = None
) -> list[dict[str, object]]:
    """Validates the archive and copies its media without touching the database, so it can run in a worker thread."""
    if expected_digest and sha256_file(archive) != expected_digest:
        raise ValueError("Backup checksum does not match")
    with tempfile.TemporaryDirectory() as temporary_dir:
        temporary_path = Path(temporary_dir)
        with zipfile.ZipFile(archive) as zip_file:
            names = set(zip_file.namelist())
            if "manifest.json" not in names or any(name.startswith("/") or ".." in Path(name).parts for name in names):
                raise ValueError("Invalid backup archive")
            zip_file.extractall(temporary_path)
        manifest = json.loads((temporary_path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") not in SUPPORTED_ARCHIVE_FORMATS or manifest.get("user_id") != user_id:
            raise ValueError("Backup belongs to another user or uses an unsupported format")
        records = manifest.get("records", [])
        if not isinstance(records, list):
            raise ValueError("Invalid backup records")
        normalized_records = []
        for record in records:
            relative_path = Path(str(record["file_path"]).replace("\\", "/"))
            if relative_path.is_absolute() or ".." in relative_path.parts or relative_path.parts[:2] != ("gifs", str(user_id)):
                raise ValueError("Backup contains an unsafe media path")
            source = temporary_path / "media" / relative_path.name
            if not source.is_file():
                raise ValueError("Backup is missing media files")
            tags = record.get("tags", "")
            if not isinstance(tags, str) and not (isinstance(tags, list) and all(isinstance(tag, str) for tag in tags)):
                raise ValueError("Backup contains invalid tags")
            normalized_record = dict(record)
            normalized_record["file_path"] = relative_path.as_posix()
            normalized_records.append(normalized_record)
        user_dir = data_dir / "gifs" / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        for record in normalized_records:
            relative_path = Path(str(record["file_path"]).replace("\\", "/"))
            source = temporary_path / "media" / relative_path.name
            shutil.copy2(source, data_dir / relative_path)
        return normalized_records


def split_archive(archive: Path, directory: Path, part_size: int) -> list[Path]:
    parts = []
    with archive.open("rb") as source:
        for part_number, chunk in enumerate(iter(lambda: source.read(part_size), b""), start=1):
            part_path = directory / f"{archive.stem}.part{part_number:03}.zip"
            part_path.write_bytes(chunk)
            parts.append(part_path)
    return parts


def append_file(source: Path, destination: Path) -> None:
    with source.open("rb") as input_file, destination.open("ab") as output_file:
        shutil.copyfileobj(input_file, output_file)
