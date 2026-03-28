import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "mathbox.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    api_key     TEXT NOT NULL,
    registered_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS groups (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    joined_at   DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS group_members (
    chat_id     INTEGER NOT NULL REFERENCES groups(chat_id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    joined_at   DATETIME DEFAULT (datetime('now')),
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS usage_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    chat_id         INTEGER NOT NULL REFERENCES groups(chat_id) ON DELETE CASCADE,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    cache_write     INTEGER NOT NULL DEFAULT 0,
    recorded_at     DATETIME DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_usage_user_date
    ON usage_log (user_id, recorded_at);

CREATE INDEX IF NOT EXISTS idx_usage_chat_date
    ON usage_log (chat_id, recorded_at);
"""


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── CRUD helpers ──────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: str | None, api_key: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, api_key)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET api_key=excluded.api_key,
                                               username=excluded.username
            """,
            (user_id, username, api_key),
        )


def get_user(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def upsert_group(chat_id: int, title: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO groups (chat_id, title)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title
            """,
            (chat_id, title),
        )


def add_member(chat_id: int, user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO group_members (chat_id, user_id) VALUES (?, ?)
            """,
            (chat_id, user_id),
        )


def get_group_registered_members(chat_id: int) -> list[sqlite3.Row]:
    """그룹 멤버 중 API 키가 등록된 유저만 반환."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT u.user_id, u.username, u.api_key
            FROM group_members gm
            JOIN users u ON u.user_id = gm.user_id
            WHERE gm.chat_id = ?
            """,
            (chat_id,),
        ).fetchall()


def insert_usage(
    user_id: int,
    chat_id: int,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO usage_log
                (user_id, chat_id, input_tokens, output_tokens, cache_read, cache_write)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, chat_id, input_tokens, output_tokens, cache_read, cache_write),
        )


def get_daily_usage(chat_id: int) -> list[sqlite3.Row]:
    """오늘(UTC 기준) 해당 그룹의 멤버별 누적 토큰 사용량."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT
                u.username,
                ul.user_id,
                SUM(ul.input_tokens)  AS total_input,
                SUM(ul.output_tokens) AS total_output,
                SUM(ul.cache_read)    AS total_cache_read,
                SUM(ul.cache_write)   AS total_cache_write,
                SUM(ul.input_tokens + ul.output_tokens) AS total_tokens
            FROM usage_log ul
            JOIN users u ON u.user_id = ul.user_id
            WHERE ul.chat_id = ?
              AND date(ul.recorded_at) = date('now')
            GROUP BY ul.user_id
            ORDER BY total_tokens DESC
            """,
            (chat_id,),
        ).fetchall()
