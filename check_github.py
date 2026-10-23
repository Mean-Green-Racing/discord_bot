"""Verify read-only GitHub SSH access without starting Discord or using Claude."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from kb import KnowledgeBase

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
branch = os.getenv("REPO_BRANCH", "main")
kb = KnowledgeBase(
    folder=ROOT / "data" / "fsae-kb",
    repo_url=os.getenv("REPO_URL", "git@github.com:Mean-Green-Racing/fsae-kb.git"),
    branch=branch,
    ssh_key_path=os.getenv("GITHUB_DEPLOY_KEY_PATH", ""),
)
try:
    # This accesses GitHub over SSH but does not clone, run Claude, or write files.
    result = kb._git("ls-remote", "--exit-code", kb.repo_url, f"refs/heads/{branch}")
except (RuntimeError, ValueError) as exc:
    print(f"GitHub deploy key test failed: {exc}", file=sys.stderr)
    raise SystemExit(1)
print(f"GitHub SSH deploy-key authentication successful for {kb.repo_url}")
print(f"Branch {branch}: {result.split()[0]}")
