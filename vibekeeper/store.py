import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory: Path, key: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.cipher = Fernet(key.encode())
        self.db = sqlite3.connect(directory / "vibekeeper.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS accounts(uid INTEGER PRIMARY KEY, token BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS oauth(nonce TEXT PRIMARY KEY, uid INTEGER, verifier TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, uid INTEGER, body BLOB, expires REAL);
            CREATE TABLE IF NOT EXISTS exports(draft TEXT, category TEXT, uid INTEGER, status TEXT,
                playlist TEXT, PRIMARY KEY(draft, category));
        """)
        self.purge_expired()

    def purge_expired(self):
        with self.db:
            self.db.execute("DELETE FROM oauth WHERE expires<?", (time.time(),))
            self.db.execute("DELETE FROM drafts WHERE expires<?", (time.time(),))
            self.db.execute("DELETE FROM exports WHERE draft NOT IN (SELECT id FROM drafts)")

    def _encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def _decrypt(self, value):
        return json.loads(self.cipher.decrypt(value))

    def account(self, uid):
        row = self.db.execute("SELECT token FROM accounts WHERE uid=?", (uid,)).fetchone()
        return self._decrypt(row[0]) if row else None

    def save_account(self, uid, token):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO accounts VALUES (?, ?)", (uid, self._encrypt(token)))

    def issue_state(self, uid, verifier):
        nonce = secrets.token_urlsafe(32)
        digest = hashlib.sha256(nonce.encode()).hexdigest()
        with self.db:
            self.db.execute("DELETE FROM oauth WHERE uid=? OR expires<?", (uid, time.time()))
            self.db.execute("INSERT INTO oauth VALUES (?, ?, ?, ?)", (digest, uid, verifier, time.time() + 600))
        return nonce

    def state_owner(self, nonce):
        digest = hashlib.sha256(nonce.encode()).hexdigest()
        row = self.db.execute("SELECT uid FROM oauth WHERE nonce=? AND expires>?", (digest, time.time())).fetchone()
        return row[0] if row else None

    def consume_state(self, nonce):
        digest = hashlib.sha256(nonce.encode()).hexdigest()
        with self.db:
            row = self.db.execute("SELECT * FROM oauth WHERE nonce=?", (digest,)).fetchone()
            self.db.execute("DELETE FROM oauth WHERE nonce=?", (digest,))
        return dict(row) if row and row["expires"] > time.time() else None

    def save_draft(self, uid, body):
        draft = secrets.token_hex(6)
        with self.db:
            self.db.execute("DELETE FROM drafts WHERE expires<?", (time.time(),))
            self.db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?)", (draft, uid, self._encrypt(body), time.time()+86400))
        return draft

    def draft(self, uid, draft):
        row = self.db.execute("SELECT body FROM drafts WHERE id=? AND uid=? AND expires>?", (draft, uid, time.time())).fetchone()
        return self._decrypt(row[0]) if row else None

    def reserve_export(self, uid, draft, category):
        with self.db:
            result = self.db.execute("INSERT OR IGNORE INTO exports VALUES (?, ?, ?, 'creating', NULL)", (draft, category, uid))
        return result.rowcount == 1

    def export(self, uid, draft, category):
        row = self.db.execute("SELECT * FROM exports WHERE uid=? AND draft=? AND category=?", (uid, draft, category)).fetchone()
        return dict(row) if row else None

    def export_status(self, uid, draft, category, status, playlist=None):
        with self.db:
            self.db.execute("UPDATE exports SET status=?, playlist=COALESCE(?,playlist) WHERE uid=? AND draft=? AND category=?",
                            (status, playlist, uid, draft, category))

    def disconnect(self, uid):
        with self.db:
            for table in ("accounts", "oauth", "drafts", "exports"):
                self.db.execute(f"DELETE FROM {table} WHERE uid=?", (uid,))

    def close(self):
        self.db.close()
