# Mean Green Bot — Hybrid Search Update (Linux / 1 GB RAM)

**Baseline:** Uses the newest FSAE trivia update (reaction answers, 300 `fsae_trivia.json` questions, 50/50 FSAE/vehicle mix), and the existing GitHub-SSH deploy-key integration and SQLite Claude budget tracking. The trivia, scores, reaction formatting, channel access, `!` commands, and spending limit are retained.

## Install without losing your settings or saved data

1. Stop the currently running bot.
2. **Back up** your existing bot directory, especially `.env`, `prompt.txt`, and `data/`.
3. Replace `bot.py` and **`kb.py`** with the two files from this update. Keep your existing `.env`, `prompt.txt`, `data/`, and SSH private key. The ZIP includes compatible `budget.py`, `trivia.py`, `fsae_trivia.json`, and `check_github.py` for a complete deployment, but you don't have to overwrite unchanged files.
4. If you haven't already installed the dependencies, run these commands on Linux in your bot directory:

   ```bash
   sudo apt-get update
   sudo apt-get install -y python3-venv git
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   python -m pip install --no-cache-dir -r requirements.txt
   ```

   On a very small VPS, run package installation with no other heavy processes. On Ubuntu/Debian with externally managed Python, the virtual environment avoids `externally-managed-environment` errors.

5. You can leave `.env` unchanged. Defaults are already safe for 1 GB Linux: semantic search on, **one ONNX CPU thread**, eight documents per batch. If you want explicit options, add:

   ```dotenv
   SEMANTIC_SEARCH=1
   EMBEDDING_THREADS=1
   EMBEDDING_BATCH_SIZE=8
   ```

6. **Optional, recommended for first setup:** while the bot is stopped, build its local embedding index:

   ```bash
   python warm_search.py
   ```

   This downloads an approximately **67 MB** model (plus tokenizer/supporting files) to `data/embedding_models/` and saves vectors to `data/semantic_index.npz`. The first download needs internet; later launches reuse the files. It connects to **your existing** private GitHub KB using your SSH deploy key; the embeddings are calculated locally. Never commit private `.env`, SSH key, or `data/`.

7. Run `python bot.py`. Check `!kb status`: **`Search: hybrid`** means semantic search is usable; **`keyword (building)`** means it's still indexing, and **`keyword (unavailable)`** means the model or dependency failed (see Linux terminal logs). Searches keep working in keyword mode even without FastEmbed.

## What's changed

- `kb.py` keeps the existing filename/heading/text keyword scoring and merges it with 384-dimensional local semantic similarity using rank fusion. The model is the small ONNX `BAAI/bge-small-en-v1.5`, not a big PyTorch model. It's a practical option for a 1 GB Linux VPS but absolute peak memory depends on the OS and other running services.
- Private KB documents are **never transmitted to any embedding API**. FastEmbed downloads **public model weights** once. The documents still go to Claude as selected excerpt text when someone uses `!ask` or mentions the bot, as they did before.
- Embeddings are **cached** on disk, validated against a SHA-256 digest of document contents, and rebuilt **only when indexed contents or the model configuration changes**. Initial indexing happens in the background, so Discord can still answer in keyword-only mode until ready.
- Questions like "What caused MGR to shut down?" can now match docs saying "dissolved during COVID" even with little literal overlap.
- `bot.py` offloads embedding inference to a worker thread so long queries don't freeze trivia reactions or the Discord connection.
- General open-ended requests ("why are we struggling?", "how can we improve?", "analyze our history") may retrieve from **up to 8 excerpts across 5 files**. Ordinary questions remain limited to **5 excerpts across 3 files** to limit Anthropic token consumption.
- `CORE_RULES` now prioritizes a useful answer, diagnosis, and practical steps while separating **confirmed evidence** from **inference**. It still avoids guessing rules or specs, uses excerpt citations, and keeps `prompt.txt` as the personality source.
- No new external embedding bill, no new Discord permissions, and no changes to the $4.75 spending guard or the trivia database.

## Commands (unchanged)

`!trivia`, `!trivia fsae`, `!trivia vehicles`, `!trivia leaderboard`, `!leaderboard`, `!leaderboard all`, `!usage`, `!kb status`, `!kb find <topic>`, `!kb refresh` (server managers), `!ask <question>`, and bot mentions.

`!kb refresh` refreshes the GitHub mirror and, if files changed, schedules re-indexing. A refresh response may appear before local semantic indexing finishes; `!kb status` shows progress.

## Files and troubleshooting

- **Persistent private files (keep):** `.env`, `prompt.txt`, `data/usage.sqlite3`, `data/trivia.sqlite3`, `data/fsae-kb/`, `data/embedding_models/`, `data/semantic_index.npz`, and your deploy key outside the repo.
- **Missing `fastembed` or unsupported CPU:** `!kb status` shows keyword fallback; reinstall requirements in your venv. Test with `python warm_search.py`.
- **Low memory:** start with `EMBEDDING_BATCH_SIZE=4`, `EMBEDDING_THREADS=1`. If the host still runs out of RAM, use `SEMANTIC_SEARCH=0` to continue in keyword-only mode. On some 1 GB machines, enabling a swap file may help initial setup, but it's not a substitute for adequate RAM.
- **Model cannot download:** a first-run public Hugging Face/model-host download is required. You can download once on a connected machine and transfer `data/embedding_models/`, or temporarily use keyword fallback.
- **Model is loaded per process:** don't run `warm_search.py` simultaneously with `bot.py` on a 1 GB machine.
- **Prompts:** the existing `bot.py` permits a max of 2,100 characters in `prompt.txt` and now reports that actual limit in its error message.

## Tests

`python3 -m unittest discover -s tests -v`

Offline tests cover the 50/50 trivia behavior, trivia data format, unchanged scores, index persistence/invalidation, hybrid fusion, fallback if local model is unavailable, and deeper retrieval. Tests use a deterministic fake semantic encoder because external model weights and live Discord credentials are not bundled. **Real model downloads, Linux memory usage, and live Discord interactions must be checked on your host.**
