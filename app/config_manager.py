import json
import os
from typing import Dict, Any

CONFIG_PATH = "/app/data/config.json"

DEFAULT_CONFIG = {
    "email": {
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
        "email_address": "",
        "app_password": "",
        "check_interval_minutes": 5
    },
    "anthropic_api_key": "",
    "subject_areas": [],
    "docmost": {
        "enabled": False,
        "db_host": "10.10.10.201",
        "db_password": "",
        "space_id": "0196753b-62a3-7d2b-8d23-473d8bd58bff"
    },
    "default_subject_area": "misc",
    "gmail_oauth": {
        "client_id": "",
        "client_secret": ""
    },
    "default_storage_path": "/mnt/documents/RabbitHole"
}


def load_config() -> Dict[str, Any]:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_PATH) as f:
        stored = json.load(f)
    config = DEFAULT_CONFIG.copy()
    config.update(stored)
    merged_email = DEFAULT_CONFIG["email"].copy()
    merged_email.update(config.get("email", {}))
    config["email"] = merged_email
    merged_dm = DEFAULT_CONFIG["docmost"].copy()
    merged_dm.update(config.get("docmost", {}))
    config["docmost"] = merged_dm
    merged_gm = DEFAULT_CONFIG["gmail_oauth"].copy()
    merged_gm.update(config.get("gmail_oauth", {}))
    config["gmail_oauth"] = merged_gm
    return config


def save_config(config: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def get_subject_area_path(config: Dict, subject_area: str) -> str:
    for sa in config.get("subject_areas", []):
        if sa["name"].lower() == subject_area.lower():
            return sa["path"]
    return os.path.join(
        config.get("default_storage_path", "/mnt/documents/RabbitHole"),
        subject_area
    )
