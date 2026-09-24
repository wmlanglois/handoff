"""Kickoff dashboard: collect a whole project spec in ONE pass, in a browser, then hand it off.

    python run/kickoff.py                      # opens http://127.0.0.1:8777
    python run/kickoff.py --port 9000 --name my-project

WHY A DASHBOARD AND NOT A CHAT. Asking these questions one at a time loses the thread: each answer
gets interpreted in isolation and the reader never sees the shape of the whole project. This was
observed directly -- a step-by-step intake run produced field-by-field parsing and no scoped
project at all. So every answer is collected FIRST and injected TOGETHER, so the reader derives one
scoped project instead of fourteen disconnected fields.

UI, as specified by the operator: radio buttons for the choice questions, text boxes with a
microphone for the open ones. The mic uses the browser's own speech recognition, so nothing is sent
anywhere and the transcript lands in the box as editable text.

WHAT IT WRITES. intake/<name>.md, VERBATIM. The operator's words are the source of truth and are
never paraphrased on the way in, because a paraphrase at intake is an inference dressed as a
requirement. Stdlib only: no Flask, no npm, nothing to install.
"""
import argparse
import html
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "run"))
from intake import QUESTIONS, FIELD  # noqa: E402  single source -- never re-type the questions

OUT_DIR = ROOT / "intake"
SEP = "; "                      # how multi-select answers are joined for storage
MULTI = {"S3", "V1"}            # the two questions that say "pick all that apply"
STATE = {"name": "kickoff", "saved": None}


def visible(branch):
    """Q0 chooses the branch. S6a is for people who can check the work themselves, S6b for those
    who cannot -- asking both would make one of them noise."""
    return [q for q in QUESTIONS if q[0] == "Q0" or branch in q[4]]


def _field(qid, prompt, choices, multi):
    lab = ('<label class="q" for="{0}"><span class="qid">{0}</span>{1}</label>'
           .format(qid, html.escape(prompt)))
    if choices:
        kind = "checkbox" if multi else "radio"
        opts = "".join(
            '<label class="opt"><input type="{0}" name="{1}" value="{2}"> '
            '<b>{2}</b> <span>{3}</span></label>'.format(kind, qid, html.escape(k), html.escape(v))
            for k, v in choices.items())
        note = ('<textarea name="{0}__note" rows="2" placeholder="anything else about this '
                '(optional)"></textarea>'.format(qid))
        return '<div class="row">{0}<div class="opts">{1}</div>{2}</div>'.format(lab, opts, note)
    return ('<div class="row">{0}<div class="ta">'
            '<textarea id="{1}" name="{1}" rows="4" '
            'placeholder="Say as much as you want. Rambling is fine."></textarea>'
            '<button type="button" class="mic" data-for="{1}" title="dictate">MIC</button>'
            '</div></div>').format(lab, qid)


CSS = """
:root{--bg:#12131a;--fg:#e8e6e3;--mut:#9a97a8;--line:#2a2b36;--acc:#7dd3a0;--card:#1a1b24}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:24px;margin:0 0 6px}
.sub{color:var(--mut);margin:0 0 28px}
.row{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:16px;margin:0 0 14px}
.q{display:block;font-weight:600;margin:0 0 10px}
.qid{display:inline-block;min-width:40px;color:var(--acc);
  font:600 12px ui-monospace,SFMono-Regular,monospace}
textarea{width:100%;background:#0e0f15;color:var(--fg);border:1px solid var(--line);
  border-radius:7px;padding:10px;font:14px/1.5 inherit;resize:vertical}
textarea:focus{outline:2px solid var(--acc);outline-offset:1px}
.ta{position:relative}
.mic{position:absolute;right:8px;bottom:10px;background:#242531;color:var(--mut);
  border:1px solid var(--line);border-radius:6px;padding:4px 9px;
  font:600 11px ui-monospace,monospace;cursor:pointer}
.mic.on{background:var(--acc);color:#0e0f15;border-color:var(--acc)}
.opts{display:flex;flex-direction:column;gap:7px;margin-bottom:10px}
.opt{display:flex;gap:8px;align-items:baseline;cursor:pointer}
.opt span{color:var(--mut)}
button.go{background:var(--acc);color:#0e0f15;border:0;border-radius:8px;padding:12px 22px;
  font:600 15px inherit;cursor:pointer}
a{color:var(--acc)}
.note{color:var(--mut);font-size:13px;margin-top:18px}
"""

JS = """
var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
document.querySelectorAll('.mic').forEach(function (btn) {
  if (!SR) { btn.textContent = 'NO MIC'; btn.disabled = true;
             btn.title = 'this browser has no speech API'; return; }
  var rec = null;
  btn.addEventListener('click', function () {
    var box = document.getElementById(btn.dataset.for);
    if (rec) { rec.stop(); return; }
    rec = new SR(); rec.continuous = true; rec.interimResults = false; rec.lang = 'en-US';
    btn.classList.add('on'); btn.textContent = 'STOP';
    rec.onresult = function (e) {
      var t = '';
      for (var i = e.resultIndex; i < e.results.length; i++) { t += e.results[i][0].transcript; }
      box.value = (box.value ? box.value.replace(/\\s*$/, ' ') : '') + t.trim();
    };
    function off() { btn.classList.remove('on'); btn.textContent = 'MIC'; rec = null; }
    rec.onerror = off; rec.onend = off;
    rec.start();
  });
});
"""


def page(branch):
    rows = "".join(_field(q[0], q[2], q[3], q[0] in MULTI) for q in visible(branch))
    other = "B" if branch == "A" else "A"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Project Kickoff</title><style>{css}</style></head><body><div class="wrap">'
        '<h1>Project Kickoff</h1>'
        '<p class="sub">Answer everything in one pass. Nothing is interpreted until you submit, so '
        'the whole picture is read at once. Skip anything you genuinely cannot answer &mdash; a gap '
        'is recorded as a gap, never guessed. Branch <b>{b}</b> '
        '(<a href="/?branch={o}">switch to {o}</a>).</p>'
        '<form method="POST" action="/save">{rows}'
        '<button class="go" type="submit">Save spec</button></form>'
        '<p class="note">Saved verbatim to {where}. Your exact words are the record.</p>'
        '</div><script>{js}</script></body></html>'
    ).format(css=CSS, js=JS, b=branch, o=other, rows=rows,
             where=html.escape(STATE.get("package") or "intake/"))


def to_markdown(answers, name):
    """Verbatim, grouped, with each question's text beside its answer so a reader who was not in
    the room has the prompt as well as the reply."""
    out = ["# Intake: {0}".format(name), "",
           "Collected in one pass via the kickoff dashboard. Answers are VERBATIM.", ""]
    section = None
    titles = {"branch": "Branch", "spec": "Spec", "verifier": "Verification",
              "environment": "Environment"}
    for qid, sect, prompt, choices, _br in QUESTIONS:
        val = (answers.get(qid) or "").strip()
        note = (answers.get(qid + "__note") or "").strip()
        if not val and not note:
            continue
        if sect != section:
            section = sect
            out += ["## " + titles.get(sect, sect), ""]
        out += ["**{0}** ({1}) -- {2}".format(qid, FIELD.get(qid, ""), prompt), ""]
        if choices and val:
            picked = [c.strip() + " = " + choices.get(c.strip(), c.strip())
                      for c in val.split(SEP) if c.strip()]
            out += ["> " + "; ".join(picked), ""]
        elif val:
            out += ["> " + val.replace("\n", "\n> "), ""]
        if note:
            out += ["> _note:_ " + note.replace("\n", "\n> "), ""]
    answered = sum(1 for q in QUESTIONS if (answers.get(q[0]) or "").strip())
    out += ["---", "",
            "Answered {0} of {1} questions. Unanswered questions are ABSENT, not empty: treat each "
            "as an open gap to raise with the operator, never as a value to invent."
            .format(answered, len(QUESTIONS))]
    return "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, code=200):
        blob = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        if self.path.startswith("/done"):
            return self._send(
                "<body style='background:#12131a;color:#e8e6e3;font:16px sans-serif;padding:60px'>"
                "<h2>Saved</h2><p>{0}</p><p>You can close this tab.</p></body>"
                .format(html.escape(str(STATE["saved"]))))
        branch = "A"
        if "branch=" in self.path:
            branch = "B" if self.path.split("branch=")[1][:1].upper() == "B" else "A"
        self._send(page(branch))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = parse_qs(self.rfile.read(n).decode("utf-8"), keep_blank_values=True)
        answers = {k: SEP.join(v) if len(v) > 1 else (v[0] if v else "") for k, v in raw.items()}
        pkg = STATE.get("package") or ""
        if pkg:
            dest = Path(pkg)
            dest.mkdir(parents=True, exist_ok=True)
            import intake as intake_mod
            for qid, _sect, _prompt, _choices, _br in QUESTIONS:
                val = (answers.get(qid) or "").strip()
                note = (answers.get(qid + "__note") or "").strip()
                text = val if not note else (val + "\n" + note).strip()
                if text:
                    intake_mod.answer(STATE["name"], qid, text, directory=dest, source="user")
            md = dest / "kickoff.md"
        else:
            OUT_DIR.mkdir(exist_ok=True)
            md = OUT_DIR / (STATE["name"] + ".md")
            (OUT_DIR / (STATE["name"] + ".answers.json")).write_text(
                json.dumps(answers, indent=2), encoding="utf-8")
        md.write_text(to_markdown(answers, STATE["name"]), encoding="utf-8")
        STATE["saved"] = md
        print("saved " + str(md))
        self.send_response(303)
        self.send_header("Location", "/done")
        self.end_headers()


def main():
    ap = argparse.ArgumentParser(description="One-pass project kickoff dashboard")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--name", default="kickoff", help="output stem written under intake/")
    ap.add_argument("--package", default="",
                    help="project package directory; answers are stored there verbatim")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    STATE["name"] = args.name
    STATE["package"] = args.package
    url = "http://127.0.0.1:{0}".format(args.port)
    print("kickoff dashboard: {0}   (ctrl-c to stop)   -> intake/{1}.md".format(url, args.name))
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
