"""Load backend-only data configuration without importing simulation modules."""
from pathlib import Path

from dotenv import load_dotenv


def load_database_env():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
