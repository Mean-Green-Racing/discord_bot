"""Download and index the MGR knowledge base with visible progress.

Activate your virtual environment and run:
    python3 -u warm_search.py

This is optional. It builds an on-disk local semantic-search index before
starting the Discord bot. Run it while the Discord bot is stopped on a 1 GB host.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import time

from dotenv import load_dotenv
from kb import KnowledgeBase

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def report(message: str) -> None:
    print(message, flush=True)


async def watch(task: asyncio.Task, label: str, interval: int = 10) -> None:
    """Report that a long external Git operation or model download is alive."""
    started = time.monotonic()
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except asyncio.TimeoutError:
            elapsed = round(time.monotonic() - started)
            report(f"  Still working on {label} ({elapsed}s elapsed)...")
    await task  # Re-raise any genuine error, rather than reporting success.


async def main() -> None:
    report("MGR Knowledge Base: search setup")
    report(f"Working folder: {ROOT}")
    report("Make sure the Discord bot is stopped while indexing on a 1 GB server.")

    kb = KnowledgeBase(
        folder=ROOT / "data" / "fsae-kb",
        repo_url=os.getenv("REPO_URL", "git@github.com:Mean-Green-Racing/fsae-kb.git"),
        branch=os.getenv("REPO_BRANCH", "main"),
        ssh_key_path=os.getenv("GITHUB_DEPLOY_KEY_PATH", ""),
        refresh_seconds=int(os.getenv("SYNC_INTERVAL_SECONDS", "600")),
        semantic_enabled=os.getenv("SEMANTIC_SEARCH", "1").lower() in ("1", "true", "yes"),
        embedding_threads=int(os.getenv("EMBEDDING_THREADS", "1")),
        embedding_batch_size=int(os.getenv("EMBEDDING_BATCH_SIZE", "8")),
    )

    report("\n[1/3] Connecting to GitHub and indexing repository documents...")
    sync_task = asyncio.create_task(kb.ensure_ready())
    await watch(sync_task, "GitHub sync / document indexing")
    report(f"  Indexed {kb.file_count} files at Git commit {kb.commit[:10] or 'unknown'}.")
    if kb.last_error:
        report(f"  WARNING: GitHub sync failed; cached documents were used: {kb.last_error}")

    report("\n[2/3] Preparing local semantic-search index...")
    if not kb.semantic_enabled:
        report("  Skipped: SEMANTIC_SEARCH=0 (keyword search only).")
    elif kb.semantic_status == "ready":
        report("  Existing index loaded from disk. No model download needed.")
    elif kb._semantic_task is not None:
        report("  First run may download model weights, then embed document passages.")
        report("  Model directory: " + str(kb.embedding_cache_dir))
        report("  If this takes a while, progress will be printed every 10 seconds.")
        await watch(kb._semantic_task, "model download / embedding index")
    else:
        report(f"  Current status: {kb.semantic_status} (no build was scheduled).")

    report(f"  Semantic search: {kb.semantic_status}")
    if kb.semantic_last_error:
        report(f"  WARNING: {kb.semantic_last_error}")
        report("  The Discord bot can still use keyword search as a fallback.")

    report("\n[3/3] Testing two sample searches...")
    for question in (
        "Why did Mean Green Racing shut down?",
        "What did we learn from F26 competition?",
    ):
        report(f"\n  Query: {question}")
        hits = await kb.search_async(question)
        if not hits:
            report("    No matching passages found.")
        for hit in hits:
            report(f"    - {hit.passage.path} ({hit.passage.location})")
    report("\nDone. Start your bot with: python3 bot.py")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        report("\nInterrupted. Re-run this script to finish indexing.")
        raise SystemExit(130)
    except Exception as exc:
        report(f"\nERROR: {type(exc).__name__}: {exc}")
        report("Check .env, SSH key/GitHub access, and Python dependencies.")
        raise SystemExit(1)
