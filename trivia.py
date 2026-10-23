"""FSAE / vehicle trivia with answers via reactions on the ORIGINAL Discord message.

Commands (no @mention necessary):
  !trivia [easy|medium|hard]  Alternate FSAE / Vehicles rounds
  !trivia fsae [difficulty]  Only local Mean Green Racing / FSAE questions
  !trivia vehicles [difficulty] Only Open Trivia DB vehicle questions
  !trivia score              Show your score
  !trivia leaderboard        Show server leaderboard
  !trivia skip               End the round (starter or Manage Server)
  !trivia help               Show commands

The original question message displays a countdown, and is edited for wrong
attempts, winners, and timeouts. Nothing here calls Claude or spends tokens.
"""
from __future__ import annotations

import asyncio
import html
import hashlib
import json
import math
import random
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import discord

API_URL = "https://opentdb.com/api.php"
VEHICLES_CATEGORY = 28
BATCH_SIZE = 25
API_INTERVAL = 5.3  # OpenTDB limits clients to one API request per 5 seconds.
POINTS = {"easy": 10, "medium": 20, "hard": 30}
LETTERS = "ABCD"
REACTIONS = {"A": "🇦", "B": "🇧", "C": "🇨", "D": "🇩"}
REACTION_TO_LETTER = {emoji: letter for letter, emoji in REACTIONS.items()}
ATTRIBUTION = "Questions: [Open Trivia DB](<https://opentdb.com/>) (CC BY-SA 4.0)"


def safe(text: str, limit: int = 450) -> str:
    """Safely render API text in Discord, without pinging anyone."""
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(html.unescape(text)[:limit])
    )


@dataclass
class Round:
    question: str
    difficulty: str
    options: dict[str, str]
    correct_letter: str
    starter: int
    guild_id: int
    source: str = "vehicles"
    attempts: set[int] = field(default_factory=set)
    wrong_users: list[int] = field(default_factory=list)
    deadline: float = 0.0
    deadline_epoch: int = 0
    message: discord.Message | None = None
    timer: asyncio.Task | None = None
    result: str = "active"  # active / correct / expired / skipped / setup_error
    winner_id: int | None = None
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class TriviaGame:
    def __init__(self, db_path: str | Path, timeout_seconds: int = 15, tick_seconds: int = 5,
                 fsae_questions_path: str | Path | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = max(10, int(timeout_seconds))
        self.fsae_questions_path = (Path(fsae_questions_path) if fsae_questions_path
                                    else Path(__file__).with_name("fsae_trivia.json"))
        self._fsae_cache: list[dict] = []
        self._fsae_mtime: int | None = None
        # 5-second updates, rather than 1-second edits, avoid unnecessary Discord API calls.
        self.tick_seconds = max(2, int(tick_seconds))
        self.active: dict[int, Round] = {}
        self.loading: set[int] = set()
        self.state_lock = asyncio.Lock()
        self.fetch_lock = asyncio.Lock()
        self.next_api_time = 0.0
        self._warned_missing_manage_messages = False
        self._init_db()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self):
        with closing(self._connect()) as con, con:
            con.execute("""CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY,
                question TEXT NOT NULL UNIQUE,
                correct TEXT NOT NULL,
                answers_json TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS scores (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                correct INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""")
            # A separate history keeps FSAE questions from repeating until
            # the selected difficulty's pool has been exhausted.
            con.execute("""CREATE TABLE IF NOT EXISTS fsae_seen (
                question_key TEXT PRIMARY KEY,
                difficulty TEXT NOT NULL
            )""")
            # Next source persists across restarts. Alternating gives an exact
            # 50/50 split across every pair of default rounds per server.
            con.execute("""CREATE TABLE IF NOT EXISTS trivia_source_rotation (
                guild_id INTEGER PRIMARY KEY,
                next_source TEXT NOT NULL
            )""")

    def _has_unused(self, difficulty: str | None) -> bool:
        with closing(self._connect()) as con:
            if difficulty:
                row = con.execute(
                    "SELECT 1 FROM questions WHERE used=0 AND difficulty=? LIMIT 1",
                    (difficulty,),
                ).fetchone()
            else:
                row = con.execute("SELECT 1 FROM questions WHERE used=0 LIMIT 1").fetchone()
        return row is not None

    def _save_batch(self, results: list[dict]) -> int:
        inserted = 0
        with closing(self._connect()) as con, con:
            for item in results:
                if item.get("type") != "multiple" or item.get("category") != "Vehicles":
                    continue
                level = item.get("difficulty", "")
                if level not in POINTS:
                    continue
                prompt = html.unescape(str(item.get("question", ""))).strip()
                right = html.unescape(str(item.get("correct_answer", ""))).strip()
                wrong = [html.unescape(str(s)).strip() for s in item.get("incorrect_answers", [])]
                answers = [right, *wrong]
                if (not prompt or len(prompt) > 450 or len(wrong) != 3
                        or any(not s or len(s) > 140 for s in answers)
                        or len({s.casefold() for s in answers}) != 4):
                    continue
                cursor = con.execute(
                    "INSERT OR IGNORE INTO questions(question,correct,answers_json,difficulty) "
                    "VALUES(?,?,?,?)",
                    (prompt, right, json.dumps(answers, ensure_ascii=False), level),
                )
                inserted += cursor.rowcount
        return inserted

    async def _refill(self, difficulty: str | None):
        async with self.fetch_lock:
            if self._has_unused(difficulty):
                return
            params = {"amount": BATCH_SIZE, "category": VEHICLES_CATEGORY, "type": "multiple"}
            if difficulty:
                params["difficulty"] = difficulty
            for attempt in range(2):
                delay = self.next_api_time - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self.next_api_time = time.monotonic() + API_INTERVAL
                try:
                    timeout = aiohttp.ClientTimeout(total=15)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(API_URL, params=params) as response:
                            data = await response.json(content_type=None)
                            if response.status == 429:
                                code = 5
                            elif response.status != 200:
                                raise RuntimeError(f"Open Trivia DB returned HTTP {response.status}")
                            else:
                                code = data.get("response_code")
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    raise RuntimeError(f"Could not reach Open Trivia DB: {exc}") from exc
                if code == 0:
                    self._save_batch(data.get("results", []))
                    return
                if code == 1:
                    return
                if code == 5 and attempt == 0:
                    continue
                raise RuntimeError(f"Open Trivia DB response code {code}; try again later")

    def _take_cached(self, difficulty: str | None, recycle: bool = False):
        clause = " AND difficulty=?" if difficulty else ""
        params = (difficulty,) if difficulty else ()
        with closing(self._connect()) as con, con:
            row = con.execute(
                "SELECT * FROM questions WHERE used=0" + clause + " ORDER BY RANDOM() LIMIT 1",
                params,
            ).fetchone()
            if row is None and recycle:
                con.execute("UPDATE questions SET used=0 WHERE 1=1" + clause, params)
                row = con.execute(
                    "SELECT * FROM questions WHERE used=0" + clause + " ORDER BY RANDOM() LIMIT 1",
                    params,
                ).fetchone()
            if row is not None:
                con.execute("UPDATE questions SET used=1 WHERE id=?", (row["id"],))
        return dict(row) if row is not None else None

    async def _choose_vehicle_question(self, difficulty: str | None, guild_id: int, user_id: int) -> Round | None:
        row = self._take_cached(difficulty)
        if row is None:
            try:
                await self._refill(difficulty)
            except RuntimeError:
                row = self._take_cached(difficulty, recycle=True)
                if row is None:
                    raise
            if row is None:
                row = self._take_cached(difficulty, recycle=True)
        if row is None:
            return None
        values: list[str] = json.loads(row["answers_json"])
        random.shuffle(values)
        choices = dict(zip(LETTERS, values))
        right_letter = next(letter for letter, value in choices.items() if value == row["correct"])
        return Round(row["question"], row["difficulty"], choices, right_letter,
                     user_id, guild_id, source="vehicles")

    def _fsae_bank(self) -> list[dict]:
        """Load the flat JSON question bank on first use and after file edits."""
        try:
            mtime = self.fsae_questions_path.stat().st_mtime_ns
        except OSError as exc:
            raise RuntimeError(f"Missing FSAE question file: {self.fsae_questions_path}") from exc
        if mtime == self._fsae_mtime:
            return self._fsae_cache
        try:
            raw = json.loads(self.fsae_questions_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot load FSAE trivia JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise RuntimeError("FSAE trivia JSON must be a flat array of question objects")
        parsed = []
        for item in raw:
            if not isinstance(item, dict) or item.get("difficulty") not in POINTS:
                continue
            q = item.get("question")
            choices = item.get("choices")
            ix = item.get("answerIndex")
            if not (isinstance(q, str) and 0 < len(q) <= 450
                    and isinstance(choices, list) and len(choices) == 4
                    and all(isinstance(c, str) and 0 < len(c) <= 140 for c in choices)
                    and len({c.casefold() for c in choices}) == 4
                    and type(ix) is int and 0 <= ix <= 3):
                continue
            parsed.append(item)
        if not parsed:
            raise RuntimeError("No valid FSAE trivia questions in the JSON file")
        self._fsae_mtime = mtime
        self._fsae_cache = parsed
        return parsed

    @staticmethod
    def _fsae_key(item: dict) -> str:
        return hashlib.sha256(
            (item["difficulty"] + "\0" + item["question"]).encode("utf-8")
        ).hexdigest()

    def _choose_fsae_question(self, difficulty: str | None,
                              guild_id: int, user_id: int) -> Round | None:
        pool = [item for item in self._fsae_bank()
                if difficulty is None or item["difficulty"] == difficulty]
        if not pool:
            return None
        with closing(self._connect()) as con, con:
            seen = {row[0] for row in con.execute("SELECT question_key FROM fsae_seen")}
            unseen = [item for item in pool if self._fsae_key(item) not in seen]
            if not unseen:
                # Recycle only after all eligible questions have appeared.
                if difficulty is None:
                    con.execute("DELETE FROM fsae_seen")
                else:
                    con.execute("DELETE FROM fsae_seen WHERE difficulty=?", (difficulty,))
                unseen = pool
            selected = random.choice(unseen)
            con.execute("INSERT OR REPLACE INTO fsae_seen(question_key,difficulty) VALUES(?,?)",
                        (self._fsae_key(selected), selected["difficulty"]))
        answers = list(selected["choices"])
        correct_answer = answers[selected["answerIndex"]]
        random.shuffle(answers)
        choices = dict(zip(LETTERS, answers))
        correct_letter = next(letter for letter, value in choices.items()
                              if value == correct_answer)
        return Round(selected["question"], selected["difficulty"], choices,
                     correct_letter, user_id, guild_id, source="fsae")

    def _next_source(self, guild_id: int) -> str:
        """Flip between FSAE and Vehicles after each automatic question request."""
        with closing(self._connect()) as con, con:
            row = con.execute("SELECT next_source FROM trivia_source_rotation WHERE guild_id=?",
                              (guild_id,)).fetchone()
            source = row[0] if row is not None else "fsae"
            following = "vehicles" if source == "fsae" else "fsae"
            con.execute("""INSERT INTO trivia_source_rotation(guild_id,next_source)
                VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET
                next_source=excluded.next_source""", (guild_id, following))
        return source

    async def _choose_question(self, difficulty: str | None, guild_id: int,
                               user_id: int, source: str | None = None) -> Round | None:
        """Alternate bank/API in normal mode, falling back if one is unavailable."""
        preferred = source or self._next_source(guild_id)
        sources = [preferred] if source else [preferred, "vehicles" if preferred == "fsae" else "fsae"]
        failures = []
        for candidate in sources:
            try:
                game = (self._choose_fsae_question(difficulty, guild_id, user_id)
                        if candidate == "fsae"
                        else await self._choose_vehicle_question(difficulty, guild_id, user_id))
            except RuntimeError as exc:
                failures.append(f"{candidate}: {exc}")
                continue
            if game is not None:
                return game
        if failures:
            raise RuntimeError("; ".join(failures))
        return None

    def _record_answer(self, guild_id: int, user_id: int, correct: bool, points: int):
        with closing(self._connect()) as con, con:
            con.execute("""INSERT INTO scores(guild_id,user_id,points,correct,attempts)
                VALUES(?,?,?,?,1)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET
                  points=points+excluded.points,
                  correct=correct+excluded.correct,
                  attempts=attempts+1""", (guild_id, user_id, points, int(correct)))

    async def _say(self, message, content: str):
        await message.channel.send(content, allowed_mentions=discord.AllowedMentions.none())

    def _render(self, game: Round) -> str:
        options = "\n".join(
            f"{REACTIONS[letter]} **{letter}.** {safe(answer, 140)}"
            for letter, answer in game.options.items()
        )
        content = (
            f"**{'FSAE TRIVIA' if game.source == 'fsae' else 'VEHICLE TRIVIA'} | {game.difficulty.upper()}** "
            f"({POINTS[game.difficulty]} points)\n\n"
            f"{safe(game.question)}\n\n{options}\n\n"
        )
        if game.result == "active":
            seconds = max(0, math.ceil(game.deadline - time.monotonic()))
            minutes, seconds = divmod(seconds, 60)
            content += (
                f"(ends <t:{game.deadline_epoch}:R>)\n"
                "React to this message with 🇦 🇧 🇨 or 🇩. "
                "One guess per player; first correct reaction wins."
            )
        elif game.result == "correct":
            content += (
                f"🏁 **<@{game.winner_id}> got it right!** "
                f"+{POINTS[game.difficulty]} points.\n"
                f"Correct answer: **{game.correct_letter}. "
                f"{safe(game.options[game.correct_letter], 140)}**"
            )
        elif game.result in ("expired", "skipped"):
            label = "⏰ Time's up!" if game.result == "expired" else "⏭️ Round skipped."
            content += (
                f"{label}\nCorrect answer: **{game.correct_letter}. "
                f"{safe(game.options[game.correct_letter], 140)}**"
            )
        else:
            content += "Cannot start this round: please grant the bot **Add Reactions** and **Read Message History**."

        if game.wrong_users:
            # Keep the original message below the 2,000-character Discord limit.
            hidden_count = max(0, len(game.wrong_users) - 7)
            recent = game.wrong_users[-7:]
            content += "\n\n**Wrong guesses:**\n"
            if hidden_count:
                content += f"...and {hidden_count} earlier wrong guess(es).\n"
            content += "\n".join(f"<@{user_id}> got it wrong." for user_id in recent)

        if game.source == "vehicles":
            content += f"\n\n*{ATTRIBUTION}*"
        if game.result != "active":
            content += "\nType `!trivia` to play again."
        return content

    async def _edit(self, game: Round):
        if game.message is None:
            return
        async with game.edit_lock:
            try:
                # Re-render inside the lock, so a queued countdown never undoes a win.
                await game.message.edit(
                    content=self._render(game),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                print(f"Trivia message edit failed: {exc}")

    async def _remove_player_reaction(self, game: Round, emoji: str, user_id: int):
        """Hide the chosen emoji after recording it (needs Manage Messages)."""
        if game.message is None:
            return
        try:
            await game.message.remove_reaction(emoji, discord.Object(id=user_id))
        except discord.Forbidden:
            if not self._warned_missing_manage_messages:
                print("Trivia privacy warning: grant the bot Manage Messages to remove player reactions. "
                      "Without it, users can see which answer someone selected.")
                self._warned_missing_manage_messages = True
        except discord.HTTPException as exc:
            print(f"Could not remove a trivia guess reaction: {exc}")

    async def _countdown(self, channel_id: int, game: Round):
        try:
            while True:
                remaining = game.deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(min(self.tick_seconds, remaining))
                expired = False
                async with self.state_lock:
                    if self.active.get(channel_id) is not game:
                        return
                    if time.monotonic() >= game.deadline:
                        del self.active[channel_id]
                        game.result = "expired"
                        expired = True
                await self._edit(game)
                if expired:
                    return
        except asyncio.CancelledError:
            return

    async def _start(self, message, difficulty: str | None, source: str | None = None):
        channel_id = message.channel.id
        async with self.state_lock:
            busy = channel_id in self.loading or channel_id in self.active
            if not busy:
                self.loading.add(channel_id)
        if busy:
            await self._say(message, "A trivia question is already active here. React to its original message.")
            return

        game: Round | None = None
        try:
            async with message.channel.typing():
                game = await self._choose_question(difficulty, message.guild.id, message.author.id, source)
            if game is None:
                await self._say(message, "No trivia questions available for that difficulty right now.")
                return
            game.deadline = time.monotonic() + self.timeout_seconds
            game.deadline_epoch = int(time.time() + self.timeout_seconds)
            game.message = await message.channel.send(
                self._render(game), allowed_mentions=discord.AllowedMentions.none()
            )
            async with self.state_lock:
                self.active[channel_id] = game
                self.loading.discard(channel_id)
            game.timer = asyncio.create_task(self._countdown(channel_id, game))
            try:
                for emoji in REACTIONS.values():
                    await game.message.add_reaction(emoji)
            except discord.HTTPException as exc:
                print(f"Trivia reaction initialization failed: {exc}")
                async with self.state_lock:
                    if self.active.get(channel_id) is game:
                        del self.active[channel_id]
                        game.result = "setup_error"
                if game.timer:
                    game.timer.cancel()
                await self._edit(game)
        except Exception as exc:
            async with self.state_lock:
                if game is not None and self.active.get(channel_id) is game:
                    del self.active[channel_id]
                if game is not None and game.timer:
                    game.timer.cancel()
            print(f"Trivia startup error: {type(exc).__name__}: {exc}")
            try:
                await self._say(message, "Could not load vehicle trivia. Try `!trivia` again later.")
            except discord.HTTPException:
                pass
        finally:
            async with self.state_lock:
                self.loading.discard(channel_id)

    async def handle_reaction(self, payload) -> bool:
        """Handle only reactions to the bot's currently active question message."""
        choice = REACTION_TO_LETTER.get(str(payload.emoji))
        if choice is None:
            return False
        game = self.active.get(payload.channel_id)
        if (game is None or game.message is None
                or payload.message_id != game.message.id
                or payload.guild_id != game.guild_id
                or getattr(payload.member, "bot", False)):
            return False

        changed = False
        async with self.state_lock:
            if self.active.get(payload.channel_id) is not game:
                return False
            if time.monotonic() >= game.deadline:
                del self.active[payload.channel_id]
                game.result = "expired"
                if game.timer:
                    game.timer.cancel()
                changed = True
            elif payload.user_id not in game.attempts:
                game.attempts.add(payload.user_id)
                correct = choice == game.correct_letter
                points = POINTS[game.difficulty] if correct else 0
                self._record_answer(game.guild_id, payload.user_id, correct, points)
                if correct:
                    del self.active[payload.channel_id]
                    game.result = "correct"
                    game.winner_id = payload.user_id
                    if game.timer:
                        game.timer.cancel()
                else:
                    game.wrong_users.append(payload.user_id)
                changed = True

        # Remove the participant's reaction so Discord doesn't permanently show
        # who selected which letter. It may still be briefly visible to others.
        await self._remove_player_reaction(game, str(payload.emoji), payload.user_id)
        if changed:
            await self._edit(game)
        return True

    async def _skip(self, message):
        game = None
        async with self.state_lock:
            game = self.active.get(message.channel.id)
            if game is None:
                error = "No trivia question is active here."
            elif (message.author.id != game.starter
                  and not message.author.guild_permissions.manage_guild):
                error = "Only the round starter or a server manager can skip it."
            else:
                del self.active[message.channel.id]
                game.result = "skipped"
                if game.timer:
                    game.timer.cancel()
                error = ""
        if error:
            await self._say(message, error)
        else:
            await self._edit(game)

    async def _score(self, message):
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT points,correct,attempts FROM scores WHERE guild_id=? AND user_id=?",
                (message.guild.id, message.author.id),
            ).fetchone()
        if row is None:
            await self._say(message, "Your trivia score is 0. Type `!trivia` to start.")
        else:
            await self._say(message, f"Your trivia score: **{row['points']} points** | "
                            f"{row['correct']} correct / {row['attempts']} attempts.")

    async def _leaderboard(self, message):
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT user_id,points,correct FROM scores WHERE guild_id=? "
                "ORDER BY points DESC, correct DESC, user_id LIMIT 10",
                (message.guild.id,),
            ).fetchall()
        if not rows:
            await self._say(message, "No trivia scores yet. Type `!trivia` to start.")
            return
        lines = ["**MGR TRIVIA LEADERBOARD**"]
        lines += [f"{i}. <@{r['user_id']}> — **{r['points']} pts** ({r['correct']} correct)"
                  for i, r in enumerate(rows, 1)]
        await self._say(message, "\n".join(lines))

    async def handle_message(self, message, bot_id: int) -> bool:
        """Handle trivia commands before routing to Claude. Only reactions count."""
        clean = re.sub(rf"<@!?{bot_id}>", "", message.content).strip()
        # Commands require a leading !. Never treat ordinary chat as commands.
        if clean.casefold() in ("!leaderboard trivia", "!trivialeaderboard"):
            await self._leaderboard(message)
            return True
        command = re.fullmatch(r"!trivia(?:\s+(.+))?", clean, flags=re.IGNORECASE)
        if command:
            arg = (command.group(1) or "").strip().lower()
            parts = arg.split()
            if (arg in ("", "next")
                    or (parts and len(parts) <= 2
                        and all(p in POINTS or p in ("fsae", "vehicles", "next") for p in parts)
                        and sum(p in POINTS for p in parts) <= 1
                        and sum(p in ("fsae", "vehicles") for p in parts) <= 1)):
                difficulty = next((p for p in parts if p in POINTS), None)
                source = next((p for p in parts if p in ("fsae", "vehicles")), None)
                await self._start(message, difficulty, source)
            elif arg in ("leaderboard", "lb", "top"):
                await self._leaderboard(message)
            elif arg in ("score", "stats"):
                await self._score(message)
            elif arg == "skip":
                await self._skip(message)
            elif arg in ("help", "commands"):
                await self._say(message,
                    "`!trivia` — alternate FSAE and Vehicles questions (50/50)\n"
                    "`!trivia easy|medium|hard` — choose difficulty\n"
                    "`!trivia fsae [easy|medium|hard]` — FSAE only\n"
                    "`!trivia vehicles [easy|medium|hard]` — OpenTDB only\n"
                    "React 🇦 🇧 🇨 🇩 to answer (one guess per player).\n"
                    "`!trivia score` · `!trivia leaderboard` · `!trivia skip` · `!trivia help`")
            elif re.fullmatch(r"(?:answer\s+)?[a-d]", arg, flags=re.IGNORECASE):
                await self._say(message, "Answers are reactions now. Tap 🇦 🇧 🇨 or 🇩 on the trivia question.")
            else:
                await self._say(message, "Unknown trivia command. Try `!trivia help`.")
            return True
        return False
