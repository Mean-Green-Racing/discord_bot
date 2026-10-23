# Mean Green Racing Discord knowledge bot (SSH deploy key)

This version connects to `Mean-Green-Racing/fsae-kb` **using SSH and a repository deploy key**, not a GitHub personal access token. It retains the local `AGENTS.md`-guided retrieval, automatic Git updates, Haiku 5.5 responses, per-user cooldown, and local $4.75/month spending guard.

## 1. Replace the bot files

The ZIP contains all necessary files. If installing into your existing working `mgr bot` folder, stop the bot and **replace `bot.py` and `kb.py` together**. Also copy `check_github.py` for the connection test. Keep your existing `budget.py`, `prompt.txt`, `.env`, and `data/` folder; do not overwrite your own `.env`.

```bash
cd "/Users/school/Downloads/mgr bot"
python3 -m pip install -r requirements.txt
```

## 2. Set up the private half of the deploy key on your Mac

GitHub stores the **public half** of a deploy key in repository Settings → Deploy keys. The bot must use the *matching private key file* on your Mac. GitHub cannot provide the private half to you. If the deploy key already exists in GitHub and you have the matching private key, copy that private file safely to `~/.ssh/fsae_kb_deploy` and skip key generation. Never upload the private key to Discord, GitHub, or Claude.

If you **do not have** the matching private key, generate a new pair locally:

```bash
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -f "$HOME/.ssh/fsae_kb_deploy" -C "mgr-discord-kb" -N ""
chmod 600 "$HOME/.ssh/fsae_kb_deploy"
cat "$HOME/.ssh/fsae_kb_deploy.pub"
```

Add the **public `.pub` key text** to `https://github.com/Mean-Green-Racing/fsae-kb` → Settings → Deploy keys → Add deploy key. Keep **Allow write access unchecked**. You must have permission to modify the repository's deploy keys. If the repo already has an unrelated deploy key, you may add a separate one (each key can only be used for one repository).

Security note: `-N ""` creates a key without a passphrase for unattended operation; protect the Mac and the key file. A passphrase-protected key can work if it has been unlocked in the SSH agent before starting the bot.

## 3. Establish GitHub SSH host trust

The code deliberately uses `StrictHostKeyChecking=yes` and won't automatically accept an unknown server. Run this interactive test **once** from your Mac terminal:

```bash
ssh -T -i "$HOME/.ssh/fsae_kb_deploy" -o IdentitiesOnly=yes git@github.com
```

If SSH prompts to trust `github.com`, **verify the displayed ED25519 fingerprint** against GitHub's official documentation before answering `yes`. At the time of writing, GitHub's documented Ed25519 fingerprint is:

```text
SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
```

Source: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints

An SSH reply such as "successfully authenticated, but GitHub does not provide shell access" indicates SSH authentication succeeded. `ssh -T` can exit with code 1 even on success.

## 4. Configure `.env`

Keep your existing Discord and Anthropic API keys. Add or update the following lines:

```dotenv
DISCORD_TOKEN=YOUR_EXISTING_DISCORD_TOKEN
ANTHROPIC_API_KEY=YOUR_EXISTING_ANTHROPIC_API_KEY
ALLOWED_GUILD_ID=1562934894852579511

GITHUB_DEPLOY_KEY_PATH=~/.ssh/fsae_kb_deploy
REPO_URL=git@github.com:Mean-Green-Racing/fsae-kb.git
REPO_BRANCH=main
SYNC_INTERVAL_SECONDS=600
```

If you previously configured `GITHUB_TOKEN=`, delete that old line; it is no longer used. If you forget to change your old HTTPS `REPO_URL`, the script automatically translates a recognized GitHub HTTPS URL to SSH. **Point `GITHUB_DEPLOY_KEY_PATH` to the private file, never the `.pub` file.** The bot checks for secure private-key permissions (`chmod 600` on macOS/Linux).

If you moved the bot to a different Discord server, set that server's ID in `ALLOWED_GUILD_ID`. You may optionally restrict it to particular channels with `ALLOWED_CHANNEL_IDS=id1,id2`.

## 5. Verify the deploy key without Claude credits

```bash
python3 check_github.py
```

This runs `git ls-remote` over SSH, confirms access to `main`, and **uses no Claude API tokens**. Successful output should look like:

```text
GitHub SSH deploy-key authentication successful for git@github.com:Mean-Green-Racing/fsae-kb.git
Branch main: <commit-hash>
```

Then start the real bot:

```bash
python3 bot.py
```

Files are cloned into `data/fsae-kb/` and refreshed approximately every 10 minutes when requests arrive. If the folder already contains a checkout of the same repo with an HTTPS origin, the bot safely updates its local `origin` to SSH before fetching. It refuses to silently replace an unrelated Git remote.

## 6. Discord commands and retrieval behavior

- `@Mean Green Bot kb status`: indexed files, current commit and estimated monthly API spend, free.
- `@Mean Green Bot kb find precharge`: local AGENTS.md-guided document search, free.
- `@Mean Green Bot kb refresh`: force a repository fetch; requires Manage Server permission.
- `@Mean Green Bot What are our precharge requirements?`: retrieves up to five short passages from at most three KB files, then asks Claude and includes source links.

Local Git fetch, indexing, `AGENTS.md` routing, and text search use **zero Claude tokens**. Only the question and selected excerpts are sent to Anthropic. By default the bot limits normal output to 850 tokens, enforces a 15-second per-user cooldown, and tracks API consumption in `data/usage.sqlite3`. A local estimated spending guard stops around $4.75/month; also set a **$5 spend limit in your Anthropic workspace**, because local tracking cannot cover other API use or all billing conditions.

The bot intentionally does not push changes to GitHub. Do not hand-edit the cache under `data/fsae-kb/`, because an update resets the mirror to the remote branch. The key remains on your machine; it is never sent to Claude. For a private KB, restrict access to Discord channels authorized to receive its contents.

## 7. Troubleshooting

| Error | What to check |
|---|---|
| `Set GITHUB_DEPLOY_KEY_PATH` | Add the local *private* key path to `.env`. |
| `Deploy private key not found` | Confirm the exact filename under `~/.ssh/`. |
| `unsafe permissions` | Run `chmod 600 ~/.ssh/fsae_kb_deploy`. |
| `Host key verification failed` | Establish SSH trust after checking GitHub's official fingerprint. |
| `Permission denied (publickey)` | Verify the `.pub` key is registered in the correct repository's Deploy keys and matches your private key. |
| `Repository not found` | Verify the SSH repository URL and permission to read that repo. |
| No documents found | Run `@Mean Green Bot kb refresh`, then `kb status` and `kb find <topic>`; inspect AGENTS.md and supported file types. |

Unit tests can be run with `python3 -m pytest` after `python3 -m pip install pytest`.
