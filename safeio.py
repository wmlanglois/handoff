"""One-line guard against the Windows cp1252 console crash.

Model output and statutory text carry non-cp1252 characters (the minus sign U+2212, the
section sign, curly quotes). Printing them to a Windows console or a captured pipe whose
default encoding is cp1252 raises UnicodeEncodeError and kills the process mid-job -- even
when the work itself was fine. Every process entry point calls force_utf8() first.
"""
import sys


def force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
