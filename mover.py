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
    "MAX_ATTEMPTS": "3",
    "STATE_FILE": "~/.local/state/qbt-jelly-mover/state.json",
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
- For media_type "other": library_folder is "" and moves is []; junk stays [].
- confidence: "low" if unsure about the classification or the naming,
  otherwise "high".
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


def build_prompt(name: str, files: list[dict], candidates: dict) -> str:
    lines = [f"Torrent: {name}", "Files:"]
    for f in files[:400]:
        size_mb = f["size"] / 1e6
        lines.append(f"{f['name']} ({size_mb:.0f} MB)")
    if len(files) > 400:
        lines.append(f"... and {len(files) - 400} more files")
    lines.append("Existing library folders (possible matches):")
    any_match = False
    for label, names in candidates.items():
        for n in names:
            lines.append(f"[{label}] {n}")
            any_match = True
    if not any_match:
        lines.append("(none)")
    return "\n".join(lines)


def ask_claude(prompt: str, cfg: dict) -> dict:
    cmd = [
        cfg["CLAUDE_BIN"],
        "--model", cfg["CLAUDE_MODEL"],
        "-p",
        "--tools", "",
        "--no-session-persistence",
        "--setting-sources", "",
        "--system-prompt", SYSTEM_PROMPT,
        "--json-schema", json.dumps(PLAN_SCHEMA),
        "--output-format", "json",
        "--max-budget-usd", cfg["CLAUDE_MAX_BUDGET_USD"],
    ]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                         timeout=int(cfg["CLAUDE_TIMEOUT"]))
    if res.returncode != 0:
        raise RuntimeError(f"claude exited {res.returncode}: {res.stderr.strip()[:500]}")
    out = res.stdout
    payload = json.loads(out[out.index("{"):out.rindex("}") + 1])
    if payload.get("is_error"):
        raise RuntimeError(f"claude reported error: {str(payload.get('result'))[:500]}")
    plan = payload.get("structured_output")
    if not isinstance(plan, dict):
        raise RuntimeError("claude returned no structured_output")
    cost = payload.get("total_cost_usd")
    log(f"  claude plan: type={plan.get('media_type')} "
        f"confidence={plan.get('confidence')} folder={plan.get('library_folder')!r} "
        f"cost=${cost:.4f}" if cost is not None else f"  claude plan: {plan.get('media_type')}")
    return plan


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


def validate_plan(plan: dict, file_names: list[str]
                  ) -> tuple[str, str, list[tuple[str, str]], list[tuple[str, str]]]:
    """Returns (media_type, library_folder, moves, junk) where moves and junk
    are [(from, to), ...] lists. Raises ValueError."""
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

    seen_to, moves = set(), []
    for m in plan["moves"]:
        src = m["from"]
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

    prompt = build_prompt(name, files, library_candidates(name, cfg))
    plan = ask_claude(prompt, cfg)
    media_type, folder, moves, junk = validate_plan(plan, file_names)

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
        attempts = entry.get("attempts", 0)
        if entry.get("status") == "failed" and attempts >= int(cfg["MAX_ATTEMPTS"]):
            continue
        if t["progress"] < 1 or t["state"] in UNSTABLE_STATES:
            continue
        try:
            process_torrent(t, qbt, state, cfg, dry_run)
        except Exception as e:  # noqa: BLE001 -- keep the daemon alive
            attempts += 1
            log(f"  ERROR ({attempts}/{cfg['MAX_ATTEMPTS']}): {e}")
            state.set(h, status="failed", name=t["name"], added_on=t.get("added_on"),
                      attempts=attempts, error=str(e)[:300])
            if attempts >= int(cfg["MAX_ATTEMPTS"]):
                log(f"  giving up on {t['name']!r}; will not retry "
                    f"(delete its state entry to retry)")


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
