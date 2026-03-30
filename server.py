#!/usr/bin/env python3
"""
Claude Code Session Dashboard

A localhost web dashboard for browsing, searching, and resuming Claude Code
sessions. Scans ~/.claude/projects/ for session JSONL files and presents them
in a filterable, sortable table with expandable chat history.

Usage:
    python3 server.py              # starts on http://127.0.0.1:7891
    python3 server.py --port 8080  # custom port

Requires: Python 3.9+, macOS, Ghostty terminal (for session resume).
No third-party dependencies.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"
PORT = 7891
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
INDEX_TTL = 30
LAST_LIVE_FILE = CLAUDE_DIR / "dashboard-last-live.json"


# ── Last-live persistence ──────────────────────────────────────────────

_last_live = {}  # session_id -> epoch float
_last_live_lock = threading.Lock()


def _load_last_live():
    global _last_live
    try:
        _last_live = json.loads(LAST_LIVE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        _last_live = {}


def _save_last_live():
    try:
        LAST_LIVE_FILE.write_text(json.dumps(_last_live))
    except OSError:
        pass


# ── Session index (lightweight, no JSONL parsing) ───────────────────────

_index = []
_index_ts = 0.0
_index_lock = threading.Lock()


def _build_index():
    sessions = []
    if not PROJECTS_DIR.exists():
        return sessions
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jf in proj_dir.glob("*.jsonl"):
            try:
                st = jf.stat()
                sessions.append(
                    {
                        "id": jf.stem,
                        "file": str(jf),
                        "proj": proj_dir.name,
                        "mtime": st.st_mtime,
                        "size": st.st_size,
                    }
                )
            except OSError:
                pass
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def get_index(force=False):
    global _index, _index_ts
    with _index_lock:
        if force or time.time() - _index_ts > INDEX_TTL:
            _index = _build_index()
            _index_ts = time.time()
        return list(_index)


# ── Session detail parsing ──────────────────────────────────────────────

_cache = {}
_cache_lock = threading.Lock()


def _head_lines(path, n=30):
    lines = []
    try:
        with open(path, "r", errors="replace") as f:
            for i, ln in enumerate(f):
                if i >= n:
                    break
                lines.append(ln)
    except Exception:
        pass
    return lines


def _tail_lines(path, n=120, chunk=65536):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            buf = b""
            while buf.count(b"\n") <= n + 1 and size > 0:
                read = min(chunk, size)
                size -= read
                f.seek(size)
                buf = f.read(read) + buf
        return buf.decode("utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def _extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return " ".join(parts).strip()
    return ""


_SYS_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def _clean(text):
    text = _SYS_RE.sub("", text).strip()
    if text.lstrip().startswith("Accessing workspace"):
        parts = text.split("---\n", 1)
        if len(parts) > 1 and parts[1].strip():
            text = parts[1].strip()
    return text


def get_chat_history(session_id, limit=30):
    """Extract the last N user/assistant messages from a session JSONL."""
    # Find the file
    filepath = None
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.exists():
            filepath = str(candidate)
            break
    if not filepath:
        return []

    messages = []
    for ln in reversed(_tail_lines(filepath, 500)):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        t = d.get("type")
        if t not in ("user", "assistant"):
            continue
        txt = _clean(_extract_text(d.get("message", {}).get("content", "")))
        if not txt:
            continue
        messages.append({"role": t, "text": txt[:800], "ts": d.get("timestamp", "")})
        if len(messages) >= limit:
            break

    messages.reverse()
    return messages


def parse_session(entry):
    sid = entry["id"]
    mtime = entry["mtime"]

    with _cache_lock:
        cached = _cache.get(sid)
        if cached and cached[0] == mtime:
            return cached[1]

    cwd = git_branch = custom_title = None
    forked_from = None
    last_user = last_assistant = None
    user_turns = 0

    for ln in _head_lines(entry["file"], 40):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        t = d.get("type")
        if t == "custom-title":
            custom_title = d.get("customTitle")
        if t in ("user", "assistant") and not cwd:
            cwd = d.get("cwd")
            git_branch = d.get("gitBranch")
        ff = d.get("forkedFrom")
        if ff and not forked_from:
            forked_from = ff.get("sessionId", str(ff)) if isinstance(ff, dict) else str(ff)
        if t == "user":
            txt = _extract_text(d.get("message", {}).get("content", ""))
            if txt:
                user_turns += 1

    recent_msgs = []  # collect last 4 messages (newest first, reversed later)
    for ln in reversed(_tail_lines(entry["file"], 120)):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        t = d.get("type")
        if t in ("user", "assistant") and not cwd and d.get("cwd"):
            cwd = d["cwd"]
            if not git_branch:
                git_branch = d.get("gitBranch")
        if t == "custom-title" and not custom_title:
            custom_title = d.get("customTitle")
        if t == "assistant" and not last_assistant:
            txt = _clean(_extract_text(d.get("message", {}).get("content", "")))
            if txt:
                last_assistant = txt[:400]
        if t == "user" and not last_user:
            txt = _clean(_extract_text(d.get("message", {}).get("content", "")))
            if txt:
                last_user = txt[:400]
        if t in ("user", "assistant") and len(recent_msgs) < 4:
            txt = _clean(_extract_text(d.get("message", {}).get("content", "")))
            if txt:
                role = "you" if t == "user" else "claude"
                recent_msgs.append({"role": role, "text": txt[:200]})
        if t == "user":
            txt = _extract_text(d.get("message", {}).get("content", ""))
            if txt:
                user_turns += 1
        if last_user and last_assistant and len(recent_msgs) >= 4:
            break
    recent_msgs.reverse()

    # Deduplicate user_turns (head and tail may overlap for short files)
    # For short files (<80 lines), head covers most/all, tail re-counts.
    # Use file size heuristic: small files (<100KB) are likely single-turn.
    is_single_turn = entry["size"] < 100_000 and user_turns <= 2

    detail = {
        "id": sid,
        "proj": entry["proj"],
        "cwd": cwd or ("/" + entry["proj"].lstrip("-").replace("-", "/")),
        "displayName": _display_name(entry["proj"], cwd),
        "lastActive": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        "mtime": mtime,
        "gitBranch": git_branch,
        "customTitle": custom_title,
        "forkedFrom": forked_from,
        "lastUser": last_user,
        "lastAssistant": last_assistant,
        "recentMessages": recent_msgs,
        "sizeKB": round(entry["size"] / 1024),
        "singleTurn": is_single_turn,
    }

    with _cache_lock:
        _cache[sid] = (mtime, detail)
    return detail


_HOME_PREFIX = str(Path.home()).replace("/", "-").lstrip("-")


def _display_name(encoded, cwd=None):
    if cwd:
        home = str(Path.home())
        if cwd == home:
            return "~"
        if cwd.startswith(home + "/"):
            return "~/" + cwd[len(home) + 1 :]
        return cwd
    name = encoded
    pfx = "-" + _HOME_PREFIX + "-"
    if name.startswith(pfx):
        name = name[len(pfx) :]
    return name or "~"


def _get_path_data():
    """Build deduplicated path options by actual cwd, and a mapping for filtering."""
    # Step 1: group by proj dir, find best display name per proj
    by_proj = {}
    with _cache_lock:
        for sid, (_, detail) in _cache.items():
            proj = detail["proj"]
            if proj not in by_proj:
                by_proj[proj] = {"display": None, "count": 0}
            by_proj[proj]["count"] += 1
            dn = detail["displayName"]
            cur = by_proj[proj]["display"]
            # Prefer display names starting with ~ (real cwd) over dash-encoded fallbacks
            if cur is None or (not cur.startswith("~") and dn.startswith("~")):
                by_proj[proj]["display"] = dn

    # Step 2: merge proj dirs with same display name (worktrees sharing a cwd)
    by_display = {}
    for proj, info in by_proj.items():
        dn = info["display"] or _display_name(proj, None)
        if dn not in by_display:
            by_display[dn] = {"projs": set(), "count": 0}
        by_display[dn]["projs"].add(proj)
        by_display[dn]["count"] += info["count"]

    options = []
    for dn in sorted(by_display.keys(), key=str.lower):
        options.append({"value": dn, "label": dn, "count": by_display[dn]["count"]})
    return options, by_display


# ── Active sessions ─────────────────────────────────────────────────────


def get_active_ids():
    active = set()
    sdir = CLAUDE_DIR / "sessions"
    if not sdir.exists():
        return active
    # Collect live sessions; track unmatched ones (sessionId has no JSONL)
    unmatched = []  # (sessionId, cwd, startedAt) for resumed sessions
    for f in sdir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid") or int(f.stem)
            sid = data.get("sessionId")
            os.kill(pid, 0)
            if sid:
                active.add(sid)
                cwd = data.get("cwd", "")
                started = data.get("startedAt", 0) / 1000  # ms → s
                unmatched.append((sid, cwd, started))
        except (ProcessLookupError, ValueError, json.JSONDecodeError, OSError):
            pass
        except PermissionError:
            active.add(data.get("sessionId", ""))

    # For resumed sessions (`claude -c`), the sessions/*.json has a new
    # sessionId but messages are written to the original JSONL under the
    # original sessionId. Resolve these by finding recently-modified JSONLs
    # in the same project directory.
    for sid, cwd, started in unmatched:
        proj_name = cwd.replace("/", "-")
        proj_dir = PROJECTS_DIR / proj_name
        if not proj_dir.is_dir():
            continue
        # Skip if this sessionId already has a matching JSONL
        if (proj_dir / f"{sid}.jsonl").exists():
            continue
        # Find JSONLs modified after this session started, not already claimed
        for jf in proj_dir.glob("*.jsonl"):
            if jf.stem in active:
                continue  # already claimed by a direct match
            try:
                if jf.stat().st_mtime >= started:
                    active.add(jf.stem)
            except OSError:
                pass

    # Record last-live timestamps for all active sessions
    now = time.time()
    with _last_live_lock:
        for sid in active:
            _last_live[sid] = now
        _save_last_live()

    return active


# ── Binary resolution ───────────────────────────────────────────────────


def _find_binary(name):
    """Resolve a binary's absolute path using a login shell (picks up full PATH)."""
    try:
        result = subprocess.run(
            ["/bin/zsh", "-lc", f"command -v {name}"],
            capture_output=True, text=True, timeout=5,
        )
        path = result.stdout.strip()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    # Fallback: common locations
    for p in [
        os.path.expanduser(f"~/.local/bin/{name}"),
        f"/usr/local/bin/{name}",
        f"/opt/homebrew/bin/{name}",
    ]:
        if os.path.isfile(p):
            return p
    return None


# ── Cache warming ───────────────────────────────────────────────────────


def _warm():
    for e in get_index():
        try:
            parse_session(e)
        except Exception:
            pass


threading.Thread(target=_warm, daemon=True).start()


# ── HTTP handler ────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content):
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/":
            self._html(HTML)
            return

        if parsed.path == "/api/sessions":
            offset = int(qs.get("offset", ["0"])[0])
            limit = int(qs.get("limit", ["10"])[0])
            search = qs.get("search", [""])[0].lower().strip()
            path_filter = qs.get("path", [""])[0]
            sort_dir = qs.get("sort", ["desc"])[0]
            hide_print = qs.get("hide_print", ["1"])[0] == "1"
            force = qs.get("refresh", [""])[0] == "1"

            idx = get_index(force=force)
            active = get_active_ids()
            path_options, by_display = _get_path_data()

            filtered_idx = idx
            if path_filter:
                matching_projs = by_display.get(path_filter, {}).get("projs", set())
                filtered_idx = [e for e in filtered_idx if e["proj"] in matching_projs]

            # Parse all matching entries, filter print sessions
            all_parsed = []
            for e in filtered_idx:
                d = parse_session(e)
                if not d:
                    continue
                if hide_print and d.get("singleTurn"):
                    continue
                d["active"] = d["id"] in active
                ll = _last_live.get(d["id"])
                d["lastLive"] = datetime.fromtimestamp(ll, tz=timezone.utc).isoformat() if ll else None
                all_parsed.append(d)

            if sort_dir == "live":
                all_parsed.sort(key=lambda d: _last_live.get(d["id"], 0), reverse=True)

            if search:
                results = []
                for d in all_parsed:
                    haystack = " ".join(
                        filter(
                            None,
                            [
                                d["id"], d["cwd"], d["displayName"],
                                d.get("gitBranch"), d.get("customTitle"),
                                d.get("lastUser"), d.get("lastAssistant"),
                            ],
                        )
                    ).lower()
                    if search in haystack:
                        results.append(d)
                total = len(results)
                page = results[offset : offset + limit]
            else:
                total = len(all_parsed)
                page = all_parsed[offset : offset + limit]

            self._json(
                {
                    "sessions": page,
                    "total": total,
                    "offset": offset,
                    "hasMore": offset + limit < total,
                    "activeCount": len(active),
                    "paths": path_options,
                }
            )
            return

        if parsed.path.startswith("/api/history/"):
            sid = parsed.path.split("/api/history/", 1)[1]
            limit = int(qs.get("limit", ["30"])[0])
            msgs = get_chat_history(sid, limit)
            self._json({"messages": msgs})
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/api/launch", "/api/fork"):
            is_fork = parsed.path == "/api/fork"
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            sid = body.get("sessionId", "")
            cwd = body.get("cwd", str(Path.home()))

            if not sid:
                self._json({"error": "Missing sessionId"}, 400)
                return

            claude_bin = _find_binary("claude")
            if not claude_bin:
                self._json({"error": "claude not found in PATH"}, 500)
                return

            fork_flag = " --fork-session" if is_fork else ""
            cmd = (
                f"cd {shlex.quote(cwd)} && "
                f"exec {shlex.quote(claude_bin)} --resume {shlex.quote(sid)}"
                f"{fork_flag} --dangerously-skip-permissions"
            )
            ghostty = _find_binary("ghostty") or "/Applications/Ghostty.app/Contents/MacOS/ghostty"
            try:
                subprocess.Popen(
                    [ghostty, "-e", "/bin/zsh", "-c", cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._json({"ok": True})
            except FileNotFoundError:
                self._json({"error": "Ghostty not found"}, 500)
            except Exception as ex:
                self._json({"error": str(ex)}, 500)
            return

        self.send_error(404)


# ── Dashboard HTML ──────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Sessions</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#9889;</text></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg0:#0a0e14;--bg1:#0d1117;--bg2:#161b22;--bg3:#1c2129;
  --bd:#30363d;--bd2:#3d444d;
  --t0:#f0f6fc;--t1:#e6edf3;--t2:#8b949e;--t3:#656d76;
  --blue:#58a6ff;--blued:#1f6feb;--green:#3fb950;--greend:#238636;
  --orange:#d29922;--red:#f85149;--purple:#bc8cff;
  --mono:'SF Mono','Fira Code','JetBrains Mono',monospace;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
  --r:8px;--tr:150ms ease;
}
body{font-family:var(--sans);background:var(--bg0);color:var(--t1);line-height:1.5;min-height:100vh}
.container{margin:0;padding:24px 28px}

header{margin-bottom:16px}
.header-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{width:32px;height:32px;background:linear-gradient(135deg,var(--blue),var(--purple));border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;color:#fff;font-weight:700}
.logo h1{font-size:18px;font-weight:600;color:var(--t0)}
.btn{background:var(--bg2);border:1px solid var(--bd);color:var(--t1);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-family:var(--sans);transition:all var(--tr);display:inline-flex;align-items:center;gap:5px}
.btn:hover{background:var(--bg3);border-color:var(--bd2)}
.btn svg{width:13px;height:13px;fill:currentColor}
.btn.spin svg{animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.search-wrap{position:relative;flex:1;min-width:200px}
.search-icon{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--t3);pointer-events:none}
.search-input{width:100%;background:var(--bg1);border:1px solid var(--bd);color:var(--t1);padding:8px 12px 8px 32px;border-radius:6px;font-size:13px;font-family:var(--sans);outline:none;transition:border-color var(--tr)}
.search-input:focus{border-color:var(--blue)}
.search-input::placeholder{color:var(--t3)}

/* Autocomplete */
.ac-wrap{position:relative;min-width:240px;max-width:400px;flex-shrink:0}
.ac-input{width:100%;background:var(--bg1);border:1px solid var(--bd);color:var(--t1);padding:8px 30px 8px 12px;border-radius:6px;font-size:13px;font-family:var(--sans);outline:none;transition:border-color var(--tr)}
.ac-input:focus{border-color:var(--blue)}
.ac-input::placeholder{color:var(--t3)}
.ac-input.active-filter{border-color:var(--blued);background:rgba(31,111,235,.08)}
.ac-clear{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--t3);font-size:18px;cursor:pointer;padding:0 2px;line-height:1;display:none}
.ac-clear:hover{color:var(--t1)}
.ac-list{position:absolute;top:calc(100% + 4px);left:0;right:0;background:var(--bg2);border:1px solid var(--bd);border-radius:6px;max-height:300px;overflow-y:auto;display:none;z-index:100;box-shadow:0 8px 24px rgba(0,0,0,.5)}
.ac-wrap.open .ac-list{display:block}
.ac-item{padding:6px 12px;font-size:12px;cursor:pointer;color:var(--t1);transition:background var(--tr);display:flex;justify-content:space-between;align-items:center;gap:8px}
.ac-item:hover,.ac-item.hi{background:var(--bg3)}
.ac-item-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:11px}
.ac-item-count{color:var(--t3);font-size:10px;flex-shrink:0}
.ac-match{color:var(--blue)}
.ac-all{color:var(--t2);border-bottom:1px solid var(--bd);font-family:var(--sans)}

.stats{display:flex;gap:14px;margin-top:8px;font-size:11px;color:var(--t3);flex-wrap:wrap}
.stat{display:flex;align-items:center;gap:4px}
.dot{width:6px;height:6px;border-radius:50%;display:inline-block}
.dot-g{background:var(--green)}.dot-b{background:var(--blue)}

.tbl-wrap{border:1px solid var(--bd);border-radius:var(--r);overflow:hidden;margin-top:12px}
table{width:100%;border-collapse:collapse;table-layout:fixed}
thead{background:var(--bg1)}
th{padding:8px 12px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--t3);text-align:left;border-bottom:1px solid var(--bd);white-space:nowrap;user-select:none}
th.sortable{cursor:pointer;transition:color var(--tr)}
th.sortable:hover{color:var(--t1)}
.sort-arrow{margin-left:3px;font-size:9px}
td{padding:10px 12px;border-bottom:1px solid var(--bd);font-size:13px;vertical-align:top;background:var(--bg2);overflow:hidden;word-wrap:break-word}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bg3)}
tr.is-active td:first-child{box-shadow:inset 3px 0 0 var(--green)}

.col-session{width:8%}.col-path{width:27%}.col-time{width:8%}.col-msgs{width:52%}.col-act{width:5%}

.sid{font-family:var(--mono);font-size:12px;color:var(--t2);cursor:pointer;transition:color var(--tr);display:inline-block}
.sid:hover{color:var(--blue)}
.badges{margin-top:3px;display:flex;gap:4px;flex-wrap:wrap}
.badge{font-size:9px;padding:1px 6px;border-radius:10px;font-family:var(--mono);white-space:nowrap;font-weight:500;letter-spacing:.2px}
.b-active{background:rgba(63,185,80,.12);color:var(--green);border:1px solid rgba(63,185,80,.2)}
.b-fork{background:rgba(210,153,34,.12);color:var(--orange);border:1px solid rgba(210,153,34,.2)}
.b-branch{background:rgba(63,185,80,.1);color:var(--green);border:1px solid rgba(63,185,80,.15)}
.ctitle{font-size:11px;color:var(--purple);margin-top:3px;font-style:italic;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fork-from{font-size:10px;color:var(--orange);margin-top:2px}
.path-text{font-family:var(--mono);font-size:12px;color:var(--t1);cursor:pointer;transition:color var(--tr);word-break:break-all}
.path-text:hover{color:var(--blue)}
.branch-line{margin-top:3px}
.time-text{font-size:13px;color:var(--t2);white-space:nowrap}
.size-text{font-size:10px;color:var(--t3);margin-top:2px}
.msg-line{font-size:12px;line-height:1.4;margin-bottom:2px;color:var(--t2);overflow:hidden;white-space:nowrap;text-overflow:ellipsis;word-break:break-word}
.msg-line:last-child{margin-bottom:0}
.msg-preview{max-height:76px;overflow:hidden}
.msg-l{font-weight:600;font-size:9px;text-transform:uppercase;letter-spacing:.4px;margin-right:4px}
.ml-you{color:var(--blue)}.ml-cl{color:var(--purple)}
/* Expanded row */
tr.expanded td{background:var(--bg3)}
.chat-log{max-height:220px;overflow-y:scroll;border:1px solid var(--bd);border-radius:6px;background:var(--bg1);padding:8px 0;margin-top:4px}
.chat-msg{padding:4px 12px;font-size:11px;line-height:1.5;word-break:break-word}
.chat-msg+.chat-msg{margin-top:2px}
.chat-msg-u{color:var(--t1)}
.chat-msg-a{color:var(--t2)}
.chat-msg .cm-role{font-weight:600;font-size:9px;text-transform:uppercase;letter-spacing:.4px;margin-right:5px}
.chat-msg-u .cm-role{color:var(--blue)}
.chat-msg-a .cm-role{color:var(--purple)}
.chat-loading{text-align:center;padding:16px;color:var(--t3);font-size:11px}
tr{cursor:pointer}
tr:hover td{background:var(--bg3)}

.btn-action{border:none;color:#fff;width:28px;height:28px;border-radius:5px;cursor:pointer;font-size:13px;transition:all var(--tr);display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;padding:0}
.btn-resume{background:var(--blued)}
.btn-resume:hover{background:var(--blue);transform:translateY(-1px)}
.btn-fork{background:rgba(163,113,247,.18);color:var(--purple)}
.btn-fork:hover{background:rgba(163,113,247,.32);transform:translateY(-1px)}
.action-btns{display:flex;gap:3px;align-items:center;justify-content:flex-end}
td:last-child{padding:10px 4px 10px 0}

.show-more{text-align:center;padding:14px;background:var(--bg2);border-top:1px solid var(--bd)}
.btn-more{background:transparent;border:1px solid var(--bd);color:var(--t2);padding:7px 28px;border-radius:6px;cursor:pointer;font-size:12px;font-family:var(--sans);transition:all var(--tr)}
.btn-more:hover{background:var(--bg3);border-color:var(--bd2);color:var(--t1)}
.loading{text-align:center;padding:50px 20px;color:var(--t3)}
.spinner{width:20px;height:20px;border:2px solid var(--bd);border-top-color:var(--blue);border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 10px}
.empty{text-align:center;padding:50px 20px;color:var(--t3);font-size:13px}
.toast-wrap{position:fixed;bottom:20px;right:20px;z-index:1000;display:flex;flex-direction:column;gap:6px}
.toast{background:var(--bg2);border:1px solid var(--bd);color:var(--t1);padding:8px 16px;border-radius:6px;font-size:12px;animation:slideIn .3s ease;box-shadow:0 4px 16px rgba(0,0,0,.5)}
.toast-ok{border-left:3px solid var(--green)}.toast-err{border-left:3px solid var(--red)}
@keyframes slideIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:var(--bg0)}
::-webkit-scrollbar-thumb{background:var(--bd);border-radius:3px}::-webkit-scrollbar-thumb:hover{background:var(--bd2)}
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="header-top">
      <div class="logo">
        <div class="logo-icon">C</div>
        <h1>Claude Sessions</h1>
      </div>
      <button class="btn" onclick="doRefresh()" id="rbtn">
        <svg viewBox="0 0 16 16"><path d="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 1 1 .908-.418A6 6 0 1 1 8 2v1z"/><path d="M8 1v4l3-2-3-2z"/></svg>
        Refresh
      </button>
    </div>
    <div class="filters">
      <div class="search-wrap">
        <span class="search-icon"><svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor"><path d="M11.5 7a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0zm-.82 4.74a6 6 0 1 1 1.06-1.06l3.04 3.04a.75.75 0 1 1-1.06 1.06l-3.04-3.04z"/></svg></span>
        <input type="text" class="search-input" placeholder="Search sessions..." id="search">
      </div>
      <div class="ac-wrap" id="acWrap">
        <input type="text" class="ac-input" placeholder="Filter by path..." id="pathInput" autocomplete="off">
        <button class="ac-clear" id="acClear">&times;</button>
        <div class="ac-list" id="acList"></div>
      </div>
    </div>
    <div class="stats" id="stats"></div>
  </header>
  <div id="content"><div class="loading"><div class="spinner"></div>Loading sessions...</div></div>
</div>
<div class="toast-wrap" id="toasts"></div>

<script>
const PAGE = 50;
let sessions = [];
let total = 0;
let hasMore = false;
let activeCount = 0;
let loading = false;
let lastRefresh = 0;
let sortDir = 'desc';
let allPaths = [];
let selectedPath = '';
let acIndex = -1;

// ── API ─────────────────────────────────────────────
async function load(append, refresh) {
  if (loading) return;
  loading = true;
  const q = document.getElementById('search').value.trim();
  const offset = append ? sessions.length : 0;
  const params = new URLSearchParams({offset, limit: PAGE, sort: sortDir});
  if (q) params.set('search', q);
  if (selectedPath) params.set('path', selectedPath);
  if (refresh) params.set('refresh', '1');
  try {
    const r = await fetch('/api/sessions?' + params);
    const d = await r.json();
    sessions = append ? sessions.concat(d.sessions) : d.sessions;
    total = d.total; hasMore = d.hasMore; activeCount = d.activeCount;
    lastRefresh = Date.now();
    if (d.paths) allPaths = d.paths;
    render();
    updateStats();
  } catch(e) {
    document.getElementById('content').innerHTML =
      '<div class="empty">Failed to load sessions: '+esc(e.message)+'</div>';
  }
  loading = false;
}

async function launch(id, cwd) {
  try {
    const r = await fetch('/api/launch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({sessionId:id, cwd})
    });
    const d = await r.json();
    d.ok ? toast('Launched in Ghostty') : toast(d.error||'Launch failed','err');
  } catch(e) { toast('Failed: '+e.message,'err'); }
}

async function fork(id, cwd) {
  try {
    const r = await fetch('/api/fork', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({sessionId:id, cwd})
    });
    const d = await r.json();
    d.ok ? toast('Forked in Ghostty') : toast(d.error||'Fork failed','err');
  } catch(e) { toast('Failed: '+e.message,'err'); }
}

// ── Autocomplete ────────────────────────────────────
function acShow() {
  const input = document.getElementById('pathInput');
  const val = input.value.toLowerCase();
  const list = document.getElementById('acList');
  const wrap = document.getElementById('acWrap');

  let filtered = val ? allPaths.filter(p => p.label.toLowerCase().includes(val)) : allPaths;
  if (!filtered.length && !val) { wrap.classList.remove('open'); return; }

  let html = '<div class="ac-item ac-all" data-val="">All paths</div>';
  html += filtered.map(p => {
    let label = esc(p.label);
    if (val) {
      const idx = p.label.toLowerCase().indexOf(val);
      if (idx >= 0)
        label = esc(p.label.slice(0,idx))+'<span class="ac-match">'+esc(p.label.slice(idx,idx+val.length))+'</span>'+esc(p.label.slice(idx+val.length));
    }
    return '<div class="ac-item" data-val="'+attr(p.value)+'"><span class="ac-item-label">'+label+'</span><span class="ac-item-count">'+p.count+'</span></div>';
  }).join('');

  list.innerHTML = html;
  acIndex = -1;
  wrap.classList.add('open');
}

function acSelect(value) {
  const input = document.getElementById('pathInput');
  const wrap = document.getElementById('acWrap');
  const clear = document.getElementById('acClear');
  selectedPath = value;
  if (value) {
    const match = allPaths.find(p => p.value === value);
    input.value = match ? match.label : value;
    input.classList.add('active-filter');
    clear.style.display = 'block';
  } else {
    input.value = '';
    input.classList.remove('active-filter');
    clear.style.display = 'none';
  }
  wrap.classList.remove('open');
  load(false, false);
}

function acClear() {
  acSelect('');
  document.getElementById('pathInput').focus();
}

// AC keyboard nav
document.getElementById('pathInput').addEventListener('keydown', e => {
  const list = document.getElementById('acList');
  const items = list.querySelectorAll('.ac-item');
  if (e.key === 'ArrowDown') { e.preventDefault(); acIndex = Math.min(acIndex+1, items.length-1); acHighlight(items); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); acIndex = Math.max(acIndex-1, 0); acHighlight(items); }
  else if (e.key === 'Enter') {
    e.preventDefault();
    if (acIndex >= 0 && items[acIndex]) acSelect(items[acIndex].dataset.val);
    else if (document.getElementById('acWrap').classList.contains('open') && items.length > 1) acSelect(items[1].dataset.val);
  }
  else if (e.key === 'Escape') { document.getElementById('acWrap').classList.remove('open'); }
});

function acHighlight(items) {
  items.forEach((el,i) => el.classList.toggle('hi', i === acIndex));
  if (items[acIndex]) items[acIndex].scrollIntoView({block:'nearest'});
}

document.getElementById('pathInput').addEventListener('input', () => { acShow(); });
document.getElementById('pathInput').addEventListener('focus', () => { acShow(); });
document.getElementById('acClear').addEventListener('click', acClear);

// Close AC on outside click
document.addEventListener('mousedown', e => {
  if (!e.target.closest('.ac-wrap')) document.getElementById('acWrap').classList.remove('open');
});

// AC list click
document.getElementById('acList').addEventListener('mousedown', e => {
  const item = e.target.closest('.ac-item');
  if (item) { e.preventDefault(); acSelect(item.dataset.val); }
});

// ── Rendering ───────────────────────────────────────
function render() {
  const el = document.getElementById('content');
  if (!sessions.length) {
    el.innerHTML = '<div class="tbl-wrap"><div class="empty">'
      + (document.getElementById('search').value.trim() || selectedPath
         ? 'No sessions match your filters' : 'No sessions found') + '</div></div>';
    return;
  }
  let html = '<div class="tbl-wrap"><table><thead><tr>'
    + '<th class="col-session">Session</th>'
    + '<th class="col-path">Path</th>'
    + '<th class="col-time sortable" onclick="toggleSort()">'+(sortDir==='desc'?'Last Active':'Last Live')+' <span class="sort-arrow">'+'\u25BC</span></th>'
    + '<th class="col-msgs">Messages</th>'
    + '<th class="col-act"></th>'
    + '</tr></thead><tbody>';
  html += sessions.map(row).join('');
  html += '</tbody></table>';
  if (hasMore)
    html += '<div class="show-more"><button class="btn-more" onclick="showMore()">Show More ('+sessions.length+' of '+total+')</button></div>';
  html += '</div>';
  el.innerHTML = html;
}

function row(s) {
  const sid = s.id.substring(0,8)+'\u2026'+s.id.slice(-4);
  let badges = '';
  if (s.active) badges += '<span class="badge b-active">\u25cf live</span>';
  if (s.forkedFrom) badges += '<span class="badge b-fork">\u2442 fork</span>';
  let sc = '<span class="sid" data-copy="'+attr(s.id)+'" title="Click to copy">'+sid+'</span>';
  if (badges) sc += '<div class="badges">'+badges+'</div>';
  if (s.forkedFrom) sc += '<div class="fork-from">\u21b3 '+s.forkedFrom.substring(0,12)+'</div>';

  let pc = '<div class="path-text" data-display="'+attr(s.displayName)+'" title="'+attr(s.cwd)+'">'+esc(s.displayName)+'</div>';
  if (s.gitBranch) pc += '<div class="branch-line"><span class="badge b-branch">'+esc(s.gitBranch)+'</span></div>';

  let tc = '<div class="time-text">'+relTime(s.mtime)+'</div><div class="size-text">'+s.sizeKB+' KB</div>';

  let mc = '<div class="msg-preview" id="preview-'+s.id+'">';
  if (s.recentMessages && s.recentMessages.length) {
    s.recentMessages.forEach(function(m) {
      var lbl = m.role==='you' ? '<span class="msg-l ml-you">You</span>' : '<span class="msg-l ml-cl">Claude</span>';
      mc += '<div class="msg-line">'+lbl+esc(m.text)+'</div>';
    });
  } else {
    mc += '<span style="color:var(--t3);font-size:11px">No messages</span>';
  }
  mc += '</div><div class="chat-log" id="chatlog-'+s.id+'" style="display:none"></div>';

  return '<tr data-sid="'+attr(s.id)+'" class="'+(s.active?'is-active':'')+'">'
    +'<td>'+sc+'</td><td>'+pc+'</td><td>'+tc+'</td><td>'+mc+'</td>'
    +'<td><div class="action-btns">'
    +'<button class="btn-action btn-resume" data-id="'+attr(s.id)+'" data-cwd="'+attr(s.cwd)+'" title="Resume in Ghostty">\u25B6</button>'
    +'<button class="btn-action btn-fork" data-id="'+attr(s.id)+'" data-cwd="'+attr(s.cwd)+'" title="Fork in Ghostty"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9"/><path d="M12 12v3"/></svg></button>'
    +'</div></td></tr>';
}

function updateStats() {
  document.getElementById('stats').innerHTML =
    '<span class="stat"><span class="dot dot-b"></span>'+total+' sessions</span>'
    +'<span class="stat"><span class="dot dot-g"></span>'+activeCount+' active</span>'
    +'<span class="stat" id="rtime">Updated just now</span>';
}

// ── Expand/collapse rows ────────────────────────────
let expandedId = null;
const historyCache = {};

async function toggleExpand(sid) {
  const row = document.querySelector('tr[data-sid="'+sid+'"]');
  if (!row) return;
  const log = document.getElementById('chatlog-'+sid);
  const preview = document.getElementById('preview-'+sid);
  if (!log || !preview) return;

  // Collapse if already expanded
  if (expandedId === sid) {
    row.classList.remove('expanded');
    log.style.display = 'none';
    preview.style.display = '';
    expandedId = null;
    return;
  }

  // Collapse previous
  if (expandedId) {
    const prev = document.querySelector('tr[data-sid="'+expandedId+'"]');
    if (prev) prev.classList.remove('expanded');
    const prevLog = document.getElementById('chatlog-'+expandedId);
    const prevPrev = document.getElementById('preview-'+expandedId);
    if (prevLog) prevLog.style.display = 'none';
    if (prevPrev) prevPrev.style.display = '';
  }

  // Expand this row
  expandedId = sid;
  row.classList.add('expanded');
  preview.style.display = 'none';
  log.style.display = '';

  // Load history (cached)
  if (historyCache[sid]) {
    log.innerHTML = renderChat(historyCache[sid]);
    log.scrollTop = log.scrollHeight;
    return;
  }

  log.innerHTML = '<div class="chat-loading">Loading history...</div>';
  try {
    const r = await fetch('/api/history/'+sid);
    const d = await r.json();
    historyCache[sid] = d.messages;
    log.innerHTML = renderChat(d.messages);
    log.scrollTop = log.scrollHeight;
  } catch(e) {
    log.innerHTML = '<div class="chat-loading">Failed to load</div>';
  }
}

function renderChat(msgs) {
  if (!msgs.length) return '<div class="chat-loading">No messages</div>';
  return msgs.map(m =>
    '<div class="chat-msg chat-msg-'+(m.role==='user'?'u':'a')+'">'
    +'<span class="cm-role">'+(m.role==='user'?'You':'Claude')+'</span>'
    +esc(m.text)+'</div>'
  ).join('');
}

// ── Scroll passthrough for chat logs ────────────────
document.addEventListener('wheel', e => {
  const log = e.target.closest('.chat-log');
  if (!log) return;
  const atTop = log.scrollTop <= 0 && e.deltaY < 0;
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 1 && e.deltaY > 0;
  if (atTop || atBottom) {
    // Let the page scroll instead
    log.style.pointerEvents = 'none';
    requestAnimationFrame(() => { log.style.pointerEvents = ''; });
  }
}, {passive: false});

// ── Events ──────────────────────────────────────────
document.addEventListener('click', e => {
  const resume = e.target.closest('.btn-resume');
  if (resume) { e.stopPropagation(); launch(resume.dataset.id, resume.dataset.cwd); return; }
  const forkBtn = e.target.closest('.btn-fork');
  if (forkBtn) { e.stopPropagation(); fork(forkBtn.dataset.id, forkBtn.dataset.cwd); return; }
  const sidEl = e.target.closest('.sid');
  if (sidEl && sidEl.dataset.copy) {
    e.stopPropagation();
    navigator.clipboard.writeText(sidEl.dataset.copy).then(()=>toast('Copied'));
    return;
  }
  const pathEl = e.target.closest('.path-text');
  if (pathEl && pathEl.dataset.display) {
    e.stopPropagation();
    acSelect(pathEl.dataset.display);
    return;
  }
  // Row click -> expand
  const row = e.target.closest('tr[data-sid]');
  if (row) toggleExpand(row.dataset.sid);
});

let searchTimer;
document.getElementById('search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => load(false, false), 300);
});

document.addEventListener('keydown', e => {
  if ((e.key === '/' || (e.metaKey && e.key === 'k')) && !['INPUT','SELECT'].includes(document.activeElement.tagName)) {
    e.preventDefault(); document.getElementById('search').focus();
  }
  if (e.key === 'Escape' && document.activeElement.id !== 'pathInput') {
    document.getElementById('search').value = '';
    acSelect('');
    document.getElementById('search').blur();
  }
});

function showMore() { load(true, false); }
function toggleSort() { sortDir = sortDir==='desc'?'live':'desc'; load(false, false); }

function doRefresh() {
  const btn = document.getElementById('rbtn');
  btn.classList.add('spin');
  load(false, true).then(() => { btn.classList.remove('spin'); toast('Refreshed'); });
}

setInterval(() => load(false, true), 5*60*1000);
setInterval(() => {
  const el = document.getElementById('rtime');
  if (!el||!lastRefresh) return;
  const s = Math.floor((Date.now()-lastRefresh)/1000);
  if (s < 5) el.textContent = 'Updated just now';
  else if (s < 60) el.textContent = 'Updated '+s+'s ago';
  else el.textContent = 'Updated '+Math.floor(s/60)+'m ago';
}, 10000);

function relTime(ts) {
  const d = Date.now()/1000-ts;
  if (d<60) return 'just now'; if (d<3600) return Math.floor(d/60)+'m ago';
  if (d<86400) return Math.floor(d/3600)+'h ago'; if (d<604800) return Math.floor(d/86400)+'d ago';
  if (d<2592000) return Math.floor(d/604800)+'w ago'; return new Date(ts*1000).toLocaleDateString();
}
function esc(s){if(!s)return'';const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function attr(s){return(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;')}
function trunc(s,n){return s&&s.length>n?s.slice(0,n)+'\u2026':s}
function toast(m,t){const e=document.createElement('div');e.className='toast toast-'+(t||'ok');e.textContent=m;document.getElementById('toasts').appendChild(e);setTimeout(()=>e.remove(),3000);}

load(false, false);

load(false, false);
</script>
</body>
</html>
"""


# ── Main ────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Claude Code Session Dashboard")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to listen on (default: {PORT})")
    parser.add_argument("--host", default=HOST, help=f"Host to bind to (default: {HOST})")
    args = parser.parse_args()

    _load_last_live()
    get_index()
    try:
        server = HTTPServer((args.host, args.port), Handler)
    except OSError as e:
        if e.errno == 48:
            print(f"Port {args.port} already in use. Dashboard may already be running.")
            print(f"Visit http://{args.host}:{args.port}")
            sys.exit(1)
        raise
    print(f"Claude Session Dashboard: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
