# Mean Green Bot — Mermaid Gantt + Manager Source Limit

This is an **incremental update** to the hybrid-search, reaction-trivia version of Mean Green Bot. It uses the latest `kb.py` that produces short `[filename](https://github.com/.../blob/main/...)` source links.

## Install (existing bot, 1 GB Linux server)

Stop the running bot, back up your existing `.env`, `prompt.txt`, `bot.py`, `kb.py` and `data/` folder, and **copy these four files** into `/root/discord_bot`:

- `bot.py` — Discord command routing and PNG chart uploads
- `kb.py` — configurable search result limits, same hybrid keyword + semantic logic, same short GitHub `main` source links
- `bot_settings.py` — new; writes the saved manager setting to `data/bot_settings.json`
- `mermaid_gantt.py` — new; scans complete Markdown files and renders supported Gantt code locally

Keep your existing `budget.py`, `trivia.py`, `fsae_trivia.json`, `.env`, `prompt.txt`, `data/usage.sqlite3`, `data/trivia.sqlite3`, existing semantic index and deploy key unchanged. You **do not** need to run `warm_search.py` again or rebuild embeddings.

Inside your existing virtual environment:

```bash
cd /root/discord_bot
source .venv/bin/activate
python3 -m pip install --no-cache-dir 'Pillow>=10,<13'
python3 -u bot.py
```

The supplied `requirements.txt` documents the full set of compatible dependencies for clean installs. For an existing install you only need the extra Pillow dependency.

**Discord bot permissions:** Attach Files, Send Messages, Read Message History, and existing message-content/reaction permissions. Server managers need **Manage Server** permission to change the source limit; ordinary members can see and use charts.

## Commands

| Command | Who | Cost | Behavior |
|---|---|---|---|
| `!kb gantt` | Everyone | 0 Claude tokens | Display the default project Mermaid Gantt chart as a PNG |
| `!kb gantt suspension` | Everyone | 0 Claude tokens | Find the most relevant Mermaid Gantt for Suspension |
| `!kb gantt list` | Everyone | 0 Claude tokens | List source Markdown charts and clickable short links |
| `@Mean Green Bot show the gantt chart for aero` | Everyone | 0 Claude tokens | Render the original chart from the KB |
| `!kb sources` | Everyone | 0 Claude tokens | Display current limit (default: **3** distinct files) |
| `!kb sources 6` | **Manage Server** only | 0 Claude tokens | Save new limit, from **1 to 10** sources, across restarts |
| `!kb status` | Everyone | 0 Claude tokens | Now also displays `Sources: N` |

**One "source" means one distinct document/file**, not one chunk. The search system can retrieve up to **two excerpts per file** and is capped at **18,000 total excerpt characters** even when set to 10 sources. Requesting 10 means "up to 10 relevant files", not a guarantee that 10 exist.

Short masked source links still point to the KB's configured GitHub branch (`main` by default), with **no commit hash or line numbers**.

## Mermaid Gantt support and accuracy

Discord doesn't natively draw Mermaid in messages. The bot renders the chart to a local PNG using Pillow and attaches it as an image (nothing sent to a third-party chart-rendering website). It reads the entire Markdown file for code fences matching:

    ```mermaid
    gantt
      title F27 design
      dateFormat YYYY-MM-DD
      section Suspension
      Geometry :crit, geo, 2026-10-18, 14d
      Bell Cranks :active, bell, after geo, 7d
      Design Gate :milestone, gate, 2026-12-05, 0d
    ```

The local renderer supports section labels, explicit dates, durations in whole days/weeks, `after` dependencies, `until` references, `done`, `active`, `crit`, `milestone`, and `excludes weekends` (plus supported explicit dates). It is a **lightweight visual approximation of Mermaid**, not the complete Mermaid engine; it doesn't reproduce every Mermaid style or advanced date feature. If the chart needs unsupported syntax, the bot will show a clickable GitHub source link **instead of inventing dates**. You can view the original chart with full Mermaid support on GitHub.

If an AI response happens to include a Mermaid Gantt code block, the bot also tries to attach a rendered PNG; that AI-generated chart is labeled as such. The original Markdown chart command is preferable for verified schedule information.

## Verification and regression tests

```bash
python3 -m unittest discover -s tests -v
```

Tests cover hybrid search, source-count bounds and persistence, code-fence extraction from long Markdown files, schedule dependencies, PNG output, permissions, budget tracking, trivia, and command routing. Local test fixtures are synthetic; this package does not contain or connect to the private FSAE repository. Live Discord permissions and your exact Gantt syntax should be checked on your host.
