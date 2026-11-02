"""MGR FSAE / Vehicle trivia with private Discord buttons and group scoring.

Commands (no mention required):
  !trivia [easy|medium|hard]       Alternate FSAE / Vehicles rounds (50/50)
  !trivia fsae [difficulty]        Local FSAE / Mean Green Racing questions
  !trivia vehicles [difficulty]    Open Trivia DB Vehicles questions
  !trivia score                   Your existing points and accuracy
  !trivia leaderboard             Server leaderboard
  !trivia skip                    Starter or Manage Server can cancel a round
  !trivia help                    Show help

Everyone can answer once using buttons. A question lasts 60 seconds regardless
of the timeout passed by older bot.py configurations. No scores or correct
choices are announced until the minute ends; points depend on answer speed.
No Claude calls or Anthropic API tokens are used.
"""
from __future__ import annotations

import asyncio
import html
import hashlib
import json
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
ATTRIBUTION = "Questions: [Open Trivia DB](<https://opentdb.com/>) (CC BY-SA 4.0)"


def safe(text: str, limit: int = 450) -> str:
    """Safely render API text in Discord, without pinging anyone."""
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(html.unescape(text)[:limit])
    )


@dataclass(frozen=True)
class Guess:
    """Private answer with elapsed time captured at the moment of submission."""
    letter: str
    elapsed: float


@dataclass
class Round:
    question: str
    difficulty: str
    options: dict[str, str]
    correct_letter: str
    starter: int
    guild_id: int
    source: str = "vehicles"
    guesses: dict[int, Guess] = field(default_factory=dict)
    credited: dict[int, int] = field(default_factory=dict)
    deadline: float = 0.0
    deadline_epoch: int = 0
    started: float = 0.0
    message: discord.Message | None = None
    view: TriviaView | None = None
    timer: asyncio.Task | None = None
    edit_task: asyncio.Task | None = None
    pending_update: bool = False
    result: str = "active"  # active / expired / skipped / setup_error
    save_failed: bool = False
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class TriviaAnswerButton(discord.ui.Button):
    """Choice buttons use interaction callbacks; no message reactions needed."""

    def __init__(self, trivia: TriviaGame, game: Round, letter: str):
        super().__init__(label=letter, style=discord.ButtonStyle.primary)
        self.trivia = trivia
        self.game = game
        self.letter = letter

    async def callback(self, interaction: discord.Interaction):
        await self.trivia.handle_button(interaction, self.game, self.letter)


class TriviaView(discord.ui.View):
    def __init__(self, trivia: TriviaGame, game: Round):
        # Never use View's sliding timeout for competition timing: interactions
        # extend it. The monotonic deadline and countdown task enforce 60 sec.
        super().__init__(timeout=None)
        self.game = game
        for letter in LETTERS:
            self.add_item(TriviaAnswerButton(trivia, game, letter))

    def disable_choices(self):
        for item in self.children:
            item.disabled = True
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"Trivia button callback error: {type(error).__name__}: {error}")
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(
                    "Could not register that guess. Please try again.", ephemeral=True
                )
            except discord.HTTPException:
                pass


class TriviaGame:
    def __init__(self, db_path: str | Path, timeout_seconds: int = 60, tick_seconds: int = 5,
                 fsae_questions_path: str | Path | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Older bot.py versions pass TRIVIA_TIMEOUT_SECONDS=15. The new
        # competition is always one minute, even if bot.py still passes 15.
        self.timeout_seconds = 60
        self.fsae_questions_path = (Path(fsae_questions_path) if fsae_questions_path
                                    else Path(__file__).with_name("fsae_trivia.json"))
        self._fsae_cache: list[dict] = []
        self._fsae_mtime: int | None = None
        # Keep this old argument for compatibility. Discord's <t:...:R>
        # timestamp supplies a live countdown; only vote changes need edits.
        self.tick_seconds = max(2, int(tick_seconds))
        self.active: dict[int, Round] = {}
        self.loading: set[int] = set()
        self.state_lock = asyncio.Lock()
        self.fetch_lock = asyncio.Lock()
        self.next_api_time = 0.0
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
        # Accept both the newer flat array and the older JSON format with
        # easy/medium/hard arrays (plus an optional about section).
        if isinstance(raw, dict):
            raw = [question for level in POINTS
                   for question in raw.get(level, [])
                   if isinstance(question, dict)]
        if not isinstance(raw, list):
            raise RuntimeError("FSAE trivia JSON must contain question objects")
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

    @staticmethod
    def _speed_points(difficulty: str, elapsed: float) -> int:
        """Correct answers earn 1x-2x the original difficulty points.

        At t=0: 2x. At t=30s: 1.5x. At t=60s: 1x.
        Wrong answers always earn zero, regardless of speed.
        """
        base = POINTS[difficulty]
        elapsed = max(0.0, min(60.0, elapsed))
        return base + int(base * (60.0 - elapsed) / 60.0 + 0.5)

    def _save_round_answers(self, game: Round) -> None:
        """Commit every participant's score together when the round ends."""
        rows = []
        for user_id, guess in game.guesses.items():
            correct = guess.letter == game.correct_letter
            points = self._speed_points(game.difficulty, guess.elapsed) if correct else 0
            if correct:
                game.credited[user_id] = points
            rows.append((game.guild_id, user_id, points, int(correct)))
        if not rows:
            return
        with closing(self._connect()) as con, con:
            con.executemany("""INSERT INTO scores(guild_id,user_id,points,correct,attempts)
                VALUES(?,?,?,?,1)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET
                  points=points+excluded.points,
                  correct=correct+excluded.correct,
                  attempts=attempts+1""", rows)

    async def _say(self, message, content: str):
        await message.channel.send(content, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _truncated_mentions(users: list[int], max_count: int = 34) -> str:
        shown = users[:max_count]
        result = ", ".join(f"<@{uid}>" for uid in shown) or "Nobody yet"
        if len(users) > len(shown):
            result += f" ... and {len(users) - len(shown)} more"
        return result

    def _render(self, game: Round) -> discord.Embed:
        """Build a fresh embed from current state, never expose guesses early."""
        title = f"{'FSAE' if game.source == 'fsae' else 'VEHICLE'} TRIVIA | {game.difficulty.upper()}"
        options = "\n".join(
            f"**{letter}.** {safe(answer, 140)}" for letter, answer in game.options.items()
        )
        description = f"**{safe(game.question)}**\n\n{options}"
        if game.result == "active":
            description += (
                f"\n\n**Time remaining:** <t:{game.deadline_epoch}:R>"
                f"\nClick A, B, C or D below. One private guess per player."
                f"\nCorrect answers earn **{POINTS[game.difficulty]}–{2 * POINTS[game.difficulty]} pts**"
                " depending on response speed. Results reveal after 60 seconds."
            )
            color = discord.Color.blurple()
        elif game.result == "expired":
            description += (
                f"\n\n**Time's up!** Correct answer: "
                f"**{game.correct_letter}. {safe(game.options[game.correct_letter], 140)}**"
            )
            color = discord.Color.green()
        elif game.result == "skipped":
            description += (
                f"\n\n**Round cancelled — no points awarded.**\nCorrect answer: "
                f"**{game.correct_letter}. {safe(game.options[game.correct_letter], 140)}**"
            )
            color = discord.Color.orange()
        else:
            description += "\n\nCould not start this round. Check the bot's Send Messages / Embed Links permissions."
            color = discord.Color.red()

        embed = discord.Embed(title=title, description=description, color=color)
        if game.result == "active":
            ids = list(game.guesses)
            embed.add_field(
                name=f"Answered ({len(ids)})",
                value=self._truncated_mentions(ids),
                inline=False,
            )
        elif game.result == "expired":
            winners = sorted(
                ((user_id, guess.elapsed, game.credited[user_id])
                 for user_id, guess in game.guesses.items() if user_id in game.credited),
                key=lambda record: (record[1], record[0])
            )
            if winners:
                # Discord limits individual embed fields to 1024 characters.
                # Show the fastest 70 directly; all players receive points.
                # The single-embed display is limited by Discord's API size caps.
                result_lines = [
                    f"{i}. <@{uid}> — **+{pts} pts** ({elapsed:.1f}s)"
                    for i, (uid, elapsed, pts) in enumerate(winners[:70], 1)
                ]
                pages = []
                current = ""
                for line in result_lines:
                    if current and len(current) + len(line) + 1 > 990:
                        pages.append(current)
                        current = line
                    else:
                        current = f"{current}\n{line}" if current else line
                if current:
                    pages.append(current)
                for number, chunk in enumerate(pages, 1):
                    embed.add_field(
                        name="Correct answers" if number == 1 else "Correct answers (continued)",
                        value=chunk, inline=False
                    )
                if len(winners) > 70:
                    embed.add_field(name="Additional winners",
                                    value=f"{len(winners) - 70} additional correct players were scored.",
                                    inline=False)
            else:
                embed.add_field(name="Correct answers", value="Nobody got it right.", inline=False)
            incorrect = [uid for uid, guess in game.guesses.items()
                         if guess.letter != game.correct_letter]
            if incorrect:
                embed.add_field(name=f"Incorrect ({len(incorrect)})",
                                value=self._truncated_mentions(incorrect), inline=False)
            if not game.guesses:
                embed.add_field(name="Participation", value="Nobody submitted an answer.", inline=False)
            if game.save_failed:
                embed.add_field(name="Score error",
                                value="Could not save scores. Contact a server manager.", inline=False)
        elif game.result == "skipped" and game.guesses:
            embed.add_field(name=f"Answered before cancellation ({len(game.guesses)})",
                            value=self._truncated_mentions(list(game.guesses)), inline=False)
        if game.source == "vehicles":
            embed.add_field(name="Question source",
                            value="[Open Trivia DB](https://opentdb.com/) (CC BY-SA 4.0)", inline=False)
        if game.result != "active":
            embed.set_footer(text="Type !trivia to play again.")
        return embed

    async def _edit(self, game: Round):
        if game.message is None:
            return
        async with game.edit_lock:
            try:
                await game.message.edit(
                    embed=self._render(game), view=game.view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                print(f"Trivia message edit failed: {exc}")

    def _queue_edit(self, game: Round):
        """Coalesce bursts of guesses into edits; Discord has rate limits."""
        game.pending_update = True
        if game.edit_task is None or game.edit_task.done():
            game.edit_task = asyncio.create_task(self._edit_after_votes(game))

    async def _edit_after_votes(self, game: Round):
        try:
            while game.pending_update and game.result == "active":
                game.pending_update = False
                await asyncio.sleep(0.7)
                if game.result != "active":
                    return
                await self._edit(game)
        except asyncio.CancelledError:
            return

    async def _finish(self, channel_id: int, game: Round, outcome: str):
        """Close once, award once, disable buttons, and reveal results."""
        async with self.state_lock:
            if game.result != "active" or self.active.get(channel_id) is not game:
                return False
            game.result = outcome
            del self.active[channel_id]
            if outcome == "expired":
                try:
                    self._save_round_answers(game)
                except sqlite3.Error as exc:
                    game.save_failed = True
                    print(f"Trivia scoring failed: {exc}")
            if game.view is not None:
                game.view.disable_choices()
            if game.timer is not None and game.timer is not asyncio.current_task():
                game.timer.cancel()
            if game.edit_task is not None and game.edit_task is not asyncio.current_task():
                game.edit_task.cancel()
        await self._edit(game)
        return True

    async def _countdown(self, channel_id: int, game: Round):
        try:
            await asyncio.sleep(max(0.0, game.deadline - time.monotonic()))
            await self._finish(channel_id, game, "expired")
        except asyncio.CancelledError:
            return

    async def _start(self, message, difficulty: str | None, source: str | None = None):
        channel_id = message.channel.id
        async with self.state_lock:
            busy = channel_id in self.loading or channel_id in self.active
            if not busy:
                self.loading.add(channel_id)
        if busy:
            await self._say(message, "A trivia question is already active here. Use its answer buttons.")
            return

        game: Round | None = None
        try:
            async with message.channel.typing():
                game = await self._choose_question(difficulty, message.guild.id, message.author.id, source)
            if game is None:
                await self._say(message, "No trivia questions available for that difficulty right now.")
                return
            game.started = time.monotonic()
            game.deadline = game.started + self.timeout_seconds
            game.deadline_epoch = int(time.time() + self.timeout_seconds)
            game.view = TriviaView(self, game)
            game.message = await message.channel.send(
                embed=self._render(game), view=game.view,
                allowed_mentions=discord.AllowedMentions.none()
            )
            async with self.state_lock:
                self.active[channel_id] = game
                self.loading.discard(channel_id)
            game.timer = asyncio.create_task(self._countdown(channel_id, game))
        except Exception as exc:
            if game is not None:
                async with self.state_lock:
                    if self.active.get(channel_id) is game:
                        del self.active[channel_id]
                    if game.timer:
                        game.timer.cancel()
                    if game.view:
                        game.view.disable_choices()
            print(f"Trivia startup error: {type(exc).__name__}: {exc}")
            try:
                await self._say(message, "Could not start trivia. Check the bot permissions and try again.")
            except discord.HTTPException:
                pass
        finally:
            async with self.state_lock:
                self.loading.discard(channel_id)

    async def handle_button(self, interaction: discord.Interaction,
                            game: Round, letter: str):
        """Lock in one guess per player; answer correctness stays private."""
        if getattr(interaction.user, "bot", False):
            await interaction.response.send_message("Bots cannot play trivia.", ephemeral=True)
            return
        status = "closed"
        overdue = False
        async with self.state_lock:
            channel_id = interaction.channel_id
            if (self.active.get(channel_id) is game and game.result == "active"
                    and interaction.guild_id == game.guild_id
                    and game.message is not None
                    and interaction.message is not None
                    and interaction.message.id == game.message.id):
                now = time.monotonic()
                if now >= game.deadline:
                    overdue = True
                elif interaction.user.id in game.guesses:
                    status = "duplicate"
                elif letter in game.options:
                    game.guesses[interaction.user.id] = Guess(
                        letter=letter, elapsed=max(0.0, now - game.started)
                    )
                    status = "accepted"
        if status == "accepted":
            answer = f"Answer **{letter}** locked in. Results reveal when the minute ends."
        elif status == "duplicate":
            answer = "You've already submitted an answer this round. One guess per player."
        else:
            answer = "This trivia round has ended. Type `!trivia` for a new one."
        try:
            await interaction.response.send_message(
                answer, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            print(f"Trivia private acknowledgment failed: {exc}")
        if status == "accepted":
            self._queue_edit(game)
        if overdue:
            await self._finish(channel_id, game, "expired")

    async def handle_reaction(self, payload) -> bool:
        """Compatibility with older bot.py on_raw_reaction_add handlers."""
        return False

    async def _skip(self, message):
        async with self.state_lock:
            game = self.active.get(message.channel.id)
            if game is None:
                error = "No trivia question is active here."
            elif (message.author.id != game.starter
                  and not message.author.guild_permissions.manage_guild):
                error = "Only the round starter or a server manager can skip it."
            else:
                error = ""
        if error:
            await self._say(message, error)
        else:
            await self._finish(message.channel.id, game, "skipped")

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
        """Handle trivia commands before Claude; button clicks use their view."""
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
                    "Click A/B/C/D buttons to answer. Everyone has 60 seconds; one guess each.\n"
                    "`!trivia score` · `!trivia leaderboard` · `!trivia skip` · `!trivia help`")
            elif re.fullmatch(r"(?:answer\s+)?[a-d]", arg, flags=re.IGNORECASE):
                await self._say(message, "Tap the A/B/C/D buttons on the active trivia question.")
            else:
                await self._say(message, "Unknown trivia command. Try `!trivia help`.")
            return True
        return False
