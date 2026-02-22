import json
import os
from typing import Any, Dict

from lib.cfg_schema import normalize_config


def load_json_file(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return default


def save_json_file(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if os.name != "nt":
        os.chmod(path, 0o600)


def load_config(config_path: str) -> Dict[str, Any]:
    data = load_json_file(config_path, {})
    norm, _errs = normalize_config(data)
    return norm
