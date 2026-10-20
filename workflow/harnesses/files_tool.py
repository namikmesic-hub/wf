"""Files tool: reading and searching files, kept to the folders a step may read.

A read-only agent on Cursor's SDK gets these tools in place of Cursor's own read, grep, glob and
ls. Cursor's tools take any path, and a search from the home folder walks every other app's
private data, so macOS asks the person at the Mac about each one. The SDK can't refuse a call
before it runs, so wf runs the reads itself, like the read-only `git` tool (git_tool.py):
- read_file: a file's lines, numbered, from `offset`, at most `limit` of them.
- search_files: the lines that match a regular expression, in a file or in the files under a folder.
- find_files: the files under a folder whose path matches a glob pattern.
- list_dir: a folder's entries.
Their names differ from Cursor's own tools (read, grep, glob, ls), so a call of one of those is
never taken for one of these.

A path is relative to the workspace, or absolute. Either way it must lead into one of the
`roots`: the workspace, and the folders of wf's that the step's instruction names
(SessionSpec.read_roots). A path that leads elsewhere, also through a symlink, is refused, and a
search follows no symlink out of them. A search lists the files the way git does (tracked and
untracked, without what .gitignore leaves out), or walks the folder when git lists none there,
skipping .git. Every answer stops at LIMIT characters, and a search at MAX_FILES files.
Only regular files are read: a named pipe or a device is refused, and opened without waiting.

An agent's regular expression can take time that grows without bound, and Python can't stop a
thread that runs one. So search_files runs in a Python process of its own, stopped after
SEARCH_SECONDS.

A glob pattern's `*` and `?` stay within one folder, and `**` crosses folders. A pattern without
a `/` matches a file's name at any depth.

run() is plain Python. Each adapter wraps it in its own tool type, like submit_result.
"""

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from ..workspaces import git_env

LIMIT = 50_000  # characters in one answer
MAX_FILES = 50_000  # files one search looks at
MAX_BYTES = 2_000_000  # a bigger file isn't searched
MAX_READ = 20_000_000  # nor read
LINES = 2_000  # lines read_file returns when the call gives no limit
SEARCH_SECONDS = 30
WHERE = "a path relative to the workspace, or an absolute path inside it or inside a folder your instruction names"

TOOLS = {
    "read_file": ("Read a text file. Lines come numbered. Give `offset` (the first line, from 1) and `limit` "
                  f"(how many lines, {LINES} by default) to read part of a long file. The path is {WHERE}.",
                  {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
                   "limit": {"type": "integer", "minimum": 1}}, ["path"]),
    "search_files": ("Search files for a regular expression (Python syntax). Gives path:line:text for each match, "
             "or only the paths with `files_only`. `path` is a file or a folder to search under "
             f"(the workspace by default), and `glob` keeps only the files whose path matches it. The path is {WHERE}.",
             {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"},
              "case_insensitive": {"type": "boolean"}, "files_only": {"type": "boolean"}}, ["pattern"]),
    "find_files": ("List the files under a folder (the workspace by default) whose path relative to it matches "
             f"a glob pattern, such as **/*.py or src/*.ts, where * stays within one folder and ** crosses "
             f"folders. A pattern without / matches file names at any depth. The folder is {WHERE}.",
             {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
    "list_dir": (f"List a folder's entries (the workspace by default). Folders end with /. The folder is {WHERE}.",
                 {"path": {"type": "string"}}, []),
}
NAMES = tuple(TOOLS)


def schema(name: str) -> dict:
    _, properties, required = TOOLS[name]
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def description(name: str) -> str:
    return TOOLS[name][0]


class _Refused(Exception):
    pass


def run(name: str, cwd: Path, roots: list[Path], args: object) -> tuple[str, bool]:
    """Returns (text for the agent, is_error). `roots` are the folders besides `cwd` it may read."""
    if not isinstance(args, dict):
        return "Error: pass the arguments as an object.", True
    allowed = _unique([Path(cwd).resolve(), *(Path(root).resolve() for root in roots)])
    try:
        if name == "read_file":
            text = _read(Path(cwd), allowed, args)
        elif name == "search_files":
            text = _search_apart(Path(cwd), allowed, args)
        elif name == "find_files":
            text = _find(Path(cwd), allowed, args)
        elif name == "list_dir":
            text = _list(Path(cwd), allowed, args)
        else:
            return f"Error: there's no file tool called `{name}`.", True
    except _Refused as refusal:
        return f"Error: {refusal}", True
    return _cut(text), False


def _target(cwd: Path, allowed: list[Path], given: object, default: str | None = None) -> Path:
    """The path a call names, resolved, if it leads into one of the allowed folders."""
    if given is None and default is not None:
        given = default
    if not isinstance(given, str) or not given.strip() or "\x00" in given:
        raise _Refused("give the path as a string")
    path = Path(os.path.expanduser(given))
    resolved = (path if path.is_absolute() else cwd / path).resolve()
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise _Refused(f"`{given}` is outside the workspace and the folders your instruction names, "
                       "so it can't be read here")
    if not resolved.exists():
        raise _Refused(f"`{given}` doesn't exist")
    return resolved


def _shown(cwd: Path, path: Path) -> str:
    """A path as the answer names it: relative to the workspace, or absolute outside it."""
    try:
        return str(path.relative_to(cwd.resolve()))
    except ValueError:
        return str(path)


def _search_apart(cwd: Path, allowed: list[Path], args: dict) -> str:
    """search_files, in a Python process of its own that stops after SEARCH_SECONDS."""
    request = json.dumps({"cwd": str(cwd), "roots": [str(root) for root in allowed], "args": args})
    try:
        done = subprocess.run([sys.executable, "-I", "-m", __name__], input=request, capture_output=True, text=True,
                              timeout=SEARCH_SECONDS)
    except subprocess.TimeoutExpired:
        raise _Refused(f"the search took longer than {SEARCH_SECONDS} seconds, so it stopped: "
                       "make the pattern simpler, or search a smaller folder") from None
    except OSError as error:
        raise _Refused(f"the search didn't start: {error}") from None
    try:
        text, refused = json.loads(done.stdout)
    except ValueError:
        raise _Refused(f"the search failed: {done.stderr.strip()[-500:] or 'no answer'}") from None
    if refused:
        raise _Refused(text)
    return text


def _read(cwd: Path, allowed: list[Path], args: dict) -> str:
    path = _target(cwd, allowed, args.get("path"))
    if path.is_dir():
        raise _Refused(f"`{args.get('path')}` is a folder: use list_dir")
    data = _bytes(path, MAX_READ)
    if b"\x00" in data[:8192]:
        raise _Refused(f"`{args.get('path')}` is a binary file")
    lines = data.decode("utf-8", errors="replace").splitlines()
    offset = max(int(args.get("offset") or 1), 1)
    limit = max(int(args.get("limit") or LINES), 1)
    chosen = lines[offset - 1:offset - 1 + limit]
    if not chosen:
        return f"(`{args.get('path')}` has {len(lines)} lines, so none from line {offset})"
    numbered = "\n".join(f"{number:6}\t{line}" for number, line in enumerate(chosen, start=offset))
    rest = len(lines) - (offset - 1 + len(chosen))
    return numbered + (f"\n[{rest} more lines: read on with offset {offset + len(chosen)}]" if rest > 0 else "")


def _search(cwd: Path, allowed: list[Path], args: dict) -> str:
    target = _target(cwd, allowed, args.get("path"), ".")
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise _Refused("give the pattern as a string")
    try:
        regex = re.compile(pattern, re.IGNORECASE if args.get("case_insensitive") else 0)
    except re.error as error:
        raise _Refused(f"the pattern isn't a valid regular expression: {error}") from None
    only = args.get("glob") if isinstance(args.get("glob"), str) and args.get("glob") else None
    found, size, files_only = [], 0, bool(args.get("files_only"))
    files = [target] if target.is_file() else _files(target, allowed)
    for count, path in enumerate(files, start=1):
        if count > MAX_FILES:
            found.append(f"[stopped after {MAX_FILES} files: search a smaller folder]")
            break
        if only and not _matches(_shown(target, path) if target.is_dir() else path.name, only):
            continue
        try:
            data = _bytes(path, MAX_BYTES)
        except _Refused:
            continue
        if b"\x00" in data[:8192]:
            continue
        shown = _shown(cwd, path)
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            if regex.search(line):
                entry = shown if files_only else f"{shown}:{number}:{line[:500]}"
                found.append(entry)
                size += len(entry) + 1
                if files_only:
                    break
        if size > LIMIT:
            break
    return "\n".join(found) if found else "(no matches)"


def _find(cwd: Path, allowed: list[Path], args: dict) -> str:
    folder = _target(cwd, allowed, args.get("path"), ".")
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise _Refused("give the pattern as a string")
    if not folder.is_dir():
        raise _Refused(f"`{args.get('path')}` isn't a folder")
    found = []
    for count, path in enumerate(_files(folder, allowed), start=1):
        if count > MAX_FILES:
            found.append(f"[stopped after {MAX_FILES} files: list a smaller folder]")
            break
        if _matches(_shown(folder, path), pattern):
            found.append(_shown(cwd, path))
    return "\n".join(sorted(found)) if found else "(no files match)"


def _list(cwd: Path, allowed: list[Path], args: dict) -> str:
    folder = _target(cwd, allowed, args.get("path"), ".")
    if not folder.is_dir():
        raise _Refused(f"`{args.get('path')}` isn't a folder: use read_file")
    entries = sorted(f"{entry.name}/" if entry.is_dir() else entry.name for entry in folder.iterdir())
    return "\n".join(entries) if entries else "(the folder is empty)"


def _matches(path: str, pattern: str) -> bool:
    """Does a relative path match a glob? A pattern without `/` is matched against the file's name."""
    return _glob(pattern).match(path if "/" in pattern else path.rsplit("/", 1)[-1]) is not None


def _glob(pattern: str) -> re.Pattern:
    """A glob as a regular expression: `**/` is any folders or none, `**` anything, `*` and `?`
    anything within one folder, and `[...]` one of its characters (`[!...]` none of them)."""
    out, at = [], 0
    while at < len(pattern):
        if pattern.startswith("**/", at):
            out.append("(?:.*/)?")
            at += 3
        elif pattern.startswith("**", at):
            out.append(".*")
            at += 2
        elif pattern[at] in "*?":
            out.append("[^/]*" if pattern[at] == "*" else "[^/]")
            at += 1
        elif pattern[at] == "[" and (end := pattern.find("]", at + 2)) != -1:
            inside = pattern[at + 1:end].replace("\\", "\\\\")
            out.append("[" + ("^" + inside[1:] if inside.startswith("!") else inside) + "]")
            at = end + 1
        else:
            out.append(re.escape(pattern[at]))
            at += 1
    return re.compile("".join(out) + r"\Z")


def _files(folder: Path, allowed: list[Path]) -> Iterator[Path]:
    """The files under a folder, as git lists them there, else by walking it. Each one is
    resolved, and one that leads out of the allowed folders (a symlink) is left out."""
    listed = _git_files(folder)
    paths = (folder / name for name in listed) if listed else _walk(folder)
    for path in paths:
        resolved = path.resolve()
        if resolved.is_file() and any(resolved.is_relative_to(root) for root in allowed):
            yield resolved


def _git_files(folder: Path) -> list[str]:
    env = {k: v for k, v in git_env(GIT_TERMINAL_PROMPT="0").items() if not k.startswith("GIT_CONFIG")}
    try:
        done = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached",
                               "--others", "--exclude-standard"], cwd=folder, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 0:
        return []
    return sorted({name for name in done.stdout.decode("utf-8", errors="replace").split("\0") if name})


def _walk(folder: Path) -> Iterator[Path]:
    for top, folders, names in os.walk(folder):  # follows no symlinked folder
        folders[:] = sorted(f for f in folders if f != ".git")
        for name in sorted(names):
            yield Path(top) / name


def _bytes(path: Path, limit: int) -> bytes:
    """A regular file's bytes. It's opened without waiting, so a named pipe can't hold the read."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise _Refused(f"{path.name} isn't a regular file")
            if info.st_size > limit:
                raise _Refused(f"{path.name} is bigger than {limit} bytes")
            return file.read()
    except OSError as error:
        raise _Refused(f"can't read {path.name}: {error.strerror or error}") from None


def _cut(text: str) -> str:
    return text if len(text) <= LIMIT else text[:LIMIT] + f"\n[answer cut at {LIMIT} characters: narrow the call]"


def _unique(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(paths))


if __name__ == "__main__":  # _search_apart's process: the request on stdin, [text, refused] on stdout
    asked = json.loads(sys.stdin.read())
    try:
        found = _search(Path(asked["cwd"]), [Path(root) for root in asked["roots"]], asked["args"]), False
    except _Refused as refusal:
        found = str(refusal), True
    sys.stdout.write(json.dumps(found))
