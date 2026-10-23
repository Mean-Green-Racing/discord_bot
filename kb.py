"""Read-only local Git mirror and inexpensive AGENTS.md-guided document search.

GitHub and local search do not use Anthropic tokens. Only selected excerpts
are passed to the caller to include in a Claude request.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import time
from urllib.parse import quote, unquote


FILE_TYPES = {".md", ".markdown", ".txt", ".rst", ".csv", ".yaml", ".yml", ".json", ".pdf", ".docx"}
MAX_FILE_BYTES = 3_000_000
MAX_DOC_CHARS = 7500
MAX_PASSAGES = 5
MAX_FILES = 3
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "do", "does",
    "for", "from", "how", "i", "in", "is", "it", "me", "my", "of", "on", "or",
    "our", "please", "should", "tell", "that", "the", "their", "this", "to", "we",
    "what", "when", "where", "which", "who", "why", "will", "with", "would", "you",
}
PATH_RE = re.compile(
    r"(?<![\w./-])(?:\.\.?/)?[\w./-]+\.(?:md|markdown|txt|rst|csv|yaml|yml|json|pdf|docx)\b",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+)$")
WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def words(value: str) -> list[str]:
    result = []
    for term in WORD_RE.findall(value.lower()):
        if term in STOPWORDS:
            continue
        # A small, deterministic normalization step; no external embedding API.
        if term.endswith("ies") and len(term) > 5:
            term = term[:-3] + "y"
        elif term.endswith("s") and not term.endswith("ss") and len(term) > 4:
            term = term[:-1]
        result.append(term)
    return result


def github_repo_slug(url: str) -> str | None:
    """Return owner/repo for an HTTPS or SSH GitHub remote, if recognized."""
    url = url.strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            slug = url[len(prefix):].rstrip("/")
            if slug.endswith(".git"):
                slug = slug[:-4]
            if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
                return slug
            raise ValueError(f"Invalid GitHub repository URL: {url}")
    return None


def ssh_repo_url(url: str) -> str:
    """For GitHub, force SSH even if an old .env still specifies HTTPS."""
    slug = github_repo_slug(url)
    if slug is not None:
        return f"git@github.com:{slug}.git"
    return url


@dataclass(frozen=True)
class Passage:
    path: str
    start: int
    end: int
    content: str
    title: str = ""
    source_kind: str = "lines"  # lines, page, paragraphs

    @property
    def location(self) -> str:
        if self.source_kind == "page":
            return f"page {self.start}"
        if self.source_kind == "paragraphs":
            return f"paragraphs {self.start}-{self.end}"
        return f"lines {self.start}-{self.end}"


@dataclass(frozen=True)
class SearchHit:
    passage: Passage
    score: float


class KnowledgeBase:
    def __init__(
        self,
        folder: Path,
        repo_url: str,
        branch: str = "main",
        ssh_key_path: str | Path = "",
        refresh_seconds: int = 600,
    ) -> None:
        self.folder = folder
        self.repo_url = ssh_repo_url(repo_url)
        self.branch = branch
        self.ssh_key_path = str(ssh_key_path).strip()
        self.refresh_seconds = max(60, refresh_seconds)
        self._lock = asyncio.Lock()
        self._checked_at = 0.0
        self.commit = ""
        self.last_error = ""
        self.passages: list[Passage] = []
        self.routes: dict[str, str] = {}
        self.doc_words: dict[str, set[str]] = {}
        self.file_count = 0

    def _private_key(self) -> Path:
        """Validate the *local private key* before any network access."""
        if not self.ssh_key_path:
            raise RuntimeError(
                "Set GITHUB_DEPLOY_KEY_PATH in .env to the LOCAL PRIVATE key file "
                "matching the repository's GitHub deploy key (not the .pub file)."
            )
        key = Path(os.path.expandvars(self.ssh_key_path)).expanduser()
        if not key.is_absolute():
            key = Path(__file__).resolve().parent / key
        key = key.resolve()
        if key.suffix == ".pub":
            raise RuntimeError(f"Deploy key path must point to the PRIVATE key, not: {key}")
        if not key.is_file():
            raise RuntimeError(f"Deploy private key not found: {key}")
        if os.name == "posix" and (stat.S_IMODE(key.stat().st_mode) & 0o077):
            raise RuntimeError(
                f"Deploy private key has unsafe permissions: {key}. Run: chmod 600 '{key}'"
            )
        return key

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Never include the private key contents in .env or in a Git URL.
        # Only clone/fetch access the network. Other commands work offline.
        if args and args[0] in {"clone", "fetch", "ls-remote"} and github_repo_slug(self.repo_url):
            key = self._private_key()
            env["GIT_SSH_COMMAND"] = shlex.join([
                "ssh", "-i", str(key),
                "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=10",
            ])
        try:
            result = subprocess.run(
                ["git", *args], cwd=cwd, env=env,
                capture_output=True, text=True, timeout=75, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Git unavailable or timed out: {exc}") from exc
        if result.returncode:
            error = (result.stderr or result.stdout or "unknown error").strip()
            if "Host key verification failed" in error:
                error += " | Trust github.com after verifying its official SSH host-key fingerprint."
            elif "Permission denied (publickey)" in error:
                error += " | Verify that the matching PUBLIC key is a read-only deploy key on this repo."
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {error[-550:]}")
        return result.stdout.strip()

    def _ensure_correct_origin(self) -> None:
        """Safely migrate an existing checkout of the same repo from HTTPS to SSH."""
        origin = self._git("remote", "get-url", "origin", cwd=self.folder)
        if origin == self.repo_url:
            return
        old_slug = github_repo_slug(origin)
        new_slug = github_repo_slug(self.repo_url)
        if old_slug and new_slug and old_slug.lower() == new_slug.lower():
            self._git("remote", "set-url", "origin", self.repo_url, cwd=self.folder)
            return
        raise RuntimeError(
            "Existing local clone has an unexpected Git remote (not the configured KB). "
            "Refusing to change it automatically; inspect the origin before continuing."
        )

    def _sync(self) -> None:
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        if not (self.folder / ".git").is_dir():
            if self.folder.exists() and any(self.folder.iterdir()):
                raise RuntimeError(f"Cannot clone into non-empty folder: {self.folder}")
            self._git(
                "clone", "--quiet", "--depth", "1", "--single-branch", "--branch",
                self.branch, self.repo_url, str(self.folder),
            )
        else:
            # This is a dedicated, disposable *mirror*; do not edit it locally.
            self._ensure_correct_origin()
            self._git("fetch", "--quiet", "--depth", "1", "origin", self.branch, cwd=self.folder)
            self._git("reset", "--quiet", "--hard", "FETCH_HEAD", cwd=self.folder)
        current = self._git("rev-parse", "HEAD", cwd=self.folder)
        if current != self.commit or not self.passages:
            self._index_folder()
            self.commit = current

    async def ensure_ready(self, force: bool = False) -> None:
        if (
            not force and self.passages
            and time.monotonic() - self._checked_at < self.refresh_seconds
        ):
            return
        async with self._lock:
            if (
                not force and self.passages
                and time.monotonic() - self._checked_at < self.refresh_seconds
            ):
                return
            self._checked_at = time.monotonic()
            try:
                await asyncio.to_thread(self._sync)
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
                # If the network is down, reuse the last successfully indexed
                # local clone, but clearly report that it may be stale.
                if not self.passages and self.folder.exists():
                    try:
                        self._index_folder()
                        self.commit = self._git("rev-parse", "HEAD", cwd=self.folder)
                    except Exception:
                        pass
                if not self.passages:
                    raise RuntimeError(self.last_error) from exc

    def _index_folder(self) -> None:
        all_files: dict[str, Path] = {}
        for path in self.folder.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(self.folder)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if path.suffix.lower() not in FILE_TYPES:
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            all_files[rel.as_posix()] = path

        route_lines: dict[str, list[str]] = defaultdict(list)
        for rel, absolute in all_files.items():
            if absolute.name.lower() != "agents.md":
                continue
            guide = absolute.read_text(encoding="utf-8", errors="replace")
            heading = ""
            for line in guide.splitlines():
                heading_match = HEADING_RE.match(line)
                if heading_match:
                    heading = heading_match.group(1)
                for raw_ref in PATH_RE.findall(unquote(line)):
                    # Guides may mention ./foo.md or docs/foo.md; resolve
                    # relative to AGENTS.md first, then repository root.
                    for candidate in (
                        (PurePosixPath(rel).parent / raw_ref).as_posix(),
                        PurePosixPath(raw_ref).as_posix(),
                    ):
                        normalized = os.path.normpath(candidate).replace("\\", "/")
                        if normalized in all_files:
                            route_lines[normalized].append(f"{heading} {line.strip()[:240]}")
                            break

        passages: list[Passage] = []
        for rel, path in sorted(all_files.items()):
            try:
                if path.suffix.lower() == ".pdf":
                    from pypdf import PdfReader
                    reader = PdfReader(str(path))
                    for page_number, page in enumerate(reader.pages[:80], 1):
                        extracted = page.extract_text() or ""
                        if not extracted.strip():
                            continue
                        passages.extend(
                            self._make_passages(rel, extracted.splitlines(), "page", page_number)
                        )
                elif path.suffix.lower() == ".docx":
                    from docx import Document
                    document = Document(str(path))
                    lines = [p.text for p in document.paragraphs]
                    for table in document.tables:
                        for row in table.rows:
                            lines.append(" | ".join(cell.text for cell in row.cells))
                    passages.extend(self._make_passages(rel, lines, "paragraphs"))
                else:
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    passages.extend(self._make_passages(rel, raw.splitlines(), "lines"))
            except Exception as exc:
                print(f"Index warning: skipped {rel}: {type(exc).__name__}: {exc}")

        if not passages:
            raise RuntimeError("No readable documents in cloned repo (checked Markdown, text, PDF, DOCX).")
        self.passages = passages
        self.routes = {key: " ".join(value)[:2500] for key, value in route_lines.items()}
        self.file_count = len({p.path for p in passages})
        doc_words: dict[str, set[str]] = defaultdict(set)
        for passage in passages:
            doc_words[passage.path].update(words(passage.content))
            doc_words[passage.path].update(words(passage.title))
        for key in doc_words:
            doc_words[key].update(words(key))
            doc_words[key].update(words(self.routes.get(key, "")))
        self.doc_words = dict(doc_words)

    @staticmethod
    def _make_passages(path: str, lines: list[str], kind: str, page: int = 0) -> list[Passage]:
        chunks = []
        buffer: list[tuple[int, str]] = []
        n = 0
        title = ""
        for number, line in enumerate(lines, 1):
            heading_match = HEADING_RE.match(line)
            if heading_match:
                title = heading_match.group(1)
            # Split individual long lines, not entire files into one massive chunk.
            fragments = [line[i:i + 700] for i in range(0, len(line), 700)] or [""]
            for fragment in fragments:
                if n + len(fragment) > 1550 and buffer:
                    text = "\n".join(part for _, part in buffer).strip()
                    if text:
                        chunks.append(Passage(path, page if kind == "page" else buffer[0][0],
                                              page if kind == "page" else buffer[-1][0],
                                              text, title, kind))
                    buffer = buffer[-1:]  # Minimal overlap; keep chunks small.
                    n = sum(len(value) for _, value in buffer)
                buffer.append((number, fragment))
                n += len(fragment) + 1
        if buffer:
            text = "\n".join(part for _, part in buffer).strip()
            if text:
                chunks.append(Passage(path, page if kind == "page" else buffer[0][0],
                                      page if kind == "page" else buffer[-1][0],
                                      text, title, kind))
        return chunks

    def search(self, question: str) -> list[SearchHit]:
        query = list(dict.fromkeys(words(question)))
        if not query or not self.passages:
            return []
        total_docs = max(1, len(self.doc_words))
        df = Counter()
        for term in query:
            df[term] = sum(term in token_set for token_set in self.doc_words.values())
        idf = {term: math.log(1 + (total_docs - df[term] + .5) / (df[term] + .5))
               for term in query}

        results: list[SearchHit] = []
        for passage in self.passages:
            route_count = Counter(words(self.routes.get(passage.path, "")))
            path_count = Counter(words(passage.path))
            head_count = Counter(words(passage.title))
            body_count = Counter(words(passage.content))
            score = 0.0
            match_count = 0
            for term in query:
                count = body_count[term]
                present = bool(count or head_count[term] or path_count[term] or route_count[term])
                if not present:
                    continue
                match_count += 1
                # Filename and AGENTS.md route descriptions carry extra weight.
                score += idf[term] * (
                    min(count, 3) * 1.0 +
                    min(head_count[term], 2) * 2.0 +
                    min(path_count[term], 2) * 4.0 +
                    min(route_count[term], 2) * 3.0
                )
            if match_count:
                score *= (1 + 0.17 * (match_count - 1))
                # Rank an actual target document over a navigation guide unless
                # the question explicitly concerns AGENTS.md itself.
                if passage.path.lower().endswith("agents.md") and "agent" not in query:
                    score *= 0.65
                results.append(SearchHit(passage, score))

        # AGENTS.md is a routing guide: don't pay to resend it if a target
        # document was retrieved. Use the guide itself only for queries about
        # AGENTS.md or when it is the sole matching source.
        if "agent" not in query and any(
            hit.passage.path.rsplit("/", 1)[-1].lower() != "agents.md"
            for hit in results
        ):
            results = [
                hit for hit in results
                if hit.passage.path.rsplit("/", 1)[-1].lower() != "agents.md"
            ]
        results.sort(key=lambda hit: hit.score, reverse=True)
        selected: list[SearchHit] = []
        file_hits: Counter[str] = Counter()
        used_chars = 0
        for hit in results:
            path = hit.passage.path
            if file_hits[path] >= 2:
                continue
            if path not in file_hits and len(file_hits) >= MAX_FILES:
                continue
            # Prefix/location metadata costs tokens as well.
            estimated_chars = len(hit.passage.content) + len(path) + 100
            if used_chars + estimated_chars > MAX_DOC_CHARS:
                continue
            selected.append(hit)
            file_hits[path] += 1
            used_chars += estimated_chars
            if len(selected) >= MAX_PASSAGES:
                break
        return selected

    def excerpts(self, hits: list[SearchHit]) -> str:
        return "\n\n".join(
            f"[{i}] {hit.passage.path} ({hit.passage.location})\n{hit.passage.content}"
            for i, hit in enumerate(hits, 1)
        )

    def source_links(self, hits: list[SearchHit]) -> list[str]:
        seen = set()
        links = []
        for hit in hits:
            p = hit.passage
            if p.path in seen:
                continue
            seen.add(p.path)
            encoded = quote(p.path, safe="/")
            base = f"https://github.com/Mean-Green-Racing/fsae-kb/blob/{self.commit}/{encoded}"
            if p.source_kind == "lines":
                base += f"#L{p.start}-L{p.end}"
            elif p.source_kind == "page":
                base += f"#page={p.start}"
            links.append(f"[{p.path}]({base})")
        return links
