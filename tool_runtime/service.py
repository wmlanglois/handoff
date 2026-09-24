"""Authenticated local tool service. Core tools: files, python_run, _upload.

Run only for trusted local workers: python_run executes code on this host. The service binds
file paths to per-job workspaces and persists call-id results so retries do not repeat writes.
"""
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
TOOLS = [
    {"type": "function", "function": {"name": "files", "description": "List, read or write files in this job workspace.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "read", "write"]},
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "python_run", "description": "Run Python code in this job workspace.",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
]
_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


def _workspace(root, job_id):
    if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
        raise ValueError("invalid job_id")
    ws = (Path(root) / "jobs" / job_id).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _path(ws, name):
    if not isinstance(name, str) or not name or "\\" in name or Path(name).is_absolute():
        raise ValueError("path must be relative to the job workspace")
    p = (ws / name).resolve()
    if not p.is_relative_to(ws) or p == ws:
        raise ValueError("path escapes the job workspace")
    return p


def execute(root, job_id, name, args):
    ws = _workspace(root, job_id)
    if name == "files":
        action = args.get("action")
        if action == "list":
            base = ws if not args.get("path") else _path(ws, args["path"])
            if not base.is_dir():
                raise ValueError("not a directory")
            return {"files": sorted(str(p.relative_to(ws)).replace("\\", "/") for p in base.rglob("*")
                                    if p.is_file() and not p.is_symlink())[:1000]}
        p = _path(ws, args.get("path"))
        if action == "read":
            return {"path": args["path"], "content": p.read_text(encoding="utf-8")}
        if action == "write":
            content = args.get("content")
            if not isinstance(content, str):
                raise ValueError("content must be text")
            data = content.encode("utf-8")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return {"path": args["path"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        raise ValueError("unknown files action")
    if name == "_upload":
        p = _path(ws, args.get("path"))
        data = base64.b64decode(args.get("base64", ""), validate=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return {"path": args["path"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if name == "python_run":
        code = args.get("code")
        if not isinstance(code, str):
            raise ValueError("code must be text")
        try:
            cp = subprocess.run([sys.executable, "-c", code], cwd=ws, capture_output=True,
                                text=True, timeout=60, encoding="utf-8", errors="replace")
            return {"stdout": cp.stdout[-20000:], "stderr": cp.stderr[-20000:], "exit_code": cp.returncode}
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "python_run exceeded 60 seconds", "exit_code": 124}
    raise ValueError("unknown tool: " + str(name))


def call(root, payload):
    """One idempotent call. A persisted result is returned verbatim on repeated call_id."""
    ws = _workspace(root, payload.get("job_id"))
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not _ID.fullmatch(call_id):
        raise ValueError("invalid call_id")
    receipts = ws / ".receipts"
    receipts.mkdir(exist_ok=True)
    receipt = receipts / (call_id + ".json")
    if receipt.exists():
        return json.loads(receipt.read_text(encoding="utf-8"))
    try:
        result = {"ok": True, "result": execute(root, payload["job_id"], payload.get("name"),
                                                payload.get("arguments") or {})}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:500]}
    tmp = receipts / (call_id + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(result, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, receipt)
    return result


def handler(root, token):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, value):
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if self.headers.get("Authorization") != "Bearer " + token:
                self._reply(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"ok": True, "service": "handoff-core-tools"})
            elif self.path == "/tools" and self._authorized():
                self._reply(200, {"tools": TOOLS})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path != "/call":
                self._reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 4_000_000:
                    raise ValueError("request too large")
                payload = json.loads(self.rfile.read(length))
                self._reply(200, call(root, payload))
            except Exception as exc:
                self._reply(400, {"ok": False, "error": str(exc)[:500]})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8041)
    ap.add_argument("--root", default=str(ROOT / "runs" / "tool-service"))
    ap.add_argument("--token-file", default=str(ROOT / "runs" / "tool-service" / "token.txt"))
    a = ap.parse_args()
    token_path = Path(a.token_file)
    if not token_path.exists():
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with token_path.open("x", encoding="utf-8") as f:
            f.write(secrets.token_hex(32) + "\n")
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 24:
        ap.error("token file must contain at least 24 characters")
    HTTPServer((a.host, a.port), handler(a.root, token)).serve_forever()


if __name__ == "__main__":
    main()
