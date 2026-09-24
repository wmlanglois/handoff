"""Structured, noisy logging. Every fleet component writes one timestamped JSONL
per run so troubleshooting pulls a file instead of rerunning the test. Also echoes
a compact line to the console.
"""
import datetime as dt
import json
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"

def logger(component, echo=True):
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{component}-{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl"
    fh = path.open("a", encoding="utf-8")

    def _show(value):
        try:
            return str(value)
        except Exception:
            return repr(value)

    def emit(event, **fields):
        rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
               "component": component, "event": event, **fields}
        fh.write(json.dumps(rec, ensure_ascii=False, default=repr) + "\n")
        fh.flush()
        if echo:
            extra = " ".join("{0}={1}".format(k, _show(v))
                             for k, v in fields.items() if k not in ("error",))
            line = f"  - {event} {extra}".rstrip()
            if fields.get("error"):
                line += "  ERR: {0}".format(_show(fields["error"])[:160])
            print(line, flush=True)
        return rec

    emit.path = path
    emit.close = fh.close
    return emit
