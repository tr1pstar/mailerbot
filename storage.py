import json
import os
from threading import Lock

DATA_FILE = "data.json"
_lock = Lock()


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class Storage:
    """
    Structure:
    {
      "<user_id>": {
        "accounts": {
          "user@domain.com": {
            "password": "...",
            "token": "...",
            "account_id": "...",
            "seen": ["msg_id1", ...]
          }
        }
      }
    }
    """

    def _get_user(self, uid: str) -> dict:
        return _load().get(uid, {"accounts": {}})

    def _set_user(self, uid: str, user_data: dict) -> None:
        with _lock:
            data = _load()
            data[uid] = user_data
            _save(data)

    def get_accounts(self, uid: str) -> dict:
        return self._get_user(uid).get("accounts", {})

    def get_all_accounts(self) -> dict:
        data = _load()
        return {uid: v.get("accounts", {}) for uid, v in data.items()}

    def add_account(self, uid: str, address: str, password: str, token: str, account_id: str = "") -> None:
        user = self._get_user(uid)
        user.setdefault("accounts", {})[address] = {
            "password": password,
            "token": token,
            "account_id": account_id,
            "seen": [],
        }
        self._set_user(uid, user)

    def remove_account(self, uid: str, address: str) -> None:
        user = self._get_user(uid)
        user.get("accounts", {}).pop(address, None)
        self._set_user(uid, user)

    def update_token(self, uid: str, address: str, token: str) -> None:
        with _lock:
            data = _load()
            try:
                data[uid]["accounts"][address]["token"] = token
            except KeyError:
                pass
            _save(data)

    def get_known_ids(self, uid: str, address: str) -> set:
        user = self._get_user(uid)
        return set(user.get("accounts", {}).get(address, {}).get("seen", []))

    def add_known_id(self, uid: str, address: str, msg_id: str) -> None:
        with _lock:
            data = _load()
            try:
                seen = data[uid]["accounts"][address].setdefault("seen", [])
                if msg_id not in seen:
                    seen.append(msg_id)
                    data[uid]["accounts"][address]["seen"] = seen[-500:]
            except KeyError:
                pass
            _save(data)
