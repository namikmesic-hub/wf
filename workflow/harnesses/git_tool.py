"""Git tool: read-only git for agents that get no shell.

A `read-only` agent still needs to see what changed (a reviewer reads `git diff`),
but a shell, even one limited by command prefix, can write: `git diff
--output=FILE` writes a file, and `--ext-diff` or a textconv filter runs a
program from the repo's config. So read-only agents get this tool instead of
a shell. It runs one of diff, log, show, blame or status:
- without a shell: the arguments go straight to git, so `>`, `;` and `$(...)`
  mean nothing;
- refusing options that write a file (`--output`), run a configured program
  (`--ext-diff`, `--textconv`, and `--help`, which opens the configured man
  viewer) or read files outside the repo (`--no-index`, `--contents`),
  including their abbreviations, since git accepts `--ext` for `--ext-diff`;
- refusing paths outside the repo: `git diff /etc/passwd x` turns on
  --no-index by itself when a path lies outside the work tree. Option values
  are checked too (`--ignore-revs-file=/etc/hosts`, `-O/etc/hosts`, and `-wO...`,
  since git reads bundled short options): git reads such files and its error
  messages quote them back;
- refusing blame's `-S <file>` (revisions read from a file), bundled or not;
- with external diff programs, textconv filters and the fsmonitor hook
  (which status and diff would run) switched off, without environment
  variables that point git at another repository or add configuration (a git
  hook sets some), and
  --no-optional-locks, so even `git status` leaves the index alone;
- with no stdin and no pager, within a time limit, reading at most LIMIT
  characters of output (the rest isn't even read into memory).

run() is plain Python. Each adapter wraps it in its own tool type, like
submit_result.
"""

import subprocess
import tempfile
import threading
from pathlib import Path

from ..workspaces import git_env

NAME = "git"
DESCRIPTION = (
    "Run a read-only git command in the workspace: diff, log, show, blame or status. "
    'Pass the arguments as a list, e.g. ["diff", "main...HEAD"] or ["log", "--oneline", "-20"]. '
    "No shell: pipes, redirects and other commands aren't available."
)
SCHEMA = {
    "type": "object",
    "properties": {"args": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                            "description": 'git arguments, starting with the command, e.g. ["diff", "HEAD~1"]'}},
    "required": ["args"],
    "additionalProperties": False,
}
COMMANDS = ("diff", "log", "show", "blame", "status")
REFUSED = (
    "--output",  # writes a file
    "--ext-diff", "--textconv", "--help",  # run a program from git's config
    "--no-index", "--contents",  # read files outside the repo
)
EXACT = ("--text",)  # real options that only look like abbreviations of a refused one
LIMIT = 50_000
TIMEOUT = 60


def refused(arg: str) -> bool:
    """Is this one of REFUSED, or an abbreviation of one (git accepts `--ext` for `--ext-diff`)?"""
    name = arg.split("=", 1)[0]
    if name in EXACT:  # git matches an exact option before any abbreviation
        return False
    return name.startswith("--") and len(name) > 2 and any(flag.startswith(name) for flag in REFUSED)


def outside(cwd: Path, arg: str) -> bool:
    """Does this argument name a path outside the repo? Revisions like `main...HEAD` don't:
    only an absolute path, a `..` path component, or something that exists on disk is checked."""
    candidate = Path(arg)
    if not (candidate.is_absolute() or ".." in arg.split("/") or (cwd / candidate).exists()):
        return False
    resolved = (candidate if candidate.is_absolute() else cwd / candidate).resolve()
    return not resolved.is_relative_to(cwd.resolve())


# Short options that take a value, which can follow in the same argument (-L10,20, -Sneedle),
# per command: the same letter can be a plain flag in one command and take a value in another
# (-l and -n are flags in blame, so `blame -lS<file>` is -l, then -S with a revs file).
TAKES_VALUE = {
    "blame": set("LSCM"),
    "diff": set("LSOGUnIlBMCX"), "log": set("LSOGUnIlBMCX"), "show": set("LSOGUnIlBMCX"),
    "status": set("u"),
}


def _short(arg: str, command: str) -> list[tuple[str, str]]:
    """The short options bundled in one argument, with the value the last one carries, as git
    reads them: -wO/etc/x -> [("w", ""), ("O", "/etc/x")]; -L/Start/,/End/ -> [("L", "/Start/,/End/")]."""
    if not arg.startswith("-") or arg.startswith("--"):
        return []
    found = []
    for position, letter in enumerate(arg[1:], start=1):
        if letter in TAKES_VALUE[command]:  # the rest of the argument is its value
            return found + [(letter, arg[position + 1:])]
        found.append((letter, ""))
    return found


def _value(arg: str, command: str) -> str | None:
    """The value an option carries in the same argument, which may name a file: --opt=VALUE,
    or diff's -O<orderfile>, also bundled after other short options (-wO<file>)."""
    if arg.startswith("--") and "=" in arg:
        return arg.split("=", 1)[1]
    return next((value for letter, value in _short(arg, command) if letter == "O" and value), None)


def run(cwd: str | Path, args: list[str]) -> tuple[str, bool]:
    """Returns (text for the agent, is_error)."""
    if not isinstance(args, list) or not args or not all(isinstance(arg, str) for arg in args):
        return "Error: pass the arguments as a list of strings, starting with the command.", True
    if args[0] not in COMMANDS:
        return "Error: only diff, log, show, blame and status are available.", True
    if any("\x00" in arg for arg in args):
        return "Error: an argument contains a null byte.", True
    paths_only = False
    for arg in args[1:]:
        if arg == "--":
            paths_only = True
        elif not paths_only and (refused(arg) or (args[0] == "blame" and any(l == "S" for l, _ in _short(arg, "blame")))):
            return f"Error: `{arg}` isn't allowed: it could write a file, run a program or read outside the repo.", True
        elif (paths_only or not arg.startswith("-")) and outside(Path(cwd), arg):
            return f"Error: `{arg}` is outside the repo.", True
        elif not paths_only and (value := _value(arg, args[0])) and outside(Path(cwd), value):
            return f"Error: `{arg}` names a file outside the repo.", True
    command, rest = args[0], list(args[1:])
    safety = {"diff": ["--no-ext-diff", "--no-textconv"], "log": ["--no-ext-diff", "--no-textconv"],
              "show": ["--no-ext-diff", "--no-textconv"], "blame": ["--no-textconv"]}.get(command, [])
    env = {k: v for k, v in git_env(GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0").items()
           if not k.startswith("GIT_CONFIG") and k != "GIT_EXTERNAL_DIFF"}  # config from the environment, too
    argv = ["git", "--no-pager", "--no-optional-locks", "-c", "diff.external=", "-c", "core.fsmonitor=false",
            command, *safety, *rest]
    try:
        code, out, err, cut, timed_out = _bounded(argv, cwd, env)
    except (OSError, ValueError) as error:
        return f"Error: can't run git: {error}", True
    if timed_out:
        return f"Error: git {command} took longer than {TIMEOUT}s.", True
    if code == 0:
        text = out or f"(git {command} produced no output)"
    else:  # stdout too: `git diff --exit-code` exits 1 to say "there are differences"
        text = f"git {command} exited with code {code}:\n{err}{out}"
    if len(text) > LIMIT:
        text, cut = text[:LIMIT], True
    if cut:
        text += f"\n[output cut at {LIMIT} characters; narrow the command, e.g. with a path]"
    return text, code != 0


def _bounded(argv: list[str], cwd, env) -> tuple[int, str, str, bool, bool]:
    """Run git, reading at most about LIMIT characters of its output.
    Returns (exit code, stdout, stderr, output was cut, timed out)."""
    with tempfile.TemporaryFile() as stderr:  # a file, so a chatty stderr can't block the process
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=stderr)
        expired = threading.Event()

        def stop() -> None:
            if process.poll() is None:  # only a git still running timed out
                expired.set()
                process.kill()

        timer = threading.Timer(TIMEOUT, stop)
        timer.start()
        try:
            data = process.stdout.read(LIMIT * 4 + 1)  # bytes; enough for LIMIT characters
            cut_short, killed = len(data) > LIMIT * 4, False
            if cut_short:  # don't read the rest; if git can't finish without it, stop it
                try:
                    process.wait(timeout=0.5)  # what's left often fits in the pipe: then git exits by itself
                except subprocess.TimeoutExpired:
                    killed = True
                    process.kill()
            code = process.wait()
        finally:  # whatever happened, leave no git process or open pipe behind
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        timed_out = expired.is_set()
        stderr.seek(0)
        error_text = stderr.read(LIMIT).decode("utf-8", errors="replace")
    text = data.decode("utf-8", errors="replace")
    cut = cut_short or len(text) > LIMIT
    # Only a git we stopped early has no real exit code; a finished one keeps its own
    return (0 if killed else code), text[:LIMIT], error_text, cut, timed_out
