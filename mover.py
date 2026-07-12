#!/usr/bin/env python3
"""qbt-jelly-mover: move completed qBittorrent downloads into a Jellyfin library.

Polls the qBittorrent WebUI API for completed torrents, stops them, asks
`claude -p` (sonnet) to classify the content (movie / tv / other) and produce a
move plan that follows Jellyfin naming conventions, then executes the plan with
plain `mv` operations and finally removes the torrent from qBittorrent
(without deleting files -- they have already been moved).

Everything except the classification/naming step is deterministic Python.
Stdlib only.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULTS = {
    "QBT_URL": "http://192.168.0.42:8082",
    "QBT_API_KEY": "",
    # Path prefix as qBittorrent sees it -> where that prefix is mounted here.
    "QBT_SAVE_PATH_PREFIX": "/downloads",
    "LOCAL_DOWNLOADS_DIR": "/mnt/downloads",
    "MOVIES_DIR": "/mnt/jelly/movies",
    "TV_DIR": "/mnt/jelly/tv",
    # Optional: where low-confidence media goes (empty = leave in downloads).
    "NEEDS_REVIEW_DIR": "",
    # Junk files (release-group notes, samples, ...) are moved here, never
    # into the library. Empty = "<LOCAL_DOWNLOADS_DIR>/.recycle".
    "RECYCLE_DIR": "",
    "POLL_INTERVAL": "60",
    "CLAUDE_BIN": "claude",
    "CLAUDE_MODEL": "sonnet",
    "CLAUDE_TIMEOUT": "180",
    "CLAUDE_MAX_BUDGET_USD": "1.00",
    # API key for the claude CLI. Empty = whatever auth the CLI already has
    # (subscription login), but that login expires; an API key does not.
    "ANTHROPIC_API_KEY": "",
    # Retry backoff for failed torrents: base delay before the 1st retry,
    # doubling each subsequent attempt, capped at the max. Failed torrents
    # are retried forever (never permanently given up on).
    "RETRY_BASE_DELAY_SEC": "300",
    "RETRY_MAX_DELAY_SEC": "21600",
    "STATE_FILE": "~/.local/state/qbt-jelly-mover/state.json",
    # Where per-torrent model trajectory transcripts (<hash>.log) are written.
    # Empty = same directory as STATE_FILE. Set to "off" to disable.
    "TRAJECTORY_DIR": "",
    # Only process torrents in this qBittorrent category ("" = all).
    "QBT_CATEGORY": "",
    "CLEANUP_EMPTY_DIRS": "true",
}

FORBIDDEN_CHARS = '<>:"/\\|?*'


def load_config(env_file: str) -> dict:
    cfg = dict(DEFAULTS)
    path = Path(env_file).expanduser()
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in cfg:  # real environment wins
        if k in os.environ:
            cfg[k] = os.environ[k]
    if not cfg["QBT_API_KEY"]:
        die(f"QBT_API_KEY not set (env file: {env_file})")
    if not cfg["RECYCLE_DIR"]:
        cfg["RECYCLE_DIR"] = os.path.join(
            cfg["LOCAL_DOWNLOADS_DIR"].rstrip("/"), ".recycle")
    return cfg


def die(msg: str) -> None:
    log(f"FATAL: {msg}")
    sys.exit(1)


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# --------------------------------------------------------------------------
# qBittorrent API (v5.x, Bearer-token auth)
# --------------------------------------------------------------------------

class Qbt:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _req(self, path: str, params: dict | None = None, post: bool = False) -> bytes:
        url = f"{self.url}/api/v2/{path}"
        data = None
        if params and post:
            data = urllib.parse.urlencode(params).encode()
        elif params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=data, headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def _json(self, path: str, params: dict | None = None):
        return json.loads(self._req(path, params))

    def completed(self, category: str = "") -> list[dict]:
        params = {"filter": "completed"}
        if category:
            params["category"] = category
        return self._json("torrents/info", params)

    def files(self, torrent_hash: str) -> list[dict]:
        return self._json("torrents/files", {"hash": torrent_hash})

    def stop(self, torrent_hash: str) -> None:
        try:
            self._req("torrents/stop", {"hashes": torrent_hash}, post=True)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # qBittorrent 4.x
                self._req("torrents/pause", {"hashes": torrent_hash}, post=True)
            else:
                raise

    def remove(self, torrent_hash: str) -> None:
        """Remove the torrent entry only; never its files."""
        self._req("torrents/delete",
                  {"hashes": torrent_hash, "deleteFiles": "false"}, post=True)


# --------------------------------------------------------------------------
# State (so we never re-ask Claude about a torrent we already handled)
# --------------------------------------------------------------------------

class State:
    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                log(f"WARN: could not read state file {self.path}, starting fresh")

    def get(self, torrent_hash: str) -> dict:
        return self.data.get(torrent_hash, {})

    def clear(self, torrent_hash: str) -> None:
        self.data.pop(torrent_hash, None)

    def set(self, torrent_hash: str, **fields) -> None:
        entry = self.data.setdefault(torrent_hash, {})
        entry.update(fields, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)


def parse_ts(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))


def retry_delay(attempts: int, cfg: dict) -> int:
    """Exponential backoff before retrying a failed torrent, in seconds."""
    base = int(cfg["RETRY_BASE_DELAY_SEC"])
    cap = int(cfg["RETRY_MAX_DELAY_SEC"])
    return min(base * (2 ** max(attempts - 1, 0)), cap)


def fmt_delay(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"


# --------------------------------------------------------------------------
# Claude planner
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You organize completed torrent downloads into a Jellyfin media library.
Classify the torrent and produce a move plan as JSON matching the schema.

media_type: "movie" (single feature film), "tv" (episodic series -- including
documentary series, lecture series and courses), or "other" (music, software,
books, games, etc.).

Rules:
- title: the canonical title as a metadata provider (TheMovieDB/TheTVDB) lists it.
- year: first release/air year; null if not confidently known.
- library_folder: "Title (Year)", or just "Title" when year is null.
  The characters < > : " / \\ | ? * are forbidden in folder and file names;
  replace ":" in titles with " -".
- existing: if one of the provided existing library folders is the SAME
  movie/show, set existing to that name verbatim and use it as library_folder
  (keep its naming as-is, even if unconventional). Otherwise null.
- junk: extraneous files that do not belong in a media library, listed
  verbatim from the file list: release-group notes/ads (.txt, .nfo, .url,
  .exe, .sfv), sample videos, "screens"/"proof" images, checksum files.
  Junk goes ONLY in "junk", never in moves.
- moves: every non-junk file of the torrent must appear exactly once. "from"
  must be copied verbatim from the file list. "to" is a path relative to
  library_folder.
  - movie: main video file -> "Title (Year).ext". Subtitles ->
    "Title (Year).<lang>.srt" (omit <lang> if unknown). Genuine artwork keeps
    standard Jellyfin names (cover.jpg, backdrop.jpg). Real bonus content
    (trailers, featurettes, deleted scenes) -> "extras/<original filename>".
  - tv: episodes -> "Season 01/Title S01E01.ext" (zero-padded; specials go in
    "Season 00"). Append the episode name when it is evident in the filename:
    "Season 01/Title S01E01 Episode Name.ext". For lecture series/courses,
    number the lectures in their natural order as S01E01, S01E02, ...
    Subtitles are named like their episode.
  - "Subtitle groups" listed under Files are a single directory of many
    per-language subtitle files for one episode/movie. Move the whole group
    with ONE moves entry instead of listing its files individually: "from"
    is the group's path verbatim (ending in "/"), "to" is the "to" of the
    video file these subtitles belong to, copied VERBATIM from that video's
    moves entry (Jellyfin only finds subtitles sitting next to the video and
    named after it; the mover renames each file to "Video Name.<lang>.srt"
    automatically). Never use a directory as a group's "to", and do not list
    a subtitle group's individual files anywhere in "moves" or "junk".
- For media_type "other": library_folder is "" and moves is []; junk stays [].
- confidence: "low" if unsure about the classification or the naming,
  otherwise "high".
- If you are not confident of the canonical title or release year, verify
  with WebSearch before answering. Skip the search when you already know.
Output JSON only."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "media_type": {"type": "string", "enum": ["movie", "tv", "other"]},
        "confidence": {"type": "string", "enum": ["high", "low"]},
        "title": {"type": "string"},
        "year": {"type": ["integer", "null"]},
        "library_folder": {"type": "string"},
        "existing": {"type": ["string", "null"]},
        "moves": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                },
                "required": ["from", "to"],
                "additionalProperties": False,
            },
        },
        "junk": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["media_type", "confidence", "title", "year",
                 "library_folder", "existing", "moves", "junk"],
    "additionalProperties": False,
}

# Tokens that carry no identity information (release tags, codecs, ...).
NOISE_TOKENS = {
    "1080p", "720p", "2160p", "480p", "4k", "uhd", "hdr", "hdr10", "dv",
    "bluray", "blu", "ray", "brrip", "bdrip", "webrip", "web", "dl", "hdtv",
    "dvdrip", "remux", "x264", "x265", "h264", "h265", "hevc", "avc", "av1",
    "aac", "ac3", "eac3", "dts", "dd5", "ddp5", "ddp", "atmos", "truehd",
    "10bit", "8bit", "complete", "season", "series", "extended", "remastered",
    "proper", "repack", "internal", "limited", "unrated", "uncut", "multi",
    "dual", "audio", "subs", "sub", "eng", "ita", "kor", "jap", "esp", "vostfr",
    "korean", "japanese", "chinese", "french", "italian", "spanish", "german",
    "the", "and", "of", "a", "an", "to", "in", "tgx", "rarbg", "yts", "eztv",
    "galaxyrg", "galaxytv", "video", "tv", "hd",
}


def name_tokens(s: str) -> set[str]:
    toks = set(re.findall(r"[a-z0-9]+", s.lower()))
    return {t for t in toks
            if t not in NOISE_TOKENS and not t.isdigit() and len(t) > 2
            and not re.fullmatch(r"[se]\d+|s\d+e\d+", t)}


def library_candidates(torrent_name: str, cfg: dict) -> dict[str, list[str]]:
    """Cheap local prefilter: only library folders sharing a meaningful token
    with the torrent name are shown to Claude. Keeps the prompt small."""
    want = name_tokens(torrent_name)
    out = {}
    for label, root in (("movies", cfg["MOVIES_DIR"]), ("tv", cfg["TV_DIR"])):
        matches = []
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            entries = []
        for entry in entries:
            if entry.startswith(".") or not (Path(root) / entry).is_dir():
                continue
            if want & name_tokens(entry):
                matches.append(entry)
        out[label] = matches[:12]
    return out


SUBTITLE_EXTS = {"srt", "ass", "ssa", "sub", "vtt"}
SUBTITLE_GROUP_MIN = 8  # a directory with >= this many subtitle files is grouped


def group_subtitle_dirs(files: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Bulk per-language subtitle packs (20-30 .srt files in one directory,
    one such directory per episode) blow up the prompt/output enough to
    exceed CLAUDE_TIMEOUT if listed file-by-file. Collapse each such
    directory into a single prompt line so Claude can move the whole group
    with one {from, to} entry instead of one per language file -- see
    validate_plan, which expands the group into per-file moves placed next
    to the episode's video file as "Video Name.<lang>.<ext>" (the only
    layout Jellyfin discovers external subtitles from). Returns (files to
    list individually, groups keyed by the synthetic "dir/" from-path)."""
    by_dir: dict[str, list[dict]] = {}
    for f in files:
        name = f["name"]
        ext = name.rpartition(".")[2].lower()
        if ext in SUBTITLE_EXTS:
            d = name.rsplit("/", 1)[0] if "/" in name else ""
            by_dir.setdefault(d, []).append(f)
    groups = {f"{d}/": members for d, members in by_dir.items()
             if len(members) >= SUBTITLE_GROUP_MIN}
    grouped_names = {f["name"] for members in groups.values() for f in members}
    remaining = [f for f in files if f["name"] not in grouped_names]
    return remaining, groups


def build_prompt(name: str, files: list[dict], groups: dict[str, list[dict]],
                 candidates: dict) -> str:
    lines = [f"Torrent: {name}", "Files:"]
    for f in files[:400]:
        size_mb = f["size"] / 1e6
        lines.append(f"{f['name']} ({size_mb:.0f} MB)")
    if len(files) > 400:
        lines.append(f"... and {len(files) - 400} more files")
    if groups:
        lines.append("")
        lines.append("Subtitle groups (bulk multi-language packs; see 'moves' "
                     "rules for how to move one as a single unit):")
        for d, members in groups.items():
            total_mb = sum(f["size"] for f in members) / 1e6
            sample = ", ".join(sorted(f["name"].rsplit("/", 1)[-1]
                                      for f in members)[:5])
            lines.append(f"{d} ({len(members)} subtitle files, "
                         f"{total_mb:.0f} MB total, e.g. {sample}, ...)")
    lines.append("Existing library folders (possible matches; each is prefixed "
                 "with its [media_type], which is NOT part of the folder name):")
    any_match = False
    for label, names in candidates.items():
        for n in names:
            lines.append(f"[{label}] {n}")
            any_match = True
    if not any_match:
        lines.append("(none)")
    return "\n".join(lines)


def deannotate_folder(plan: dict, candidates: dict[str, list[str]]) -> None:
    """Undo the "[media_type] " annotation build_prompt adds to existing folders.

    The model is meant to return the bare folder name, but it occasionally
    echoes the annotated string (e.g. "[tv] How I Met Your Mother (2005)")
    verbatim into library_folder/existing, which would spawn a spurious
    "[tv] ..." library folder instead of merging into the real one. We only
    rewrite values that exactly equal a string we generated, so genuinely
    bracketed folder names (e.g. "[DB]FLCL ...") are never touched. A matched
    `existing` folder is authoritative for the destination."""
    real_names = {n for names in candidates.values() for n in names}
    annotated = {f"[{label}] {n}": n
                 for label, names in candidates.items() for n in names}
    for key in ("library_folder", "existing"):
        val = plan.get(key)
        if isinstance(val, str) and val in annotated:
            plan[key] = annotated[val]
    existing = plan.get("existing")
    if isinstance(existing, str) and existing in real_names:
        plan["library_folder"] = existing


def ask_claude(prompt: str, cfg: dict, trajectory_path: Path | None = None,
               meta: dict | None = None) -> dict:
    # stream-json + verbose makes claude emit every turn (thinking, text, tool
    # calls, tool results) as JSONL, which we render into a per-torrent
    # trajectory file for observability. The terminal "result" event still
    # carries the schema-validated structured_output, same as plain json mode.
    cmd = [
        cfg["CLAUDE_BIN"],
        "--model", cfg["CLAUDE_MODEL"],
        "-p",
        "--tools", "WebSearch",
        "--allowedTools", "WebSearch",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--setting-sources", "",
        "--system-prompt", SYSTEM_PROMPT,
        "--json-schema", json.dumps(PLAN_SCHEMA),
        "--output-format", "stream-json",
        "--verbose",
        "--max-budget-usd", cfg["CLAUDE_MAX_BUDGET_USD"],
    ]
    env = os.environ.copy()
    if cfg.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = cfg["ANTHROPIC_API_KEY"]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                         timeout=int(cfg["CLAUDE_TIMEOUT"]), env=env)

    events = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # stray non-JSON line; ignore

    # Always persist the trajectory -- including on failure, where it is most
    # useful for debugging -- before we raise on any error.
    if trajectory_path is not None:
        try:
            write_trajectory(trajectory_path, prompt, events, cfg, meta,
                             stderr=res.stderr)
        except OSError as e:
            log(f"  WARN: could not write trajectory {trajectory_path}: {e}")

    if res.returncode != 0:
        raise RuntimeError(f"claude exited {res.returncode}: {res.stderr.strip()[:500]}")
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None:
        raise RuntimeError("claude produced no terminal result event")
    if result.get("is_error"):
        raise RuntimeError(f"claude reported error: {str(result.get('result'))[:500]}")
    plan = result.get("structured_output")
    if not isinstance(plan, dict):
        raise RuntimeError("claude returned no structured_output")
    cost = result.get("total_cost_usd")
    log(f"  claude plan: type={plan.get('media_type')} "
        f"confidence={plan.get('confidence')} folder={plan.get('library_folder')!r} "
        f"cost=${cost:.4f}" if cost is not None else f"  claude plan: {plan.get('media_type')}")
    return plan


# --------------------------------------------------------------------------
# Trajectory transcript (per-torrent observability)
# --------------------------------------------------------------------------

# Tool results (raw web-search dumps) are context, not the model's reasoning;
# cap them so transcripts stay readable. Thinking/text/tool calls are kept whole.
TOOL_RESULT_CAP = 2000


def trajectory_path_for(torrent_hash: str, cfg: dict) -> Path | None:
    """Path of the trajectory file for a torrent, or None if disabled."""
    d = cfg.get("TRAJECTORY_DIR", "").strip()
    if d.lower() == "off":
        return None
    base = Path(d).expanduser() if d else Path(cfg["STATE_FILE"]).expanduser().parent
    return base / f"{torrent_hash}.log"


def _blocks(event: dict) -> list:
    content = event.get("message", {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content or []


def write_trajectory(path: Path, prompt: str, events: list, cfg: dict,
                     meta: dict | None = None, stderr: str = "") -> None:
    out: list[str] = []
    w = out.append
    w("=" * 72)
    w("qbt-jelly-mover model trajectory")
    if meta:
        w(f"torrent : {meta.get('name')}")
        w(f"hash    : {meta.get('hash')}")
    w(f"time    : {time.strftime('%Y-%m-%dT%H:%M:%S')}")
    w(f"model   : {cfg['CLAUDE_MODEL']}")
    w("=" * 72)
    w("")
    w("----- PROMPT (user) -----")
    w(prompt)
    w("")
    w("----- TRAJECTORY -----")
    for e in events:
        t = e.get("type")
        if t == "assistant":
            for b in _blocks(e):
                bt = b.get("type")
                if bt == "thinking":
                    text = (b.get("thinking") or "").strip()
                    if text:
                        w("[thinking]")
                        w(text)
                        w("")
                elif bt == "text":
                    text = (b.get("text") or "").strip()
                    if text:
                        w("[assistant]")
                        w(text)
                        w("")
                elif bt in ("tool_use", "server_tool_use"):
                    w(f"[tool_use: {b.get('name')}] "
                      f"{json.dumps(b.get('input', {}), ensure_ascii=False)}")
                    w("")
        elif t == "user":
            for b in _blocks(e):
                if b.get("type") != "tool_result":
                    continue
                c = b.get("content")
                s = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                if len(s) > TOOL_RESULT_CAP:
                    s = s[:TOOL_RESULT_CAP] + f"\n... [truncated {len(s) - TOOL_RESULT_CAP} chars]"
                flag = " (error)" if b.get("is_error") else ""
                w(f"[tool_result{flag}]")
                w(s)
                w("")

    w("----- RESULT -----")
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is not None:
        w(f"is_error : {result.get('is_error')}")
        w(f"cost_usd : {result.get('total_cost_usd')}")
        w(f"num_turns: {result.get('num_turns')}")
        so = result.get("structured_output")
        w("structured_output:")
        w(json.dumps(so, indent=2, ensure_ascii=False) if so is not None else "(none)")
        if result.get("is_error"):
            w("")
            w("result text:")
            w(str(result.get("result"))[:2000])
    else:
        w("(no terminal result event)")
        if stderr.strip():
            w("")
            w("stderr:")
            w(stderr.strip()[:2000])

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out))
    tmp.replace(path)


# --------------------------------------------------------------------------
# Plan validation + execution (deterministic)
# --------------------------------------------------------------------------

def sanitize_component(name: str) -> str:
    name = name.replace(":", " -")
    name = "".join(c for c in name if c not in FORBIDDEN_CHARS and ord(c) >= 32)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name


def sanitize_relpath(rel: str) -> str:
    parts = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"path escapes destination: {rel!r}")
        clean = sanitize_component(part)
        if not clean:
            raise ValueError(f"path component empty after sanitizing: {rel!r}")
        parts.append(clean)
    if not parts:
        raise ValueError(f"empty destination path: {rel!r}")
    return "/".join(parts)


def common_root(file_names: list[str]) -> str:
    """Shared top-level folder of the torrent's files, '' if none."""
    tops = {f.split("/", 1)[0] for f in file_names}
    if len(tops) == 1 and any("/" in f for f in file_names):
        return next(iter(tops))
    return ""


# Extensions that are junk beyond doubt, used when the plan forgets a file.
JUNK_EXTS = {"txt", "nfo", "url", "lnk", "exe", "sfv", "srr", "md5", "sha",
             "sha1", "sha256", "torrent", "htm", "html", "website"}

# Language names seen in bulk subtitle packs -> tags Jellyfin parses. Keys are
# lowercased with punctuation collapsed to single spaces ("Portuguese
# (Brazilian)" and "Brazilian.Portuguese" both hit "portuguese brazilian" /
# "brazilian portuguese"). ISO codes ("en", "eng", "pt-BR") need no entry:
# they pass through verbatim and Jellyfin already parses them.
SUBTITLE_LANGS = {
    "arabic": "ar", "bulgarian": "bg", "catalan": "ca", "chinese": "zh",
    "simplified chinese": "zh-Hans", "chinese simplified": "zh-Hans",
    "traditional chinese": "zh-Hant", "chinese traditional": "zh-Hant",
    "croatian": "hr", "czech": "cs", "danish": "da", "dutch": "nl",
    "english": "en", "estonian": "et", "filipino": "fil", "finnish": "fi",
    "french": "fr", "german": "de", "greek": "el", "hebrew": "he",
    "hindi": "hi", "hungarian": "hu", "indonesian": "id", "italian": "it",
    "japanese": "ja", "korean": "ko", "latvian": "lv", "lithuanian": "lt",
    "malay": "ms", "may": "ms", "norwegian": "no", "bokmal": "nb",
    "norwegian bokmal": "nb", "persian": "fa", "farsi": "fa",
    "polish": "pl", "portuguese": "pt",
    "brazilian portuguese": "pt-BR", "portuguese brazilian": "pt-BR",
    "european portuguese": "pt-PT", "portuguese european": "pt-PT",
    "romanian": "ro", "russian": "ru", "serbian": "sr", "slovak": "sk",
    "slovenian": "sl", "spanish": "es",
    "latin american spanish": "es-419", "spanish latin american": "es-419",
    "european spanish": "es-ES", "spanish european": "es-ES",
    "castilian": "es-ES", "swedish": "sv", "tagalog": "tl", "thai": "th",
    "turkish": "tr", "ukrainian": "uk", "vietnamese": "vi",
}

# Trailing modifiers Jellyfin recognizes as stream flags ("English [SDH]" ->
# "en.sdh"). "hi" is deliberately absent: it collides with Hindi's ISO code.
SUBTITLE_FLAGS = {"sdh", "cc", "forced", "default"}


def subtitle_token(filename: str) -> str:
    """Suffix for a grouped subtitle landing next to its video as
    "Video Name.<token>.<ext>". Bulk packs name files like "12_English.srt"
    or "9_English [SDH].srt": strip the index, map the language name to a
    tag Jellyfin parses, keep recognized trailing flags. Unmapped names are
    kept as-is -- Jellyfin shows an unrecognized token as the subtitle
    track's title, which is still selectable in the player."""
    stem = filename.rpartition(".")[0] or filename
    s = re.sub(r"^\d+[ _.\-]*", "", stem) or stem
    words = re.sub(r"[^0-9A-Za-z]+", " ", s).lower().split()
    flags = []
    while len(words) > 1 and words[-1] in SUBTITLE_FLAGS:
        flags.insert(0, words.pop())
    lang = SUBTITLE_LANGS.get(" ".join(words))
    if lang:
        return ".".join([lang] + flags)
    return sanitize_component(s) or sanitize_component(stem) or "und"


def validate_plan(plan: dict, file_names: list[str],
                  groups: dict[str, list[dict]] | None = None
                  ) -> tuple[str, str, list[tuple[str, str]], list[tuple[str, str]]]:
    """Returns (media_type, library_folder, moves, junk) where moves and junk
    are [(from, to), ...] lists. Raises ValueError."""
    groups = groups or {}
    media_type = plan["media_type"]
    if media_type == "other":
        return media_type, "", [], []

    folder = sanitize_component(plan["library_folder"])
    if not folder or folder == "..":
        raise ValueError(f"bad library_folder: {plan['library_folder']!r}")

    known = set(file_names)
    root = common_root(file_names)

    def below_root(src: str) -> str:
        return src[len(root) + 1:] if root and src.startswith(root + "/") else src

    seen_from, junk = set(), []
    for src in plan.get("junk", []):
        if src not in known:
            raise ValueError(f"plan references unknown file: {src!r}")
        if src in seen_from:
            raise ValueError(f"file listed twice: {src!r}")
        seen_from.add(src)
        junk.append((src, sanitize_relpath(below_root(src))))

    seen_to, moves, group_moves = set(), [], []
    for m in plan["moves"]:
        src = m["from"]
        if src in groups:
            group_moves.append(m)
            continue
        if src not in known:
            raise ValueError(f"plan references unknown file: {src!r}")
        if src in seen_from:
            raise ValueError(f"file listed twice: {src!r}")
        seen_from.add(src)
        dst = sanitize_relpath(m["to"])
        if dst.lower() in seen_to:
            raise ValueError(f"duplicate destination: {dst!r}")
        seen_to.add(dst.lower())
        moves.append((src, dst))

    # Subtitle groups expand after the regular moves: a group's "to" names the
    # destination of the video file it subtitles (with or without extension),
    # and each member lands next to that video as "Video Name.<token>.<ext>"
    # -- the only layout Jellyfin discovers external subtitles from.
    if group_moves:
        video_dest: dict[str, str] = {}
        for _, dst in moves:
            video_dest[dst.lower()] = dst
            video_dest.setdefault(dst.rsplit(".", 1)[0].lower(), dst)
        for m in group_moves:
            src = m["from"]
            target = video_dest.get(sanitize_relpath(m["to"]).lower())
            if target is None:
                raise ValueError(
                    f"subtitle group {src!r}: to={m['to']!r} does not match "
                    "the destination of any video file in moves")
            base = target.rsplit(".", 1)[0]
            members = groups[src]
            tokens = [subtitle_token(gf["name"].rsplit("/", 1)[-1])
                      for gf in members]
            counts: dict[str, int] = {}
            for tok in tokens:
                counts[tok.lower()] = counts.get(tok.lower(), 0) + 1
            for gf, token in zip(members, tokens):
                gsrc = gf["name"]
                if gsrc in seen_from:
                    raise ValueError(f"file listed twice: {gsrc!r}")
                seen_from.add(gsrc)
                stem, _, ext = gsrc.rsplit("/", 1)[-1].rpartition(".")
                if counts[token.lower()] > 1:  # e.g. two "English" variants
                    token = sanitize_component(stem) or token
                gdst, n = f"{base}.{token}.{ext.lower()}", 2
                while gdst.lower() in seen_to:
                    gdst = f"{base}.{token}.{n}.{ext.lower()}"
                    n += 1
                seen_to.add(gdst.lower())
                moves.append((gsrc, gdst))

    # Anything Claude forgot still has to leave the downloads folder: junk
    # extensions go to the recycle bin, anything else is stashed in extras/.
    for src in file_names:
        if src in seen_from:
            continue
        rel = below_root(src)
        ext = rel.rpartition(".")[2].lower()
        if ext in JUNK_EXTS:
            junk.append((src, sanitize_relpath(rel)))
            log(f"  WARN: plan did not cover {src!r}; recycling")
            continue
        dst = sanitize_relpath("extras/" + rel)
        while dst.lower() in seen_to:
            stem, dot, ext = dst.rpartition(".")
            dst = f"{stem} (x).{ext}" if dot else f"{dst} (x)"
        seen_to.add(dst.lower())
        moves.append((src, dst))
        log(f"  WARN: plan did not cover {src!r}; stashing in extras/")
    return media_type, folder, moves, junk


def unique_dest(dst: Path) -> Path:
    """Never overwrite: append ' (n)' before the extension until free."""
    if not dst.exists():
        return dst
    stem, ext = dst.stem, dst.suffix
    for n in range(1, 1000):
        cand = dst.with_name(f"{stem} ({n}){ext}")
        if not cand.exists():
            log(f"  WARN: {dst.name!r} exists; using {cand.name!r}")
            return cand
    raise RuntimeError(f"could not find free name for {dst}")


def run_mv(src: Path, dst: Path, dry_run: bool) -> None:
    if dry_run:
        log(f"  DRY-RUN mv {src} -> {dst}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["mv", "-T", "--", str(src), str(dst)], check=True)
    if not dst.exists():
        raise RuntimeError(f"mv finished but {dst} does not exist")


def map_qbt_path(qbt_path: str, cfg: dict) -> Path:
    prefix = cfg["QBT_SAVE_PATH_PREFIX"].rstrip("/")
    local = cfg["LOCAL_DOWNLOADS_DIR"].rstrip("/")
    p = qbt_path.rstrip("/")
    if p == prefix:
        return Path(local)
    if p.startswith(prefix + "/"):
        return Path(local + p[len(prefix):])
    raise ValueError(f"torrent save path {qbt_path!r} is outside {prefix!r}")


def remove_empty_dirs(root: Path) -> None:
    """rmdir-only cleanup of the husk the torrent leaves behind. Never touches files."""
    if not root.is_dir():
        return
    for dirpath, _dirnames, _filenames in sorted(
            os.walk(root), key=lambda w: len(w[0]), reverse=True):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass  # not empty -- leave it


def execute_plan(media_type: str, folder: str, moves: list[tuple[str, str]],
                 junk: list[tuple[str, str]], torrent: dict, cfg: dict,
                 dry_run: bool) -> None:
    root = Path(cfg["TV_DIR"] if media_type == "tv" else cfg["MOVIES_DIR"])
    save_local = map_qbt_path(torrent["save_path"], cfg)
    recycle_base = Path(cfg["RECYCLE_DIR"]) / sanitize_component(torrent["name"])

    # Reuse an existing folder that differs only by case, if any.
    dest_base = root / folder
    if not dest_base.exists():
        for entry in os.listdir(root):
            if entry.lower() == folder.lower():
                dest_base = root / entry
                break

    for base, batch in ((dest_base, moves), (recycle_base, junk)):
        for src_rel, dst_rel in batch:
            src = save_local / src_rel
            dst = base / dst_rel
            if not src.is_file():
                if dst.exists():  # already moved by an earlier, interrupted attempt
                    log(f"  resume: {dst.name!r} already in place")
                    continue
                raise RuntimeError(f"source file missing: {src}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
            run_mv(src, unique_dest(dst), dry_run)

    if cfg["CLEANUP_EMPTY_DIRS"].lower() == "true" and not dry_run:
        top = common_root([m[0] for m in moves + junk])
        if top:
            remove_empty_dirs(save_local / top)
    log(f"  moved {len(moves)} file(s) -> {dest_base}")
    if junk:
        log(f"  recycled {len(junk)} junk file(s) -> {recycle_base}")


def move_to_review(torrent: dict, cfg: dict, dry_run: bool) -> bool:
    review = cfg["NEEDS_REVIEW_DIR"]
    if not review:
        return False
    src = Path(map_qbt_path(torrent["content_path"], cfg))
    if src == Path(cfg["LOCAL_DOWNLOADS_DIR"].rstrip("/")):
        # multifile torrent without a root folder: content_path == save_path
        raise RuntimeError("refusing to move the downloads root to needs_review")
    if not src.exists():
        raise RuntimeError(f"content path missing: {src}")
    dst = unique_dest(Path(review) / sanitize_component(torrent["name"]))
    run_mv(src, dst, dry_run)
    log(f"  moved to needs_review: {dst}")
    return True


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

UNSTABLE_STATES = {"moving", "checkingUP", "checkingDL", "checkingResumeData"}
STOPPED_STATES = {"stoppedUP", "pausedUP"}


def process_torrent(t: dict, qbt: Qbt, state: State, cfg: dict, dry_run: bool) -> None:
    h, name = t["hash"], t["name"]
    log(f"processing {name!r} ({h[:8]})")

    if t["state"] not in STOPPED_STATES:
        if dry_run:
            log("  DRY-RUN would stop torrent")
        else:
            qbt.stop(h)
            log("  stopped torrent")

    # priority 0 = file deselected in qBittorrent: it isn't on disk
    files = [f for f in qbt.files(h) if f.get("priority", 1) != 0]
    file_names = [f["name"] for f in files]
    if not file_names:
        raise RuntimeError("torrent has no downloaded files")

    prompt_files, groups = group_subtitle_dirs(files)
    if groups:
        log(f"  grouped {sum(len(m) for m in groups.values())} subtitle "
            f"file(s) into {len(groups)} bulk-pack director{'y' if len(groups) == 1 else 'ies'}")

    candidates = library_candidates(name, cfg)
    prompt = build_prompt(name, prompt_files, groups, candidates)
    plan = ask_claude(prompt, cfg, trajectory_path_for(h, cfg),
                      meta={"name": name, "hash": h})
    deannotate_folder(plan, candidates)
    media_type, folder, moves, junk = validate_plan(plan, file_names, groups=groups)

    if media_type == "other":
        log("  not movie/tv content; leaving torrent stopped in qBittorrent")
        state.set(h, status="skipped", name=name, added_on=t.get("added_on"), reason="not media")
        return

    if plan["confidence"] == "low":
        log("  low confidence classification")
        if move_to_review(t, cfg, dry_run):
            if not dry_run:
                qbt.remove(h)
            state.set(h, status="needs_review", name=name, added_on=t.get("added_on"))
        else:
            state.set(h, status="failed", name=name, added_on=t.get("added_on"), reason="low confidence")
        return

    execute_plan(media_type, folder, moves, junk, t, cfg, dry_run)
    if dry_run:
        log("  DRY-RUN would remove torrent from qBittorrent")
        return
    qbt.remove(h)
    state.set(h, status="done", name=name, added_on=t.get("added_on"), media_type=media_type, folder=folder)
    log(f"  done: removed torrent, library folder {folder!r}")


def poll_once(qbt: Qbt, state: State, cfg: dict, dry_run: bool) -> None:
    for t in qbt.completed(cfg["QBT_CATEGORY"]):
        h = t["hash"]
        tags = {tag.strip() for tag in t.get("tags", "").split(",")}
        if "noclaude" in tags:
            continue
        entry = state.get(h)
        if entry and entry.get("added_on") != t.get("added_on"):
            # Same hash, new download (torrent was re-added): start over.
            log(f"re-added torrent {t['name']!r}; clearing previous state")
            state.clear(h)
            entry = {}
        if entry.get("status") == "done":
            # Files are already in the library; only the removal is left.
            log(f"retrying removal of {t['name']!r}")
            if not dry_run:
                qbt.remove(h)
            continue
        if entry.get("status") in ("skipped", "needs_review"):
            continue
        if entry.get("status") == "failed":
            delay = retry_delay(entry.get("attempts", 0), cfg)
            last_updated = entry.get("updated")
            if last_updated:
                try:
                    due = time.time() - parse_ts(last_updated) >= delay
                except ValueError:
                    due = True  # unparsable timestamp: don't get stuck
                if not due:
                    continue
        if t["progress"] < 1 or t["state"] in UNSTABLE_STATES:
            continue
        try:
            process_torrent(t, qbt, state, cfg, dry_run)
        except Exception as e:  # noqa: BLE001 -- keep the daemon alive
            attempts = entry.get("attempts", 0) + 1
            delay = retry_delay(attempts, cfg)
            log(f"  ERROR (attempt {attempts}): {e}; retrying in ~{fmt_delay(delay)}")
            state.set(h, status="failed", name=t["name"], added_on=t.get("added_on"),
                      attempts=attempts, error=str(e)[:300])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", default=str(Path(__file__).parent / ".env"),
                    help="path to env-style config file")
    ap.add_argument("--once", action="store_true", help="single poll, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan (claude is still called) but change nothing")
    args = ap.parse_args()

    cfg = load_config(args.env)
    qbt = Qbt(cfg["QBT_URL"], cfg["QBT_API_KEY"])
    state = State(cfg["STATE_FILE"])

    for d in (cfg["LOCAL_DOWNLOADS_DIR"], cfg["MOVIES_DIR"], cfg["TV_DIR"]):
        if not Path(d).is_dir():
            die(f"directory not available: {d} (NAS mount down?)")

    log(f"qbt-jelly-mover started (poll={cfg['POLL_INTERVAL']}s, "
        f"model={cfg['CLAUDE_MODEL']}, dry_run={args.dry_run})")
    while True:
        try:
            poll_once(qbt, state, cfg, args.dry_run)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            log(f"WARN: poll failed: {e}")
        if args.once:
            break
        time.sleep(int(cfg["POLL_INTERVAL"]))


if __name__ == "__main__":
    main()
