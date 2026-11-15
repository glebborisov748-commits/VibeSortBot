import hashlib
import json
import secrets
import sqlite3
import time
import unicodedata
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
            CREATE TABLE IF NOT EXISTS library(id TEXT PRIMARY KEY, uid INTEGER, body BLOB, created REAL);
            CREATE INDEX IF NOT EXISTS library_owner ON library(uid, created);
            CREATE TABLE IF NOT EXISTS settings(uid INTEGER PRIMARY KEY, body BLOB);
            CREATE TABLE IF NOT EXISTS playlist_cache(id TEXT PRIMARY KEY, body BLOB, created REAL);
            CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY, uid INTEGER, next_run REAL, status TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS genre_overrides(uid INTEGER, fingerprint TEXT, body BLOB, PRIMARY KEY(uid, fingerprint));
        """)
        self.purge_expired()

    def purge_expired(self):
        with self.db:
            self.db.execute("DELETE FROM oauth WHERE expires<?", (time.time(),))
            self.db.execute("DELETE FROM drafts WHERE expires<?", (time.time(),))
            self.db.execute("DELETE FROM exports WHERE draft NOT IN (SELECT id FROM drafts) AND NOT (draft LIKE 'library:%' AND substr(draft, 9) IN (SELECT id FROM library))")
            self.db.execute("DELETE FROM playlist_cache WHERE created<?", (time.time()-86400,))

    def save_library(self, uid, body):
        identity = secrets.token_hex(12)
        with self.db:
            self.db.execute("INSERT INTO library VALUES (?, ?, ?, ?)", (identity, uid, self._encrypt(body), time.time()))
        return identity

    def library(self, uid, identity=None):
        if identity:
            row = self.db.execute("SELECT body FROM library WHERE id=? AND uid=?", (identity, uid)).fetchone()
            return self._decrypt(row[0]) if row else None
        rows = self.db.execute("SELECT id, body, created FROM library WHERE uid=? ORDER BY created DESC LIMIT 50", (uid,)).fetchall()
        result = []
        for row in rows:
            body = self._decrypt(row["body"])
            result.append({"id": row["id"], "created": row["created"], "name": body.get("name", "Мои подборки"),
                           "scanned": body.get("scanned", 0), "source_platform":body.get('source_platform'),
                           "sync":self.subscription(uid,row['id']),
                           "categories": {k:len(v) for k,v in body.get("groups", {}).items()}})
        return result

    def delete_library(self, uid, identity):
        with self.db:
            self.db.execute("DELETE FROM library WHERE uid=? AND id=?", (uid, identity))
            self.db.execute("DELETE FROM subscriptions WHERE uid=? AND id=?", (uid, identity))

    def subscription(self, uid, identity, enabled=None):
        if enabled is not None:
            if not self.library(uid, identity):
                return None
            with self.db:
                if enabled:
                    self.db.execute("INSERT OR IGNORE INTO subscriptions VALUES (?, ?, ?, 'waiting', '')", (identity, uid, time.time()+21600))
                else:
                    self.db.execute("DELETE FROM subscriptions WHERE uid=? AND id=?", (uid, identity))
        row=self.db.execute("SELECT next_run,status,error FROM subscriptions WHERE uid=? AND id=?", (uid,identity)).fetchone()
        return {"enabled":True,**dict(row)} if row else {"enabled":False}

    def due_subscriptions(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM subscriptions WHERE next_run<=? ORDER BY next_run LIMIT 1",(time.time(),)).fetchall()]

    def subscription_result(self, uid, identity, error="", delay=21600):
        with self.db:
            self.db.execute("UPDATE subscriptions SET next_run=?,status=?,error=? WHERE uid=? AND id=?",
                            (time.time()+delay,'error' if error else 'waiting',error[:350],uid,identity))

    def update_library(self, uid, identity, body):
        with self.db:
            return self.db.execute("UPDATE library SET body=? WHERE uid=? AND id=?", (self._encrypt(body), uid, identity)).rowcount == 1

    def settings(self, uid, body=None):
        if body is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (uid, self._encrypt(body)))
        row = self.db.execute("SELECT body FROM settings WHERE uid=?", (uid,)).fetchone()
        return self._decrypt(row[0]) if row else {"ai_fallback": True}

    def track_genre(self, uid, track, genre=None):
        text=", ".join(track.artists)+"\0"+track.title
        normalized=" ".join(unicodedata.normalize('NFKC',text).casefold().split())
        fingerprint=hashlib.sha256(normalized.encode()).hexdigest()
        if genre is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO genre_overrides VALUES (?, ?, ?)',(uid,fingerprint,self._encrypt(genre)))
        row=self.db.execute('SELECT body FROM genre_overrides WHERE uid=? AND fingerprint=?',(uid,fingerprint)).fetchone()
        return self._decrypt(row[0]) if row else None

    def cache_playlist(self, identity, body=None):
        if body is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO playlist_cache VALUES (?, ?, ?)", (identity, self._encrypt(body), time.time()))
        row = self.db.execute("SELECT body FROM playlist_cache WHERE id=? AND created>?", (identity, time.time()-86400)).fetchone()
        return self._decrypt(row[0]) if row else None

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
            for table in ("accounts", "oauth", "drafts", "exports", "library", "settings", "subscriptions", "genre_overrides"):
                self.db.execute(f"DELETE FROM {table} WHERE uid=?", (uid,))

    def close(self):
        self.db.close()
