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
    {
      "<uid>": {
        "mailtm": {
          "addr@domain": {"password": "...", "token": "...", "account_id": "...", "seen": [...]}
        },
        "zoho": {
          "addr@funmail.run": {"seen": [...]}
        }
      }
    }
    """

    def _get_user(self, uid: str) -> dict:
        data = _load().get(uid, {"mailtm": {}, "zoho": {}})
        # Migrate old "accounts" key to "mailtm"
        if "accounts" in data and "mailtm" not in data:
            data["mailtm"] = data.pop("accounts")
        elif "accounts" in data and "mailtm" in data:
            data.pop("accounts")
        data.setdefault("mailtm", {})
        data.setdefault("zoho", {})
        return data

    def _set_user(self, uid: str, data: dict) -> None:
        with _lock:
            d = _load()
            d[uid] = data
            _save(d)

    # ── mail.tm ───────────────────────────────────────────────────────────────

    def get_mailtm_accounts(self, uid: str) -> dict:
        return self._get_user(uid).get("mailtm", {})

    def get_all_mailtm_accounts(self) -> dict:
        data = _load()
        return {uid: v.get("mailtm", {}) for uid, v in data.items()}

    def add_mailtm_account(self, uid: str, address: str, password: str, token: str, account_id: str = "") -> None:
        u = self._get_user(uid)
        u.setdefault("mailtm", {})[address] = {
            "password": password, "token": token,
            "account_id": account_id, "seen": [],
        }
        self._set_user(uid, u)

    def remove_mailtm_account(self, uid: str, address: str) -> None:
        u = self._get_user(uid)
        u.get("mailtm", {}).pop(address, None)
        self._set_user(uid, u)

    # ── Zoho ──────────────────────────────────────────────────────────────────

    def get_zoho_addresses(self, uid: str) -> list[str]:
        return list(self._get_user(uid).get("zoho", {}).keys())

    def get_all_zoho_addresses(self) -> dict:
        data = _load()
        return {uid: list(v.get("zoho", {}).keys()) for uid, v in data.items()}

    def add_zoho_address(self, uid: str, address: str) -> None:
        u = self._get_user(uid)
        u.setdefault("zoho", {})[address] = {"seen": []}
        self._set_user(uid, u)

    def remove_zoho_address(self, uid: str, address: str) -> None:
        u = self._get_user(uid)
        u.get("zoho", {}).pop(address, None)
        self._set_user(uid, u)

    # ── Seen IDs ──────────────────────────────────────────────────────────────

    def get_known_ids(self, uid: str, address: str, source: str = "mailtm") -> set:
        u = self._get_user(uid)
        return set(u.get(source, {}).get(address, {}).get("seen", []))

    def add_known_id(self, uid: str, address: str, msg_id: str, source: str = "mailtm") -> None:
        with _lock:
            data = _load()
            try:
                seen = data[uid][source][address].setdefault("seen", [])
                if msg_id not in seen:
                    seen.append(msg_id)
                    data[uid][source][address]["seen"] = seen[-500:]
            except KeyError:
                pass
            _save(data)
