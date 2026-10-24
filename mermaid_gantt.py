"""Lightweight, PRIVATE Mermaid Gantt chart -> PNG for Discord.

Reads embedded ```mermaid gantt blocks from KB Markdown and renders the common
Mermaid Gantt subset locally with Pillow. No Chromium, remote rendering API, or
LLM tokens. Unsupported syntax fails explicitly; never invent missing dates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
from typing import Iterable


MERMAID_BLOCK = re.compile(r"(?ims)^\s*`{3,}[ \t]*mermaid[ \t]*\n(.*?)^\s*`{3,}[ \t]*$")
# Mermaid parses durations case-sensitively: 'm' minutes, 'M' months.
DURATION = re.compile(r"^(\d+(?:\.\d+)?)([dDwW])$")
DATE_FORMATS = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY/MM/DD": "%Y/%m/%d",
    "MM/DD/YYYY": "%m/%d/%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
    "DD-MM-YYYY": "%d-%m-%Y",
    "MM-DD-YYYY": "%m-%d-%Y",
}
VISUAL_WORDS = {"show", "gantt", "chart", "render", "display", "draw", "visualize",
                "view", "timeline", "diagram", "schedule", "our", "please", "the", "for",
                "me", "of", "current", "latest", "project", "what", "is", "it", "a",
                "can", "could", "would", "should", "you", "us", "we", "i", "now",
                "updated", "update", "team", "please", "get", "give", "see", "open",
                "up", "pull", "this", "that", "from", "any", "an"}
MAX_TASKS = 70
MAX_CHART_DAYS = 1095
MAX_MARKDOWN_BYTES = 3_000_000


class GanttError(ValueError):
    """Chart cannot be drawn reliably with this subset renderer."""


@dataclass(frozen=True)
class Task:
    label: str
    section: str
    start: date
    end: date
    flags: frozenset[str]
    identifier: str = ""


@dataclass(frozen=True)
class Gantt:
    title: str
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class GanttSource:
    path: str
    index: int
    source: str
    title: str


def extract_gantt_blocks(markdown: str) -> list[str]:
    """Find only true Gantt code fences, not Mermaid flowcharts."""
    found = []
    for match in MERMAID_BLOCK.finditer(markdown):
        code = match.group(1).strip()
        # Mermaid allows init directives and comment lines before 'gantt'.
        first = next((s.strip().lower() for s in code.splitlines()
                      if s.strip() and not s.lstrip().startswith(("%%", "#"))), "")
        if first == "gantt":
            found.append(code)
    return found


def remove_gantt_blocks(text: str) -> str:
    """For generated AI text: attach diagrams as images instead of raw code."""
    def rewrite(match):
        return "\n*(Gantt chart attached below.)*\n" if extract_gantt_blocks(match.group()) else match.group()
    return MERMAID_BLOCK.sub(rewrite, text).strip()


def _date(value: str, fmt: str) -> date:
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError as exc:
        raise GanttError(f"Unsupported date '{value.strip()}' for dateFormat {fmt}") from exc


def _duration_days(value: str) -> int | None:
    m = DURATION.fullmatch(value.strip())
    if not m:
        return None
    quantity = float(m.group(1))
    days = quantity * (7 if m.group(2).lower() == "w" else 1)
    if not days.is_integer():
        raise GanttError(f"Fractional duration '{value}' needs full Mermaid rendering")
    if days > MAX_CHART_DAYS:
        raise GanttError("Task duration is longer than three years")
    return int(days)


def _advance(start: date, days: int, excludes_weekends: bool,
             exclusions: frozenset[date]) -> date:
    if days < 0:
        raise GanttError("Negative task duration")
    if days == 0:
        return start
    if not excludes_weekends and not exclusions:
        return start + timedelta(days=days)
    target, counted = start, 0
    while counted < days:
        target += timedelta(days=1)
        if (excludes_weekends and target.weekday() >= 5) or target in exclusions:
            continue
        counted += 1
        if (target - start).days > MAX_CHART_DAYS:
            raise GanttError("Duration with excluded days exceeds three years")
    return target


def parse_gantt(source: str) -> Gantt:
    lines = source.splitlines()
    title = "Project Gantt Chart"
    fmt = "%Y-%m-%d"
    section = "General"
    seen_start = False
    exclusions: set[date] = set()
    exclude_weekends = False
    tasks: list[Task] = []
    by_id: dict[str, Task] = {}

    for raw in lines:
        row = raw.strip()
        if not row or row.startswith(("%%", "#")):
            continue
        if row.lower() == "gantt":
            seen_start = True
            continue
        if not seen_start:
            if row.startswith("%%{"):
                continue  # Mermaid initialization directives, not task data.
            raise GanttError("Expected 'gantt' at the start of the Mermaid block")
        directive, _, arg = row.partition(" ")
        k = directive.lower()
        if k == "title":
            title = arg.strip()[:110] or title
            continue
        if k == "section":
            section = arg.strip()[:80] or "General"
            continue
        if k == "dateformat":
            format_value = arg.strip()
            if format_value not in DATE_FORMATS:
                raise GanttError(f"Unsupported Mermaid dateFormat: {format_value}")
            fmt = DATE_FORMATS[format_value]
            continue
        if k == "excludes":
            for token in arg.split(","):
                value = token.strip().lower()
                if value == "weekends":
                    exclude_weekends = True
                elif value in {"saturday", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday"}:
                    # Single-day exclusions need full Mermaid for correct duration semantics.
                    raise GanttError(f"Cannot exactly render 'excludes {value}'")
                else:
                    exclusions.add(_date(token, fmt))
            continue
        if k in {"axisformat", "todaymarker", "tickinterval", "weekday", "inclusiveenddates", "displaymode"}:
            # Cosmetic directives only; dates still come from the source.
            continue
        if k in {"click", "classdef", "accdescr", "acctitle"}:
            continue
        if ":" not in row:
            raise GanttError(f"Unsupported task/directive: {row[:65]}")
        label, raw_metadata = row.split(":", 1)
        label = label.strip()[:100]
        if not label:
            raise GanttError("Task is missing a label")
        fields = [s.strip() for s in raw_metadata.split(",") if s.strip()]
        flags: set[str] = set()
        while fields and fields[0].lower() in {"done", "active", "crit", "milestone"}:
            flags.add(fields.pop(0).lower())
        identifier = ""
        if len(fields) == 3:
            identifier = fields.pop(0)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.-]*", identifier):
                raise GanttError(f"Invalid task ID: {identifier}")
        if len(fields) not in (1, 2):
            raise GanttError(f"Unsupported task metadata: {raw_metadata.strip()[:85]}")

        if len(fields) == 1:
            if not tasks:
                raise GanttError(f"Task '{label}' needs an explicit start date")
            start = tasks[-1].end
            finish = fields[0]
        else:
            start_expr, finish = fields
            if start_expr.lower().startswith("after "):
                ids = start_expr[6:].split()
                if not ids or any(i not in by_id for i in ids):
                    raise GanttError(f"Unresolved dependency in task '{label}': {start_expr}")
                start = max(by_id[i].end for i in ids)
            else:
                start = _date(start_expr, fmt)

        if finish.lower().startswith("until "):
            ref = finish[6:].strip()
            if ref not in by_id:
                raise GanttError(f"Unresolved 'until {ref}' in task '{label}'")
            end = by_id[ref].start
        else:
            duration = _duration_days(finish)
            end = (_advance(start, duration, exclude_weekends, frozenset(exclusions))
                   if duration is not None else _date(finish, fmt))
        if end < start:
            raise GanttError(f"Task '{label}' ends before it starts")
        item = Task(label, section, start, end, frozenset(flags), identifier)
        tasks.append(item)
        if identifier:
            if identifier in by_id:
                raise GanttError(f"Duplicate task ID: {identifier}")
            by_id[identifier] = item
        if len(tasks) > MAX_TASKS:
            raise GanttError(f"More than {MAX_TASKS} tasks; use the source file")
    if not tasks:
        raise GanttError("No supported Gantt tasks were found")
    if (max(t.end for t in tasks) - min(t.start for t in tasks)).days > MAX_CHART_DAYS:
        raise GanttError("Chart spans more than three years")
    return Gantt(title, tuple(tasks))


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    filename = ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    try:
        return ImageFont.truetype(filename, size)
    except OSError:
        return ImageFont.load_default()


def _truncate(draw, value: str, font, max_px: int) -> str:
    if draw.textlength(value, font=font) <= max_px:
        return value
    while value and draw.textlength(value + "...", font=font) > max_px:
        value = value[:-1]
    return value + "..."


def render_gantt_png(chart: Gantt) -> bytes:
    """Generate a 1600px PNG that Discord can preview natively."""
    from PIL import Image, ImageDraw

    tasks = chart.tasks
    earliest = min(x.start for x in tasks)
    latest = max(x.end for x in tasks)
    span_days = max(7, (latest - earliest).days + 2)
    width, left, right = 1600, 470, 1560
    top, row_height, section_height = 140, 36, 29
    records = []
    current = None
    for task in tasks:
        if task.section != current:
            current = task.section
            records.append(("section", task.section))
        records.append(("task", task))
    height = min(3300, top + len(records) * row_height + 65)
    if height < top + len(records) * row_height + 10:
        raise GanttError("Too many tasks to draw in a Discord-sized PNG")
    im = Image.new("RGB", (width, height), "#101827")
    d = ImageDraw.Draw(im)
    head = _font(26, True)
    sub = _font(15)
    text = _font(17)
    group_font = _font(17, True)
    d.text((28, 22), _truncate(d, chart.title, head, 1460), font=head, fill="#f2f5fc")
    d.text((30, 62), f"{earliest:%b %d, %Y}  -  {latest:%b %d, %Y}    |    {len(tasks)} tasks",
           font=sub, fill="#a1b1ca")
    d.line((left, 100, right, 100), fill="#45556f", width=2)
    cell = (right - left) / span_days

    # 6 or fewer day ticks for readability, then weekly/monthly ticks.
    tick = 1 if span_days <= 12 else (7 if span_days <= 90 else (14 if span_days <= 180 else 30))
    for n in range(0, span_days + 1, tick):
        x = int(left + n * cell)
        if x > right:
            continue
        dt = earliest + timedelta(days=n)
        d.line((x, 104, x, height - 24), fill="#283448", width=1)
        label = f"{dt:%b %d}" if span_days <= 180 else f"{dt:%b %Y}"
        d.text((x + 2, 109), label, font=sub, fill="#b9c6dd")
    def x_at(day: date) -> int:
        return round(left + ((day - earliest).days / span_days) * (right - left))

    y = top
    for kind, value in records:
        if kind == "section":
            d.rounded_rectangle((22, y + 2, right + 2, y + section_height),
                                radius=4, fill="#1b2941")
            d.text((30, y + 5), _truncate(d, str(value), group_font, 390),
                   fill="#d7e3f7", font=group_font)
            y += row_height
            continue
        task: Task = value
        if (y // row_height) % 2:
            d.rectangle((22, y, right + 1, y + row_height), fill="#141f30")
        is_crit = "crit" in task.flags
        is_done = "done" in task.flags
        is_milestone = "milestone" in task.flags
        color = ("#eaa45c" if is_crit else "#4aa99b" if is_done else
                 "#7698e3" if "active" in task.flags else "#7890b4")
        d.text((32, y + 7), _truncate(d, task.label, text, 410),
               fill="#e7ecf5", font=text)
        x0, x1 = x_at(task.start), x_at(task.end)
        if is_milestone:
            cx, cy = x0, y + 17
            d.polygon([(cx, cy - 11), (cx + 10, cy), (cx, cy + 11), (cx - 10, cy)], fill=color)
        else:
            x1 = max(x0 + 4, x1)
            d.rounded_rectangle((x0, y + 9, min(x1, right), y + 27),
                                radius=4, fill=color)
        y += row_height
    output = BytesIO()
    im.save(output, format="PNG", optimize=True)
    return output.getvalue()


def discover_gantt_sources(folder: Path) -> list[GanttSource]:
    """Read the complete Markdown files; KB passages may split a Mermaid fence."""
    sources = []
    root = Path(folder)
    if not root.is_dir():
        return sources
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in {".md", ".markdown"}:
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts) or path.stat().st_size > MAX_MARKDOWN_BYTES:
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            for index, source in enumerate(extract_gantt_blocks(raw), 1):
                m = re.search(r"(?im)^\s*title\s+(.+)$", source)
                title = m.group(1).strip() if m else PurePosixPath(relative.as_posix()).stem
                sources.append(GanttSource(relative.as_posix(), index, source, title))
        except OSError as exc:
            print(f"Cannot read Gantt source {relative}: {exc}")
    return sources


def rank_gantts(gantts: Iterable[GanttSource], query: str, hits: Iterable = ()) -> list[GanttSource]:
    """Choose source charts based on requested subsystem, file and search ranks."""
    search_terms = {s.lower() for s in re.findall(r"[a-zA-Z0-9_-]+", query)} - VISUAL_WORDS
    ranked_paths = {hit.passage.path: i for i, hit in enumerate(hits)}
    def score(chart: GanttSource):
        path_words = set(re.findall(r"[a-zA-Z0-9_-]+", chart.path.lower()))
        label_words = set(re.findall(r"[a-zA-Z0-9_-]+", chart.title.lower()))
        body_words = set(re.findall(r"[a-zA-Z0-9_-]+", chart.source.lower()))
        result = 9 * len(search_terms & path_words)
        result += 7 * len(search_terms & label_words)
        result += 2 * len(search_terms & body_words)
        if chart.path in ranked_paths:
            result += max(0, 4 - ranked_paths[chart.path] * .5)
        if not search_terms and chart.path.lower().endswith("roadmap.md"):
            result += 10
        return result
    return sorted(gantts, key=lambda chart: (-score(chart), chart.path.lower(), chart.index))
