#!/usr/bin/env python3
"""raw-drop-server.py — network ingest inbox for a claude-obsidian vault.

A tiny, dependency-free HTTP service that lets agentic tools running in OTHER
containers/hosts deposit findings into a vault's `.raw/` directory over the
network. Files land in `.raw/` exactly like a manual drop, so the normal
"ingest all" workflow picks them up on the next session. The service NEVER
ingests, mutates `wiki/` pages, or runs any pipeline — its entire job is "write
one file into the .raw/ sandbox, safely".

Transport rung: this implements the "thin HTTP drop service" option from
`wiki/references/transport-fallback.md` — for network-isolated, no-shared-
filesystem topologies where remote agents must POST findings to the vault.

── Run (from inside a vault, or with the vault path supplied) ────────────────
    RAW_DROP_TOKEN="$(openssl rand -hex 32)" python3 scripts/raw-drop-server.py
    # or, pointing at a specific vault:
    RAW_DROP_TOKEN=... RAW_DROP_VAULT=/vaults/My-Second-Brain \
        python3 scripts/raw-drop-server.py

── Config (env) ─────────────────────────────────────────────────────────────
    RAW_DROP_TOKEN        (required) shared bearer secret; server exits if unset
    RAW_DROP_VAULT        vault root. If unset, falls back to CLAUDE_OBSIDIAN_VAULT,
                          then CWD if it looks like a vault
    RAW_DROP_BIND         default 0.0.0.0   (listen address)
    RAW_DROP_PORT         default 8765      (0 = OS-assigned; printed at startup)
    RAW_DROP_MAX_BYTES    default 10485760  (10 MiB per drop)
    RAW_DROP_MAX_INFLIGHT default 16        (concurrent drops; excess -> 503)
    RAW_DROP_RATE_PER_MIN default 120       (drops/min; excess -> 429; 0 disables)
    RAW_DROP_MAX_LOG_BYTES default 5242880  (rotate audit log past this size)

NOTE on hardening: auth + the caps above bound the obvious abuse, but this is a
single-process server. For internet-facing or untrusted networks, put it behind
a reverse proxy (TLS + IP allowlist + rate limiting) — do not expose it raw.

── Client (from another container) ──────────────────────────────────────────
    curl -sS -X POST http://VAULT_HOST:8765/drop \
      -H "Authorization: Bearer $RAW_DROP_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"filename":"k8s-findings.md","agent":"scout-1",
           "source_url":"https://...","content":"# Finding\n..."}'
    # binary (e.g. a PDF): set "encoding":"base64" and base64 the content.

Endpoints:
    GET  /health   -> {"ok": true}                      (no auth; no internal state)
    POST /drop     -> {"ok": true, "path": ".raw/..."}  (auth)
    GET  /pending  -> {"pending": [...]}                (auth; un-ingested drops)
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


# ── Vault resolution ──────────────────────────────────────────────────────────
# A plugin script does NOT live inside the vault (split install: plugin at
# /opt/claude-obsidian, vaults at /vaults/<name>), so we resolve the vault from
# env or the working directory — never from the script's own location.
def _resolve_vault() -> Path:
    for env in ("RAW_DROP_VAULT", "CLAUDE_OBSIDIAN_VAULT"):
        v = os.environ.get(env)
        if v:
            return Path(v).expanduser().resolve()
    cwd = Path.cwd()
    if any((cwd / marker).exists() for marker in (".obsidian", ".raw", "wiki")):
        return cwd.resolve()
    raise SystemExit(
        "FATAL: cannot locate a vault. Set RAW_DROP_VAULT=/path/to/vault "
        "(or CLAUDE_OBSIDIAN_VAULT), or run from inside the vault directory."
    )


# ── Config ───────────────────────────────────────────────────────────────────
VAULT_ROOT = _resolve_vault()
RAW_DIR = VAULT_ROOT / ".raw"
META_DIR = VAULT_ROOT / ".vault-meta"
LOG_FILE = META_DIR / "raw-drop.log"
MANIFEST = RAW_DIR / ".manifest.json"

TOKEN = os.environ.get("RAW_DROP_TOKEN", "")
BIND = os.environ.get("RAW_DROP_BIND", "0.0.0.0")
PORT = int(os.environ.get("RAW_DROP_PORT", "8765"))
MAX_BYTES = int(os.environ.get("RAW_DROP_MAX_BYTES", str(10 * 1024 * 1024)))
MAX_INFLIGHT = int(os.environ.get("RAW_DROP_MAX_INFLIGHT", "16"))
RATE_PER_MIN = int(os.environ.get("RAW_DROP_RATE_PER_MIN", "120"))
MAX_LOG_BYTES = int(os.environ.get("RAW_DROP_MAX_LOG_BYTES", str(5 * 1024 * 1024)))

# Only these extensions are accepted. Markdown/text/json are written verbatim;
# anything else must arrive base64-encoded ("encoding":"base64").
ALLOWED_EXT = {".md", ".txt", ".json", ".pdf", ".csv", ".html"}
TEXT_EXT = {".md", ".txt", ".json", ".csv", ".html"}
# No space in the allowlist: keeps generated filenames shell-safe (spaces -> '-').
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Backpressure: cap concurrent drops (thread/memory exhaustion) and rate (disk).
_INFLIGHT = threading.BoundedSemaphore(max(1, MAX_INFLIGHT))


class RateLimiter:
    """Fixed-window request limiter. Coarse but cheap; refined limiting belongs
    in a reverse proxy (see the hardening note in the module docstring)."""

    def __init__(self, max_per_window: int, window: float = 60.0):
        self.max = max_per_window
        self.window = window
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._count = 0

    def allow(self) -> bool:
        if self.max <= 0:
            return True
        with self._lock:
            now = time.monotonic()
            if now - self._start >= self.window:
                self._start = now
                self._count = 0
            if self._count >= self.max:
                return False
            self._count += 1
            return True


_LIMITER = RateLimiter(RATE_PER_MIN)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log(event: dict) -> None:
    event["ts"] = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = json.dumps(event, ensure_ascii=False)
    try:
        META_DIR.mkdir(parents=True, exist_ok=True)
        # size-based rotation so a long-running / hammered server can't grow the
        # audit log without bound (keeps a single .1 backup)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            os.replace(LOG_FILE, LOG_FILE.parent / (LOG_FILE.name + ".1"))
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr)


def sanitize_filename(raw: str) -> tuple[str, str]:
    """Reduce an untrusted filename to a safe (stem, ext). Path-traversal proof:
    keep only the basename and an allowlisted character set."""
    name = os.path.basename((raw or "").strip()).strip()
    if not name:
        name = "finding.md"
    stem, dot, ext = name.rpartition(".")
    if not dot:  # no extension supplied
        stem, ext = name, "md"
    ext = "." + _SAFE.sub("", ext).lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"extension {ext!r} not allowed (allowed: {sorted(ALLOWED_EXT)})")
    stem = _SAFE.sub("-", stem).strip("-.") or "finding"
    return stem[:80], ext


def provenance_header(meta: dict) -> str:
    """A small commented JSON block prepended to text drops so the ingest step
    knows where the finding came from. Kept as an HTML comment so it renders
    invisibly in Obsidian but stays machine-readable. Any `-->` inside the
    untrusted values is neutralized so a caller can't close the comment early."""
    fields = {
        "dropped_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent": meta.get("agent", "unknown"),
        "source_url": meta.get("source_url", ""),
        "tags": meta.get("tags", []),
    }
    body = json.dumps(fields, ensure_ascii=False, indent=2).replace("-->", "--\\u003e")
    return f"<!-- raw-drop-provenance\n{body}\n-->\n\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "raw-drop/1.0"

    # — helpers —
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        got = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        # constant-time comparison to avoid token-length/byte timing leaks
        return hmac.compare_digest(got, expected)

    def log_message(self, *a):  # silence default stderr spam; we log ourselves
        pass

    # — routes —
    def do_GET(self):
        if self.path == "/health":
            # No internal state (no vault path) on the unauthenticated endpoint.
            self._send(200, {"ok": True, "service": "raw-drop"})
            return
        if self.path == "/pending":
            if not self._authed():
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            self._send(200, {"ok": True, "pending": self._pending()})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/drop":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._authed():
            log({"event": "drop", "status": 401, "remote": self.client_address[0]})
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        if not _LIMITER.allow():
            self._send(429, {"ok": False, "error": "rate limit exceeded"})
            return
        # concurrency cap: bound threads + peak memory from large concurrent reads
        if not _INFLIGHT.acquire(blocking=False):
            self._send(503, {"ok": False, "error": "server busy"})
            return
        try:
            self._handle_drop()
        finally:
            _INFLIGHT.release()

    def _handle_drop(self):
        # size guard via Content-Length, then hard cap on the actual read
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"ok": False, "error": "bad Content-Length"})
            return
        if length <= 0 or length > MAX_BYTES:
            self._send(413, {"ok": False, "error": f"body must be 1..{MAX_BYTES} bytes"})
            return
        raw_body = self.rfile.read(length)

        ctype = self.headers.get("Content-Type", "")
        try:
            if ctype.startswith("application/json"):
                meta = json.loads(raw_body.decode("utf-8"))
                content = meta.get("content", "")
                if meta.get("encoding") == "base64":
                    payload = base64.b64decode(content, validate=True)
                else:
                    payload = content.encode("utf-8")
                fname = meta.get("filename", "")
            else:
                # raw body: metadata travels in headers
                meta = {
                    "agent": self.headers.get("X-Agent", "unknown"),
                    "source_url": self.headers.get("X-Source-Url", ""),
                }
                payload = raw_body
                fname = self.headers.get("X-Filename", "finding.md")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "malformed body"})
            return
        except (binascii.Error, ValueError):
            self._send(400, {"ok": False, "error": "invalid base64 content"})
            return

        try:
            stem, ext = sanitize_filename(fname)
        except ValueError as e:
            self._send(415, {"ok": False, "error": str(e)})
            return

        # text drops get a provenance header; binary drops are written verbatim
        if ext in TEXT_EXT and meta.get("encoding") != "base64":
            payload = provenance_header(meta).encode("utf-8") + payload

        ts = _now().strftime("%Y%m%dT%H%M%SZ")
        agent = _SAFE.sub("-", str(meta.get("agent", "agent")))[:32] or "agent"
        short = uuid.uuid4().hex[:8]
        out_name = f"{ts}-{agent}-{stem}-{short}{ext}"
        out_path = RAW_DIR / out_name

        try:
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            tmp.write_bytes(payload)
            tmp.replace(out_path)  # atomic within the same filesystem
        except OSError as e:
            log({"event": "drop", "status": 500, "error": str(e)})
            self._send(500, {"ok": False, "error": "write failed"})
            return

        rel = f".raw/{out_name}"
        log({"event": "drop", "status": 201, "remote": self.client_address[0],
             "agent": agent, "path": rel, "bytes": len(payload)})
        self._send(201, {"ok": True, "path": rel, "bytes": len(payload)})

    # — pending = files in .raw/ not yet recorded in the manifest —
    def _pending(self) -> list[str]:
        ingested = set()
        try:
            data = json.loads(MANIFEST.read_text(encoding="utf-8"))
            ingested = set(data.get("sources", {}).keys())
        except (OSError, json.JSONDecodeError):
            pass
        out = []
        for p in sorted(RAW_DIR.glob("*")):
            if p.name.startswith(".") or p.suffix == ".tmp":
                continue
            if f".raw/{p.name}" not in ingested:
                out.append(p.name)
        return out


class DropServer(ThreadingHTTPServer):
    daemon_threads = True       # don't let in-flight handlers block shutdown
    allow_reuse_address = True  # fast rebind (also helps the test harness)


def main() -> int:
    if not TOKEN:
        print("FATAL: RAW_DROP_TOKEN is unset. Refusing to start an unauthenticated "
              "write endpoint.\n  Generate one:  export RAW_DROP_TOKEN=\"$(openssl rand -hex 32)\"",
              file=sys.stderr)
        return 2
    if len(TOKEN) < 16:
        print("FATAL: RAW_DROP_TOKEN too short (<16 chars). Use a strong secret.", file=sys.stderr)
        return 2
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    META_DIR.mkdir(parents=True, exist_ok=True)
    httpd = DropServer((BIND, PORT), Handler)
    bound_port = httpd.socket.getsockname()[1]  # real port (PORT may be 0)
    log({"event": "start", "bind": BIND, "port": bound_port, "vault": str(VAULT_ROOT),
         "max_bytes": MAX_BYTES, "max_inflight": MAX_INFLIGHT, "rate_per_min": RATE_PER_MIN})
    print(f"raw-drop listening on http://{BIND}:{bound_port}  ->  {RAW_DIR}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log({"event": "stop"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
