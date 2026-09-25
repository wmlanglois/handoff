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
for _p in (str(ROOT), str(ROOT / "run")):   # state_lock lives in run/; the service runs standalone
    if _p not in sys.path:
        sys.path.insert(0, _p)
from state_lock import exclusive_file
TOOLS = [
    {"type": "function", "function": {"name": "files", "description": "Read or change workspace files. For large files write a small first section, then append or edit with expected_sha256 from the last result. Edit replaces exactly one nonempty old_text. Each mutation receipts the complete resulting file; no final full rewrite is needed. Read supports start_line and max_lines.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "read", "write", "edit", "append"]},
         "path": {"type": "string"}, "content": {"type": "string"},
         "old_text": {"type": "string"}, "expected_sha256": {"type": "string"},
         "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}}, "required": ["action"]}}},
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
    if p.relative_to(ws).parts[0] == ".receipts":
        raise ValueError("service receipt storage is private")
    return p


def _atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Short, target-independent temp name: receipt names are already ~75 chars, and repeating them
    # here pushed deep Windows checkouts past MAX_PATH (260), so every tool write failed there.
    tmp = path.with_name('.' + secrets.token_hex(8) + '.tmp')
    try:
        with tmp.open('xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _file_change(ws, args):
    p = _path(ws, args.get('path'))
    before = p.read_bytes() if p.exists() else None
    before_sha = hashlib.sha256(before).hexdigest() if before is not None else None
    content = args.get('content')
    if not isinstance(content, str):
        raise ValueError('content must be text')
    action = args['action']
    if action in ('append', 'edit'):
        if before is None or not args.get('expected_sha256') or args['expected_sha256'] != before_sha:
            raise ValueError('stale or missing expected_sha256; read current file before editing')
        text = before.decode('utf-8')
        if action == 'append':
            data = (text + content).encode('utf-8')
        else:
            old = args.get('old_text')
            if not isinstance(old, str) or not old or text.count(old) != 1:
                raise ValueError('edit requires exactly one nonempty old_text match')
            data = text.replace(old, content, 1).encode('utf-8')
        if data == before:
            raise ValueError('edit must change the file')
    else:
        data = content.encode('utf-8')
    result = {'path': args['path'], 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
              'before_sha256': before_sha, 'operation': action}
    return p, data, result


def execute(root, job_id, name, args):
    ws = _workspace(root, job_id)
    if name == "files":
        action = args.get("action")
        if action == "list":
            base = ws if not args.get("path") else _path(ws, args["path"])
            if not base.is_dir():
                raise ValueError("not a directory")
            return {"files": sorted(str(p.relative_to(ws)).replace("\\", "/") for p in base.rglob("*")
                                    if p.is_file() and not p.is_symlink()
                                    and '.receipts' not in p.relative_to(ws).parts)[:1000]}
        p = _path(ws, args.get("path"))
        if action == "read":
            raw = p.read_bytes()
            text = raw.decode('utf-8')
            start, count = args.get('start_line', 1), args.get('max_lines')
            if type(start) is not int or start < 1 or (count is not None and (type(count) is not int or count < 1)):
                raise ValueError('line ranges must be positive integers')
            lines = text.splitlines(keepends=True)
            return {"path": args["path"], "content": ''.join(lines[start-1:None if count is None else start-1+count]),
                    'sha256': hashlib.sha256(raw).hexdigest(), 'total_lines': len(lines), 'start_line': start}
        if action in ("write", "append", "edit"):
            p, data, result = _file_change(ws, args)
            _atomic_bytes(p, data)
            return result
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
    with exclusive_file(receipts / '.call-lock', timeout=180):
        return _locked_call(root, payload, ws, receipts, call_id)


def _locked_call(root, payload, ws, receipts, call_id):
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    receipt = receipts / (call_id + ".json")
    identity = receipts / (call_id + '.request')
    if identity.exists() and identity.read_text() != fingerprint:
        raise ValueError('call_id reused with a different payload')
    if receipt.exists():
        if not identity.exists():
            raise ValueError('legacy receipt has no request identity; explicit reconciliation required')
        return json.loads(receipt.read_text(encoding="utf-8"))
    _atomic_bytes(identity, fingerprint.encode())
    intent = receipts / (call_id + '.intent')
    try:
        args = payload.get('arguments') or {}
        if payload.get('name') == 'files' and args.get('action') in ('write', 'append', 'edit'):
            if intent.exists():
                saved = json.loads(intent.read_text(encoding='utf-8'))
                p = _path(ws, args.get('path'))
                data = base64.b64decode(saved['data'], validate=True)
                info = saved['result']
                current = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
                if current not in (info['before_sha256'], info['sha256']):
                    raise ValueError('interrupted edit conflicts with current file; manual reconciliation required')
            else:
                p, data, info = _file_change(ws, args)
                _atomic_bytes(intent, json.dumps({'data': base64.b64encode(data).decode(),
                                                 'result': info}).encode())
            _atomic_bytes(p, data)
            result = {'ok': True, 'result': info}
        else:
            result = {"ok": True, "result": execute(root, payload["job_id"], payload.get("name"), args)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:500]}
    tmp = receipts / (call_id + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(result, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, receipt)
    intent.unlink(missing_ok=True)
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
