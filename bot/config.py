from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    token: str
    data_dir: Path
    allowed_user_ids: frozenset[int]
    max_file_size: int

    @classmethod
    def from_environment(cls) -> "Config":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("BOT_TOKEN is required")

        raw_ids = os.environ.get("ALLOWED_USER_IDS", "").strip()
        if not raw_ids:
            raise ValueError("ALLOWED_USER_IDS must contain at least one Telegram user ID")
        try:
            allowed_ids = frozenset(int(value.strip()) for value in raw_ids.split(",") if value.strip())
        except ValueError as error:
            raise ValueError("ALLOWED_USER_IDS must be comma-separated integers") from error
        if not allowed_ids:
            raise ValueError("ALLOWED_USER_IDS must contain at least one Telegram user ID")

        default_data_dir = Path(__file__).resolve().parent.parent / "data"
        return cls(
            token=token,
            data_dir=Path(os.environ.get("DATA_DIR", default_data_dir)),
            allowed_user_ids=allowed_ids,
            max_file_size=int(os.environ.get("MAX_FILE_SIZE", str(50 * 1024 * 1024))),
        )
