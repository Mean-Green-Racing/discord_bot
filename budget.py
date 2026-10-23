"""SQLite ledger for Claude API spending and per-Discord-user prompt/token usage.

Stored prompts are LOCAL PLAINTEXT in data/usage.sqlite3. Keep the database
private. This bot only logs questions explicitly directed to Claude, not
ordinary channel chatter, trivia or free KB commands.

One row in `prompts` represents one Discord question; `calls` may contain
multiple Claude calls for that question (a retry after max_tokens, for example).
Older `calls` records without a prompt_id still count toward the global budget
but cannot be assigned retroactively to a Discord user.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
import sqlite3

# Pricing assumptions retained from the prior bot; see Anthropic for current rates.
INPUT_PER_MILLION = 0.10
OUTPUT_PER_MILLION = 0.50
CACHE_READ_PER_MILLION = 0.01
CACHE_WRITE_PER_MILLION = 0.125
RESERVE_PER_CALL_USD = 0.01


def utc_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class Budget:
    def __init__(self, database: Path, soft_limit_usd: float = 4.75) -> None:
        database = Path(database)
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.soft_limit_usd = min(4.95, max(0.05, soft_limit_usd))
        with self._connect() as con:
            # Existing database is NOT replaced or reset.
            con.execute("""CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                state TEXT NOT NULL,
                reserved_usd REAL NOT NULL,
                actual_usd REAL NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""")
            columns = {r[1] for r in con.execute("PRAGMA table_info(calls)")}
            if "prompt_id" not in columns:
                con.execute("ALTER TABLE calls ADD COLUMN prompt_id INTEGER")
            con.execute("""CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                created_at TEXT NOT NULL,
                discord_message_id INTEGER NOT NULL UNIQUE,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'received'
            )""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_prompts_guild_month_user "
                        "ON prompts(guild_id, month, user_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_calls_prompt_id "
                        "ON calls(prompt_id)")

    @contextmanager
    def _connect(self):
        # sqlite3.Connection as a context manager commits but does NOT close;
        # explicitly close it so the long-running bot doesn't leak file handles.
        con = sqlite3.connect(self.database, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def _spent(self, con: sqlite3.Connection) -> float:
        row = con.execute(
            """SELECT COALESCE(SUM(CASE WHEN state = 'completed'
                      THEN actual_usd ELSE reserved_usd END),0)
               FROM calls WHERE month = ?""", (utc_month(),)
        ).fetchone()
        return float(row[0] or 0)

    def record_prompt(self, *, message_id: int, guild_id: int,
                      channel_id: int, user_id: int, question: str) -> int:
        """Record the user's exact submitted question once (not KB excerpts)."""
        with self._connect() as con:
            con.execute(
                """INSERT OR IGNORE INTO prompts
                   (month,created_at,discord_message_id,guild_id,channel_id,
                    user_id,question,status)
                   VALUES(?,?,?,?,?,?,?,'received')""",
                (utc_month(), datetime.now(timezone.utc).isoformat(),
                 message_id, guild_id, channel_id, user_id, question),
            )
            row = con.execute(
                "SELECT id FROM prompts WHERE discord_message_id=?", (message_id,)
            ).fetchone()
        return int(row["id"])

    def mark_prompt(self, prompt_id: int, status: str) -> None:
        if status not in ("completed", "blocked", "error"):
            raise ValueError("Invalid prompt status")
        with self._connect() as con:
            con.execute("UPDATE prompts SET status=? WHERE id=?", (status, prompt_id))

    def reserve(self, prompt_id: int | None = None) -> int | None:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if self._spent(con) + RESERVE_PER_CALL_USD > self.soft_limit_usd:
                return None
            cursor = con.execute(
                """INSERT INTO calls (month,state,reserved_usd,created_at,prompt_id)
                   VALUES (?, 'pending', ?, ?, ?)""",
                (utc_month(), RESERVE_PER_CALL_USD,
                 datetime.now(timezone.utc).isoformat(), prompt_id),
            )
            return int(cursor.lastrowid)

    def complete(self, request_id: int, usage: object) -> tuple[int, int, float]:
        normal = int(getattr(usage, "input_tokens", 0) or 0)
        read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        output = int(getattr(usage, "output_tokens", 0) or 0)
        estimated = (
            normal * INPUT_PER_MILLION
            + read * CACHE_READ_PER_MILLION
            + write * CACHE_WRITE_PER_MILLION
            + output * OUTPUT_PER_MILLION
        ) / 1_000_000
        with self._connect() as con:
            con.execute(
                """UPDATE calls
                   SET state='completed', actual_usd=?, input_tokens=?, output_tokens=?
                   WHERE id=? AND state='pending'""",
                (estimated, normal + read + write, output, request_id),
            )
        return normal + read + write, output, estimated

    def uncertain(self, request_id: int) -> None:
        # If the request reached Anthropic, tokens may have been billed even
        # though no response reached us. Preserve the conservative reservation.
        with self._connect() as con:
            con.execute(
                "UPDATE calls SET state='uncertain' WHERE id=? AND state='pending'",
                (request_id,),
            )

    def stats(self) -> dict[str, float | int]:
        with self._connect() as con:
            spent = self._spent(con)
            row = con.execute(
                """SELECT COUNT(*), COALESCE(SUM(input_tokens),0),
                          COALESCE(SUM(output_tokens),0),
                          SUM(CASE WHEN state='uncertain' THEN 1 ELSE 0 END)
                   FROM calls WHERE month=?""", (utc_month(),)
            ).fetchone()
        return {
            "estimated_usd": spent,
            "calls": int(row[0]),
            "input_tokens": int(row[1]),
            "output_tokens": int(row[2]),
            "uncertain_calls": int(row[3] or 0),
            "limit_usd": self.soft_limit_usd,
        }

    @staticmethod
    def _time_clause(month: str | None) -> tuple[str, list]:
        if month is None:
            return "", []
        return " AND p.month=?", [month]

    def user_stats(self, guild_id: int, user_id: int,
                   month: str | None = "current") -> dict[str, int | float]:
        """All-time if month=None; current UTC month if month='current'."""
        actual_month = utc_month() if month == "current" else month
        clause, params = self._time_clause(actual_month)
        with self._connect() as con:
            row = con.execute(
                """SELECT COUNT(DISTINCT p.id) AS questions,
                          COALESCE(SUM(CASE WHEN c.state='completed'
                                   THEN c.input_tokens ELSE 0 END),0) AS inp,
                          COALESCE(SUM(CASE WHEN c.state='completed'
                                   THEN c.output_tokens ELSE 0 END),0) AS out,
                          COALESCE(SUM(CASE WHEN c.state='completed' THEN c.actual_usd
                                   WHEN c.state IN ('pending','uncertain')
                                   THEN c.reserved_usd ELSE 0 END),0) AS cost,
                          COUNT(CASE WHEN c.state='completed' THEN 1 END) AS calls,
                          COUNT(CASE WHEN c.state='uncertain' THEN 1 END) AS uncertain
                   FROM prompts p LEFT JOIN calls c ON c.prompt_id=p.id
                   WHERE p.guild_id=? AND p.user_id=?""" + clause,
                (guild_id, user_id, *params),
            ).fetchone()
        return {
            "questions": int(row["questions"] or 0),
            "input_tokens": int(row["inp"]),
            "output_tokens": int(row["out"]),
            "total_tokens": int(row["inp"] + row["out"]),
            "estimated_usd": float(row["cost"]),
            "calls": int(row["calls"]),
            "uncertain_calls": int(row["uncertain"]),
        }

    def leaderboard(self, guild_id: int, month: str | None = "current",
                    limit: int = 10) -> list[dict]:
        actual_month = utc_month() if month == "current" else month
        clause, params = self._time_clause(actual_month)
        with self._connect() as con:
            rows = con.execute(
                """SELECT p.user_id AS user_id,
                          COUNT(DISTINCT p.id) AS questions,
                          COALESCE(SUM(CASE WHEN c.state='completed'
                                   THEN c.input_tokens ELSE 0 END),0) AS inp,
                          COALESCE(SUM(CASE WHEN c.state='completed'
                                   THEN c.output_tokens ELSE 0 END),0) AS out,
                          COALESCE(SUM(CASE WHEN c.state='completed' THEN c.actual_usd
                                   WHEN c.state IN ('pending','uncertain')
                                   THEN c.reserved_usd ELSE 0 END),0) AS cost,
                          COUNT(CASE WHEN c.state='uncertain' THEN 1 END) AS uncertain
                   FROM prompts p LEFT JOIN calls c ON c.prompt_id=p.id
                   WHERE p.guild_id=?""" + clause + """
                   GROUP BY p.user_id
                   ORDER BY (inp + out) DESC, questions DESC, p.user_id ASC
                   LIMIT ?""",
                (guild_id, *params, min(max(1, limit), 25)),
            ).fetchall()
        return [{
            "user_id": int(r["user_id"]), "questions": int(r["questions"]),
            "input_tokens": int(r["inp"]), "output_tokens": int(r["out"]),
            "total_tokens": int(r["inp"] + r["out"]),
            "estimated_usd": float(r["cost"]),
            "uncertain_calls": int(r["uncertain"]),
        } for r in rows]
