"""Structured, noisy logging. Every fleet component writes one timestamped JSONL
per run so troubleshooting pulls a file instead of rerunning the test. Also echoes
a compact line to the console.
"""
import datetime as dt
import json
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"   # default only; see _runs()


def _runs():
    """The shared run-state root (FLEET_RUNS_DIR aware). Event logs are evidence the architect reads
    (failure text, stop reasons), so they must land where it looks, not in whichever checkout ran."""
    try:
        import fleet
        return fleet.runs_root()
    except Exception:
        return RUNS


def logger(component, echo=True):
    runs = _runs()
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{component}-{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl"
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
