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


def create_backup(database: Database, data_dir: Path, user_id: int) -> tuple[Path, str]:
    records = [dict(row) for row in database.list_gifs(user_id)]
    file_descriptor, archive_name = tempfile.mkstemp(prefix=f"{PREFIX}-{user_id}-", suffix=".zip")
    os.close(file_descriptor)
    archive = Path(archive_name)
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
            manifest = {
                "format": 1,
                "user_id": user_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "records": records,
            }
            zip_file.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
            for record in records:
                source = data_dir / record["file_path"]
                if source.is_file():
                    zip_file.write(source, f"media/{source.name}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return archive, digest
    except Exception:
        archive.unlink(missing_ok=True)
        raise


def restore_backup(database: Database, data_dir: Path, archive: Path, user_id: int, expected_digest: str | None = None) -> int:
    if expected_digest and hashlib.sha256(archive.read_bytes()).hexdigest() != expected_digest:
        raise ValueError("Backup checksum does not match")
    with tempfile.TemporaryDirectory() as temporary_dir:
        temporary_path = Path(temporary_dir)
        with zipfile.ZipFile(archive) as zip_file:
            names = set(zip_file.namelist())
            if "manifest.json" not in names or any(name.startswith("/") or ".." in Path(name).parts for name in names):
                raise ValueError("Invalid backup archive")
            zip_file.extractall(temporary_path)
        manifest = json.loads((temporary_path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != 1 or manifest.get("user_id") != user_id:
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
            normalized_record = dict(record)
            normalized_record["file_path"] = relative_path.as_posix()
            normalized_records.append(normalized_record)
        user_dir = data_dir / "gifs" / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        for record in normalized_records:
            relative_path = Path(str(record["file_path"]).replace("\\", "/"))
            source = temporary_path / "media" / relative_path.name
            shutil.copy2(source, data_dir / relative_path)
        database.replace_user_data(user_id, normalized_records)
        return len(normalized_records)
