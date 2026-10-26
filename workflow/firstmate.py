"""Firstmate: lets a firstmate first mate watch wf runs.

firstmate (github.com/kunchenguid/firstmate) never blocks its first mate on a
long-running process. It registers the process as a "source" and wakes the
first mate when the source produces a result. An outside tool joins in as a
trusted extension package implementing `process-event-adapter/1`
(firstmate's docs/extension-bindings.md): one executable that firstmate runs
with a verb argument (`handshake` or `invoke`), one JSON request on stdin, and
one JSON response on stdout.

This module is that executable's logic. A source is one wf run. The source's
config reference names its run folder: `wf-run:/abs/repo/.wf/runs/<id>`, or
`wf-run:/abs/runs-dir/<repo key>/<id>` for a run in a runs folder. A runs folder,
outside the repo, comes from --runs-dir, WF_RUNS_DIR or runs_dir in config.toml.

    source.poll      wait up to POLL_SECONDS for the run to need attention (operate.wait),
                     with the source as the consumer. So the poll reports each state once:
                     {status: "result", output: <the run's state as JSON>}, or "no-result".
                     A configuration with problems (config/) refuses the poll before the wait,
                     with the error "unavailable", so firstmate loses no state
    result.classify  wf-waiting, wf-failed, wf-stopped, wf-limit or wf-succeeded
    result.terminal  true once the run succeeded; the source then retires (a limit
                     pauses a run: `wf retry --limit` continues it, so the source stays)
    result.silent    always false: every state change is worth the first mate's attention

firstmate copies the package into its home and checks its digest, so the
package itself is a thin shim (write_package) whose shebang is this wf's
Python, which imports this module. A wf update changes what the shim runs;
firstmate's trust model is "trusted same-user code", and binding the package
is the operator's explicit consent to that.

firstmate keeps its built-in answers channel for its own adapters, so the
first mate answers a waiting run itself, with `wf answer` (see the wf skill in
integrations/firstmate/).

`wf continue` gives a run that succeeded a follow-up: a new pass of the same
run folder. The first mate registers it as a new source. Each state of the
follow-up carries its number (`follow_up`), so that pass reports and retires
as a run does.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import config, operate
from .runs import Run

EXTENSION_ID = "io.github.namikmesic.wf"
VERSION = "0.1.0"
ADAPTER = "wf"
CAPABILITY = "process-event-adapter"
POLL_SECONDS = 55  # firstmate's own example: a finite wait well inside the host's 5-minute bound
MAX_REQUEST = 65_536  # firstmate's envelope limit
MAX_OUTPUT = 30_000  # a raw result may be at most 32,768 bytes
TERMINAL = {"succeeded"}
REFERENCE = "wf-run:"


class _Refusal(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    verb = argv[1] if len(argv) > 1 else ""
    raw = sys.stdin.read(MAX_REQUEST + 1)
    if len(raw.encode("utf-8")) > MAX_REQUEST:
        print("wf-firstmate: the request is larger than 65,536 bytes", file=sys.stderr)
        return 1
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"wf-firstmate: the request isn't JSON: {error}", file=sys.stderr)
        return 1
    if not isinstance(request, dict):
        print("wf-firstmate: the request must be a JSON object", file=sys.stderr)
        return 1
    if verb == "handshake":
        print(json.dumps(handshake(request)))
        return 0
    if verb == "invoke":
        print(json.dumps(invoke(request)))
        return 0
    print(f"wf-firstmate: unknown verb `{verb}` (handshake or invoke)", file=sys.stderr)
    return 1


def handshake(request: dict) -> dict:
    names = (request.get("capability") or {}).get("adapter_names") or [ADAPTER]
    return {
        "schema": "firstmate.extension-handshake-response.v1",
        "request_id": request.get("request_id"),
        "extension_id": EXTENSION_ID,
        "extension_version": VERSION,
        "host_protocol": 1,
        "capability": CAPABILITY,
        "capability_version": 1,
        "adapter_names": [n for n in names if n == ADAPTER],
    }


def invoke(request: dict) -> dict:
    operation, data = request.get("operation"), request.get("input") or {}
    try:
        if operation == "source.poll":
            result = _poll(data)
        elif operation == "result.classify":
            result = {"classification": f"wf-{_status(data)}"}
        elif operation == "result.terminal":
            result = {"value": _status(data) in TERMINAL}
        elif operation == "result.silent":
            result = {"value": False}
        else:
            raise _Refusal("invalid-request", f"unknown operation `{operation}`")
    except _Refusal as refusal:
        return _response(request, None, {"code": refusal.code, "retryable": refusal.retryable,
                                         "diagnostic": str(refusal)[:500]})
    except Exception as error:  # a bug here must still be a well-formed reply
        return _response(request, None, {"code": "internal", "retryable": True,
                                         "diagnostic": f"{type(error).__name__}: {error}"[:500]})
    return _response(request, result, None)


def _response(request: dict, result: dict | None, error: dict | None) -> dict:
    return {"schema": "firstmate.extension-response.v1", "request_id": request.get("request_id"),
            "ok": error is None, "result": result, "error": error}


def _poll(data: dict) -> dict:
    reference, source = data.get("config_ref"), data.get("source_id")
    if not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", source):
        raise _Refusal("invalid-request", "source_id must be a path-safe name")
    if not isinstance(reference, str) or not reference.startswith(REFERENCE):
        raise _Refusal("invalid-request", f"config_ref must look like {REFERENCE}/absolute/path/to/<run folder>")
    folder = Path(reference[len(REFERENCE):])
    if not folder.is_absolute() or os.path.normpath(folder) != str(folder):
        raise _Refusal("invalid-request", "config_ref must name a normalized absolute path")
    if not (folder / "workflow" / "manifest.json").is_file():
        raise _Refusal("unavailable", f"{folder} isn't a wf run folder")
    try:
        run = Run.at(folder)
    except ValueError as error:  # a runs folder's run, with a damaged run.json
        raise _Refusal("unavailable", str(error)) from error
    try:  # before the wait, which marks the state that it finds as reported
        loaded = config.load()
    except config.ConfigError as error:
        raise _Refusal("unavailable", "\n".join(error.problems), retryable=True) from error
    with config.pinned(loaded):
        found = operate.wait(run, POLL_SECONDS, consumer=f"firstmate-{source}")
        if found is None:
            return {"status": "no-result", "output": ""}
        return {"status": "result", "output": _bounded(found, run.command("status", "--json"))}


def _bounded(state: dict, more: str) -> str:
    """The run's state as JSON, cut down to fit a firstmate result. `more` is the command
    that prints all of it."""
    text = json.dumps(state, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) <= MAX_OUTPUT:
        return text
    slim = {k: v for k, v in state.items() if k != "output"}
    if "output" in state:
        slim["output_file"] = str(Path(state["dir"]) / "output.json")
    slim["questions"] = [{**q, "message": q["message"][:2000], "schema": {"see": more}}
                         for q in state.get("questions", [])]
    text = json.dumps(slim, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) <= MAX_OUTPUT:
        return text
    keep = ("run", "status", "seq", "dir", "workflow", "follow_up", "nodes", "answer", "retry")
    return json.dumps({k: state[k] for k in keep if k in state} | {"more": more})


def _status(data: dict) -> str:
    try:
        status = json.loads(data.get("content") or "").get("status")
    except (json.JSONDecodeError, TypeError, AttributeError):  # TypeError: content isn't text
        return "unknown"
    return status if isinstance(status, str) and re.fullmatch(r"[a-z]+", status) else "unknown"


# ---------- the package ----------


class PackageError(Exception):
    pass


def write_package(dest: Path) -> dict:
    """Write a bindable firstmate extension package into `dest` (new or empty, outside any git repo)."""
    dest = Path(dest).expanduser().absolute()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise PackageError(f"{dest} isn't empty; pick a new folder")
    if _in_git_repo(dest):
        raise PackageError(f"{dest} is inside a git repository, which firstmate refuses for packages; "
                           "use a folder like ~/.local/share/firstmate-packages/wf")
    manifest = {
        "schema": "firstmate.extension-manifest.v1",
        "id": EXTENSION_ID,
        "version": VERSION,
        "host_protocols": [1],
        "entrypoint": "bin/wf-firstmate",
        "capabilities": [{"name": CAPABILITY, "versions": [1], "adapter_names": [ADAPTER]}],
        "required_consents": ["artifact-references"],  # the config reference names a run folder
    }
    (dest / "bin").mkdir(parents=True, exist_ok=True)
    (dest / "firstmate-extension.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    entry = dest / "bin" / "wf-firstmate"
    package_root = str(Path(__file__).resolve().parent.parent)  # where this wf's `workflow` package lives
    entry.write_text(
        f"#!{sys.executable}\n"
        '"""firstmate process-event-adapter/1 for wf runs: a shim around wf (workflow/firstmate.py)."""\n'
        f"import sys\n\nsys.path.insert(0, {package_root!r})  # this wf, even if it isn't installed\n\n"
        "from workflow.firstmate import main\n\nsys.exit(main())\n",
        encoding="utf-8",
    )
    for path in (dest, dest / "bin"):  # no group or world write: firstmate refuses those
        path.chmod(0o755)
    (dest / "firstmate-extension.json").chmod(0o644)
    entry.chmod(0o755)
    return manifest


def _in_git_repo(path: Path) -> bool:
    probe = path
    while not probe.exists():
        probe = probe.parent
    try:
        done = subprocess.run(["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


def instructions(dest: Path, manifest: dict) -> str:
    skill = Path(__file__).resolve().parent.parent / "integrations" / "firstmate" / "wf" / "SKILL.md"
    return f"""Wrote the firstmate extension package {manifest['id']} {manifest['version']} to {dest}

In your firstmate checkout (for another home, set FM_HOME on each command):

1. Bind it, once per firstmate home. This is firstmate's consent step:
     bin/fm-extension.sh bind {dest} --adapter {ADAPTER} --trust-same-user-code --consent artifact-references
     bin/fm-extension.sh verify {manifest['id']}

2. Teach your first mate wf: copy {skill}
   to .agents/skills/wf/SKILL.md, and add its trigger line to .agents/skills/agent-skill-trigger-index/SKILL.md.

3. The first mate then watches each run it starts:
     wf run <workflow.yaml> name=value ... --detach --json
     bin/fm-procevent.sh register-extension {ADAPTER} <source-id> --config-ref "wf-run:<run folder from the JSON>"
"""
