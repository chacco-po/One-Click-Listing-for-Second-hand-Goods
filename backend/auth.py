"""用户认证（登录/注册）与会话管理。

安全设计（不引入额外依赖，全部用标准库）：
- 密码：PBKDF2-HMAC-SHA256 + 随机盐，120000 轮迭代，数据库不存明文
- 会话：服务端会话表 + secrets.token_urlsafe 随机 token，可即时吊销
- 客户端通过 `Authorization: Bearer <token>` 携带；服务端校验过期时间
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSION_DAYS = 7
PBKDF2_ROUNDS = 120_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  salt          TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


class AuthError(Exception):
    """认证失败（用户名/密码错误、用户不存在等）。"""

    def __init__(self, message: str, code: str = "AUTH_FAILED"):
        super().__init__(message)
        self.message = message
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (salt, hash_hex)。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return salt, digest.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, digest = hash_password(password, salt)
    return secrets.compare_digest(digest, expected_hash)


def validate_credentials(username: str, password: str) -> None:
    """入参校验（边界处验证，非法直接抛错）。"""
    username = (username or "").strip()
    if not (3 <= len(username) <= 20) or not username.replace("_", "").isalnum():
        raise AuthError("用户名需为 3-20 位字母、数字或下划线", "BAD_USERNAME")
    if not (6 <= len(password or "") <= 64):
        raise AuthError("密码长度需为 6-64 位", "BAD_PASSWORD")


class AuthRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _session(self):
        """确保连接及时关闭（避免长连接占用文件锁）。"""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------------- 用户 ----------------

    def create_user(self, username: str, password: str) -> dict:
        username = username.strip()
        validate_credentials(username, password)
        with self._session() as conn:
            exists = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
            if exists:
                raise AuthError("该用户名已被注册", "USERNAME_TAKEN")
            salt, digest = hash_password(password)
            user_id = secrets.token_hex(8)
            conn.execute(
                "INSERT INTO users (id, username, password_hash, salt, created_at) VALUES (?,?,?,?,?)",
                (user_id, username, digest, salt, _now().isoformat(timespec="seconds")),
            )
        return {"id": user_id, "username": username}

    def get_user_by_name(self, username: str) -> sqlite3.Row | None:
        with self._session() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()

    # ---------------- 会话 ----------------

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        expires = now + timedelta(days=SESSION_DAYS)
        with self._session() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (token, user_id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
            )
        return token

    def get_user_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self._session() as conn:
            row = conn.execute(
                "SELECT u.id, u.username, s.expires_at FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        try:
            if datetime.fromisoformat(row["expires_at"]) < _now():
                self.delete_session(token)
                return None
        except ValueError:
            return None
        return {"id": row["id"], "username": row["username"]}

    def delete_session(self, token: str) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def cleanup_expired(self) -> int:
        with self._session() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now().isoformat(),))
            return cur.rowcount
