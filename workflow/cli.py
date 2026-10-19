"""CLI: the `wf` command.

    wf init <name>                   write a starter workflow, <name>.yaml
    wf examples [dir]                copy the complete example workflows into dir (wf-examples)
    wf validate <file>               check a file and every sub-workflow it uses
    wf graph <file>                  print the workflow as a Mermaid diagram
    wf run <file> [name=value ...]   validate, then run it (runner.py)
    wf resume <run-id>               continue a paused or failed run

For an operator that isn't at a terminal (operate.py):
    wf run <file> --detach --json    start in the background
    wf wait <run-id>                 block until the run needs attention
    wf answer <run-id> <file>        answer what it's waiting on, and continue
    wf status <run-id> / wf list     where runs stand (newest first)
    wf report <run-id>... | --workflow NAME   what runs did, per node (report.py)
    wf discard <run-id>...           delete runs: folders, worktrees, temp folders
    wf integrate firstmate <dir>     write the firstmate extension package (firstmate.py)

Every command that makes or finds runs takes --runs-dir DIR (default: WF_RUNS_DIR) to keep run
folders outside the repo, in a folder per repo under DIR, instead of in ./.wf/runs (runs.py).

main() parses the arguments and returns the exit code. The pyproject entry
point (`wf = "workflow.cli:main"`) passes that code to the shell.

Exit codes: 0 success, 1 failure (including bad arguments), 2 paused waiting
for a person (wf run and wf resume, only with --no-input or without a
terminal), 3 nothing new before the timeout (wf wait), 130 stopped with
Ctrl-C. Bad arguments exit with 1, not argparse's usual 2, so a script can
tell a pause from a typo.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from . import diagram, firstmate, operate, report, runner, runs, spec, validate, workspaces

STARTER = """\
version: 1
name: {name}
description: ""

defaults:
  harness: claude-code
  harnesses:
    claude-code: {{model: claude-opus-5-5, effort: high}}

start:
  input:
    task: {{type: string, description: What this workflow should do}}

# Add nodes here. Each node needs `needs: [...]` or a route that points to it.
nodes: {{}}

end:
  output: {{}}
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="wf", description="Run YAML workflows of coding-agent steps.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    init = commands.add_parser("init", help="write a starter workflow file")
    init.add_argument("name", help="the workflow's name: lowercase letters, digits and -")
    init.set_defaults(handler=_init)

    check = commands.add_parser("validate", help="check a workflow and its sub-workflows")
    check.add_argument("file", type=Path)
    check.set_defaults(handler=_validate)

    graph = commands.add_parser("graph", help="print a workflow as a Mermaid diagram")
    graph.add_argument("file", type=Path)
    graph.set_defaults(handler=_graph)

    no_input = {"action": "store_true", "help": "never ask questions: pause instead (exit code 2)"}
    runs_dir = {"type": Path, "metavar": "DIR", "help": "run folders are in DIR, a folder per repo, "
                                                        "instead of ./.wf/runs (default: WF_RUNS_DIR)"}
    run = commands.add_parser("run", help="validate, then run a workflow")
    limit_help = {"help": "a limit for this run instead of the file's: cost_usd=20, tokens=5000000, "
                          "duration=2h, or NAME=none for no limit (repeat for several)"}
    run.add_argument("file", type=Path)
    run.add_argument("inputs", nargs="*", metavar="name=value", help="inputs; a value that parses as JSON is used as JSON")
    run.add_argument("--input", type=Path, dest="input_file", metavar="FILE", help="a JSON file with inputs")
    run.add_argument("--no-input", **no_input)
    run.add_argument("--detach", action="store_true", help="start in the background and return at once")
    run.add_argument("--json", action="store_true", help="with --detach: print the run id as JSON")
    run.add_argument("--dry-run", type=Path, metavar="SCRIPT", help="run with scripted agent results, answers and command results instead of real agents")
    run.add_argument("--limit", action="append", dest="limits", metavar="NAME=VALUE", **limit_help)
    run.add_argument("--runs-dir", **runs_dir)
    run.set_defaults(handler=lambda a: runner.start(a.file, a.inputs, a.input_file, a.no_input, a.detach, a.json,
                                                    a.dry_run, a.limits, a.runs_dir))

    resume = commands.add_parser("resume", help="continue a paused or failed run")
    resume.add_argument("run_id", metavar="run-id")
    resume.add_argument("--no-input", **no_input)
    resume.add_argument("--answers", type=Path, metavar="FILE", help="answers for the questions it waits on")
    resume.add_argument("--detach", action="store_true", help="continue in the background")
    resume.add_argument("--json", action="store_true", help="with --detach: print the run id as JSON")
    resume.add_argument("--limit", action="append", dest="limits", metavar="NAME=VALUE", **limit_help)
    resume.add_argument("--runs-dir", **runs_dir)
    resume.set_defaults(handler=lambda a: runner.resume(a.run_id, a.no_input, a.answers, a.detach, a.json, a.limits,
                                                        a.runs_dir))

    answer = commands.add_parser("answer", help="answer the questions a run waits on, and continue it")
    answer.add_argument("run_id", metavar="run-id")
    answer.add_argument("answers", type=Path, metavar="answers.json", help="{question key: answers}, keys from wf status")
    answer.add_argument("--detach", action="store_true", help="continue in the background")
    answer.add_argument("--json", action="store_true")
    answer.add_argument("--runs-dir", **runs_dir)
    answer.set_defaults(handler=lambda a: runner.resume(a.run_id, True, a.answers, a.detach, a.json, runs_dir=a.runs_dir))

    wait = commands.add_parser("wait", help="block until a run needs attention; print its state as JSON")
    wait.add_argument("run_id", metavar="run-id")
    wait.add_argument("--timeout", type=float, default=3600, help="seconds (default 3600); exit code 3 if nothing happened")
    wait.add_argument("--consumer", metavar="NAME", help="report each state to NAME only once")
    wait.add_argument("--runs-dir", **runs_dir)
    wait.set_defaults(handler=_wait)

    status = commands.add_parser("status", help="where a run stands")
    status.add_argument("run_id", metavar="run-id")
    status.add_argument("--json", action="store_true")
    status.add_argument("--runs-dir", **runs_dir)
    status.set_defaults(handler=_status)

    examples = commands.add_parser("examples", help="copy wf's complete example workflows into a folder")
    examples.add_argument("dir", nargs="?", type=Path, default=Path("wf-examples"))
    examples.set_defaults(handler=_examples)

    listing = commands.add_parser("list", help="every run in this folder, newest first")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--runs-dir", **runs_dir)
    listing.set_defaults(handler=_list)

    reporting = commands.add_parser("report", help="what runs did, per node: evidence for improving a workflow")
    reporting.add_argument("run_ids", nargs="*", metavar="run-id")
    reporting.add_argument("--workflow", metavar="NAME", help="every run of this workflow in this folder")
    kind = reporting.add_mutually_exclusive_group()
    kind.add_argument("--real", action="store_true", help="leave dry runs out")
    kind.add_argument("--dry-runs", action="store_true", help="only dry runs")
    reporting.add_argument("--version", metavar="V", help="only runs of this version of the workflow files (see runs[].version)")
    reporting.add_argument("--json", action="store_true")
    reporting.add_argument("--runs-dir", **runs_dir)
    reporting.set_defaults(handler=_report)

    discarding = commands.add_parser("discard", help="delete runs: their folders, worktrees and temp folders")
    discarding.add_argument("run_ids", nargs="*", metavar="run-id")
    discarding.add_argument("--workflow", metavar="NAME", help="every run of this workflow in this folder")
    discarding.add_argument("--dry-runs", action="store_true", help="only dry runs (with --workflow)")
    discarding.add_argument("--branches", action="store_true",
                            help="delete their branches too, wf/<run-id>/ and any `branch` named (a dry run's always go)")
    discarding.add_argument("--runs-dir", **runs_dir)
    discarding.set_defaults(handler=_discard)

    integrate = commands.add_parser("integrate", help="connect wf to another tool")
    integrate.add_argument("tool", choices=["firstmate"])
    integrate.add_argument("dir", type=Path, help="a new folder for the package, outside any git repo")
    integrate.set_defaults(handler=_integrate)

    # parse_known_args: argparse can't take name=value pairs after an option
    # (`wf run f.yaml --input x.json n=1`), so leftovers that look like inputs join them
    args, extra = parser.parse_known_args(argv)
    if extra and (args.command != "run" or any(e.startswith("-") or "=" not in e for e in extra)):
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if extra:
        args.inputs += extra
    try:
        return args.handler(args)
    except KeyboardInterrupt:  # anywhere the runner doesn't handle it itself
        print("\nStopped.", file=sys.stderr)
        return runner.STOPPED


def _init(args) -> int:
    if not re.fullmatch(r"[a-z0-9-]+", args.name):
        print(f"wf init: `{args.name}` isn't a valid name: use lowercase letters, digits and -", file=sys.stderr)
        return 1
    path = Path(f"{args.name}.yaml")
    try:
        with path.open("x") as file:  # "x" fails if the file exists
            file.write(STARTER.format(name=args.name))
    except FileExistsError:
        print(f"wf init: {path} already exists", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"wf init: can't write {path}: {error.strerror or error}", file=sys.stderr)
        return 1
    print(f"Created {path}")
    return 0


EXAMPLES = Path(__file__).parent / "examples"


def _examples(args) -> int:
    try:
        args.dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(EXAMPLES.glob("*.yaml")):
            target = args.dir / source.name
            if target.exists():
                print(f"{target} already exists: left as it is")
                continue
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Wrote {target}")
    except OSError as error:
        print(f"wf examples: can't write {args.dir}: {error.strerror or error}", file=sys.stderr)
        return 1
    return 0


def _validate(args) -> int:
    result = validate.check(args.file)
    for problem in result.problems:
        print(problem)
    print(f"{args.file.name}: {summary(result)}")
    return 1 if result.errors else 0


def summary(result: validate.Report) -> str:
    """"valid", "valid, 1 warning", "2 errors, 1 warning", ..."""
    errors, warnings = len(result.errors), len(result.warnings)
    parts = [_count(errors, "error")] if errors else ["valid"]
    return ", ".join(parts + ([_count(warnings, "warning")] if warnings else []))


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _find(run_id: str, runs_dir: Path | None) -> runs.Run | None:
    try:
        return runs.find(Path.cwd(), run_id, runs_dir)
    except FileNotFoundError as error:
        print(f"wf: {error}", file=sys.stderr)
        return None


def _wait(args) -> int:
    run = _find(args.run_id, args.runs_dir)
    if run is None:
        return 1
    try:
        found = operate.wait(run, args.timeout, args.consumer)
    except ValueError as error:  # a consumer name that isn't path-safe
        print(f"wf wait: --consumer must be a path-safe name: {error}", file=sys.stderr)
        return 1
    if found is None:
        print(json.dumps({"run": run.id, "status": "unchanged"}))
        return 3
    print(json.dumps(found, indent=2, ensure_ascii=False, default=str))
    return 0


def _status(args) -> int:
    run = _find(args.run_id, args.runs_dir)
    if run is None:
        return 1
    current = operate.state(run)
    print(json.dumps(current, indent=2, ensure_ascii=False, default=str) if args.json else _describe(current))
    return 0


def _list(args) -> int:
    states = [operate.state(run) for run in reversed(runs.all_runs(Path.cwd(), args.runs_dir))]
    if args.json:
        print(json.dumps(states, indent=2, ensure_ascii=False, default=str))
    else:
        for current in states:
            print(_describe(current))
    return 0


def _describe(current: dict) -> str:
    status, run_id = current.get("status"), current.get("run")
    if status == "waiting":
        detail = f"waiting for a person: {', '.join(current.get('nodes', []))} (wf answer {run_id} <answers.json>)"
    elif status == "failed":
        detail = f"failed: {current.get('node', '?')}: {current.get('error', '')} (wf resume {run_id})"
    elif status == "stopped":
        detail = f"stopped: {current.get('reason')} (wf resume {run_id})"
    elif status == "limit":
        detail = f"paused at a limit: {current.get('reason')} ({current.get('resume')})"
    elif status == "running" and current.get("agents_waiting"):
        detail = "running, waiting for an agent slot: " + ", ".join(w["node"] for w in current["agents_waiting"])
    else:
        detail = status
    return f"{run_id} [{current.get('workflow')}]: {detail}"


def _chosen(args, dry_runs: bool | None) -> list[runs.Run] | None:
    """The runs named, plus every run of --workflow, keeping only dry runs (True) or real
    ones (False) if asked. None if a named run doesn't exist."""
    chosen = []
    for run_id in dict.fromkeys(args.run_ids):  # once each, in order
        run = _find(run_id, args.runs_dir)
        if run is None:
            return None
        chosen.append(run)
    if args.workflow:
        chosen += [r for r in runs.all_runs(Path.cwd(), args.runs_dir)
                   if r.info().get("workflow") == args.workflow and r not in chosen]
    if dry_runs is not None:
        chosen = [r for r in chosen if bool(r.info().get("dry_run")) == dry_runs]
    return chosen


def _which(args, dry_runs: bool | None) -> str:
    """The runs asked for, in words: "dry runs of loop", "runs of version 1a2b3c4d", ..."""
    kind = {True: "dry runs", False: "real runs", None: "runs"}[dry_runs]
    detail = [f"of {args.workflow}"] if args.workflow else []
    detail += [f"of version {args.version}"] if getattr(args, "version", None) else []
    return " ".join([kind, *detail]) if detail else f"{kind} among those named"


def _report(args) -> int:
    dry_runs = True if args.dry_runs else False if args.real else None
    chosen = _chosen(args, dry_runs)
    if chosen is None:
        return 1
    if args.version:
        chosen = [r for r in chosen if report.version(r) == args.version]
    if not args.run_ids and not args.workflow:
        print("wf report: name runs, or --workflow <name> (wf list shows them)", file=sys.stderr)
        return 1
    if not chosen:
        print(f"wf report: no {_which(args, dry_runs)}", file=sys.stderr)
        return 1
    found = report.build(chosen)
    print(json.dumps(found, indent=2, ensure_ascii=False, default=str) if args.json else report.text(found))
    return 0


def _discard(args) -> int:
    if not args.run_ids and not args.workflow:
        print("wf discard: name runs, or --workflow <name> (wf list shows them)", file=sys.stderr)
        return 1
    found = _chosen(args, True if args.dry_runs else None)
    if found is None:
        return 1
    if not found:
        print(f"wf discard: no {_which(args, True if args.dry_runs else None)}")
        return 0
    for run in found:
        dry = bool(run.info().get("dry_run"))
        try:
            kept = runs.discard(run, branches=args.branches or dry)
        except (runs.RunBusy, workspaces.WorkspaceError) as error:
            print(f"wf discard: {error}", file=sys.stderr)
            return 1
        except OSError as error:
            print(f"wf discard: can't delete run {run.id}: {error.strerror or error}", file=sys.stderr)
            return 1
        note = f"; kept its branches {', '.join(kept)} (--branches deletes them)" if kept else ""
        print(f"Discarded run {run.id}{note}")
    return 0


def _integrate(args) -> int:
    dest = args.dir.expanduser().absolute()  # one path for writing and for the instructions, `~` too
    try:
        manifest = firstmate.write_package(dest)
    except firstmate.PackageError as error:
        print(f"wf integrate: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"wf integrate: can't write {dest}: {error.strerror or error}", file=sys.stderr)
        return 1
    print(firstmate.instructions(dest, manifest))
    return 0


def _graph(args) -> int:
    try:
        workflow = spec.load(args.file)
    except spec.SpecError as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 1
    sys.stdout.write(diagram.mermaid(workflow))
    return 0


if __name__ == "__main__":  # `python -m workflow.cli`, as detached runs start it
    sys.exit(main())
