"""CLI: the `wf` command.

    wf init <name>                   write a starter workflow, <name>.yaml
    wf examples [dir]                copy the complete example workflows into dir (wf-examples)
    wf validate <file>               check a file and every sub-workflow it uses
    wf graph <file>                  print the workflow as a Mermaid diagram
    wf run <file> [name=value ...]   validate, then run it (runner.py)
    wf retry <run-id>                continue a failed or paused run from its last checkpoint (--model
                                     NODE=MODEL or NODE[INDEX]=MODEL: another model, overrides.py)
    wf continue <run-id> <request>   give a run that succeeded a follow-up request (runner.py)

Tools that agent nodes can use (`tools: [slack, ...]`; workflow/tools/):
    wf tools list                    each tool, how it signs in, and whether it's set up
    wf tools <name> setup            sign in once; the secret goes to the macOS Keychain
    wf tools <name> doctor           check that a tool works; `wf tools doctor` checks them all

Runtimes that need setting up (workflow/harnesses/cursor.py):
    wf runtimes cursor signin        keep a Cursor API key in the macOS Keychain, for Cursor's SDK
    wf runtimes cursor doctor        check Node, Cursor's SDK, the key, and how model ids map (--model ID)

For an operator that isn't at a terminal (operate.py):
    wf run <file> --detach --json    start in the background
    wf wait <run-id>                 block until the run needs attention
    wf answer <run-id> <file>        answer what it's waiting on, and continue
    wf status <run-id> / wf list     where runs stand (newest first), and in status, how far, as an estimate (progress.py)
    wf steer <run-id> <node> <message>   send a message into a node's live agent session (steer.py)
    wf report <run-id>... | --workflow NAME   what runs did, per node (report.py)
    wf trace <run-id> [--node N] [--pass P] [--field F] [--var PATH]
                                     what one run read, decided and did, from its trace (trace_view.py)
    wf discard <run-id>...           delete runs: folders, worktrees, temp folders, saved sessions
    wf integrate firstmate <dir>     write the firstmate extension package (firstmate.py)

To see runs as they happen (observe.py, ui.py):
    wf sessions [run-id]             each agent session that runs now
    wf watch <run-id>                the run's steps and tool calls, as they come
    wf ui                            a web page on this machine: runs, graph, progress, metrics, transcripts

wf's own settings, in ~/.wf/config.toml (config/):
    wf config show                   the value of each key, and where it comes from

Every command that makes or finds runs takes --runs-dir DIR, to keep run folders outside the repo.
They then go in a folder per repo under DIR, instead of in ./.wf/runs (runs.py). WF_RUNS_DIR, else
runs_dir in config.toml, gives the default DIR.

main() parses the arguments, loads the configuration, and returns the exit code. A configuration
with problems stops every command before it starts. The pyproject entry point
(`wf = "workflow.cli:main"`) passes that code to the shell.

Exit codes: 0 means success, and 1 means failure, bad arguments included. A
pause for a person gives 2, from wf run, wf retry and wf continue, only with
--no-input or without a terminal. Nothing new before the timeout gives 3 (wf
wait), and Ctrl-C gives 130. Bad arguments exit with 1, not argparse's usual
2, so a script can tell a pause from a typo. A message that `wf steer`
doesn't send fails too: exit code 1.
"""

import argparse
import asyncio
import getpass
import json
import re
import sys
import webbrowser
from pathlib import Path

from . import config, diagram, firstmate, observe, operate, progress, report, runner, runs, spec, steer, trace_view, ui, validate, workspaces
from .tools import keychain, signin
from .tools.catalog import TOOLS

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
    runs_dir = {"type": config.flag("runs_dir"), "metavar": "DIR",
                "help": "run folders are in DIR, a folder per repo, instead of ./.wf/runs "
                        "(default: WF_RUNS_DIR, else runs_dir in ~/.wf/config.toml)"}
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

    retry = commands.add_parser("retry", help="continue a failed or paused run from its last checkpoint")
    retry.add_argument("run_id", metavar="run-id")
    retry.add_argument("--no-input", **no_input)
    retry.add_argument("--answers", type=Path, metavar="FILE", help="answers for the questions it waits on")
    retry.add_argument("--detach", action="store_true", help="continue in the background")
    retry.add_argument("--json", action="store_true", help="with --detach: print the run id as JSON")
    retry.add_argument("--limit", action="append", dest="limits", metavar="NAME=VALUE", **limit_help)
    retry.add_argument("--model", action="append", dest="models", metavar="NODE=MODEL",
                       help="another model for an agent node, or for one item with NODE[INDEX]=MODEL, for the rest "
                            "of the run (repeat for several)")
    retry.add_argument("--harness", action="append", dest="harnesses", metavar="NODE=HARNESS",
                       help="the harness for that node or item: needed when the node names a list or a template")
    retry.add_argument("--effort", action="append", dest="efforts", metavar="NODE=EFFORT",
                       help="the effort for that node or item (not for cursor, whose model id carries it)")
    retry.add_argument("--runs-dir", **runs_dir)
    retry.set_defaults(handler=lambda a: runner.retry(
        a.run_id, a.no_input, a.answers, a.detach, a.json, a.limits, a.runs_dir,
        {"model": a.models or [], "harness": a.harnesses or [], "effort": a.efforts or []}))

    answer = commands.add_parser("answer", help="answer the questions a run waits on, and continue it")
    answer.add_argument("run_id", metavar="run-id")
    answer.add_argument("answers", type=Path, metavar="answers.json", help="{question key: answers}, keys from wf status")
    answer.add_argument("--detach", action="store_true", help="continue in the background")
    answer.add_argument("--json", action="store_true")
    answer.add_argument("--runs-dir", **runs_dir)
    answer.set_defaults(handler=lambda a: runner.retry(a.run_id, True, a.answers, a.detach, a.json, runs_dir=a.runs_dir))
    _continue_command(commands, no_input, runs_dir)
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

    sessions = commands.add_parser("sessions", help="each agent session that runs now, in this folder's runs or in one run")
    sessions.add_argument("run_id", nargs="?", metavar="run-id")
    sessions.add_argument("--json", action="store_true")
    sessions.add_argument("--runs-dir", **runs_dir)
    sessions.set_defaults(handler=_sessions)

    watching = commands.add_parser("watch", help="print a run's steps and tool calls as they come, until it succeeds")
    watching.add_argument("run_id", metavar="run-id")
    watching.add_argument("--from-start", action="store_true", help=f"print every event so far first, not the last {observe.WATCH_BACK}")
    watching.add_argument("--runs-dir", **runs_dir)
    watching.set_defaults(handler=_watch)

    page = commands.add_parser("ui", help="a web page on this machine that shows runs: graph, progress, metrics, transcripts")
    page.add_argument("--port", type=config.flag("ui.port"),
                      help="the port on 127.0.0.1, or 0 for any free one (default: ui.port in ~/.wf/config.toml, "
                           f"else {config.KEYS['ui.port'].default})")
    page.add_argument("--all-repos", action="store_true", help="the runs of every repo in the runs folder, not only this repo's")
    page.add_argument("--open", action="store_true", help="open the page in the browser")
    page.add_argument("--runs-dir", **runs_dir)
    page.set_defaults(handler=_ui)

    configuration = commands.add_parser("config", help="wf's own settings, in ~/.wf/config.toml (WF_HOME moves ~/.wf)")
    showing = configuration.add_subparsers(dest="config_command", required=True, parser_class=_Parser).add_parser(
        "show", help="the value of each key, and where it comes from: a flag, the environment, the file or the default")
    showing.add_argument("--runs-dir", **runs_dir)
    showing.set_defaults(handler=_config_show)

    steering = commands.add_parser(
        "steer", help="send a message into the live agent session of a running node",
        description="Send a message into the live agent session of a running node. wf steer trusts your user "
                    "account, as every wf command does: any process that runs as you can steer a run. That includes "
                    "an agent that can run commands as you: on Claude Code and Cursor with edit or full access, and "
                    "on Codex with full access.")
    steering.add_argument("run_id", metavar="run-id")
    steering.add_argument("node", help="an agent node, as events name it: implement, or build[1]/implement in a sub-workflow")
    steering.add_argument("message", nargs="?", help="what to tell the agent (or use --file)")
    steering.add_argument("--item", type=int, metavar="INDEX", help="the item, for a for_each node")
    steering.add_argument("--file", type=Path, metavar="FILE", help="read the message from FILE (- for stdin)")
    steering.add_argument("--json", action="store_true")
    steering.add_argument("--runs-dir", **runs_dir)
    steering.set_defaults(handler=_steer)

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

    tracing = commands.add_parser("trace", help="what one run read, decided and did: renders, routes, attempts, tool calls")
    tracing.add_argument("run_id", metavar="run-id")
    tracing.add_argument("--node", metavar="N", help="only this node: its id, or its path like build[1]/implement")
    tracing.add_argument("--pass", type=int, dest="pass_no", metavar="P", help="only this pass of the node")
    tracing.add_argument("--field", metavar="F", help="only renders of this field (instruction, command, env, routes, ...)")
    tracing.add_argument("--var", metavar="PATH", help="the value of a variable path in each render that read it, e.g. review.blocking_issues")
    tracing.add_argument("--json", action="store_true")
    tracing.add_argument("--runs-dir", **runs_dir)
    tracing.set_defaults(handler=_trace)

    discarding = commands.add_parser("discard", help="delete runs: their folders, worktrees, temp folders and saved sessions")
    discarding.add_argument("run_ids", nargs="*", metavar="run-id")
    discarding.add_argument("--workflow", metavar="NAME", help="every run of this workflow in this folder")
    discarding.add_argument("--dry-runs", action="store_true", help="only dry runs (with --workflow)")
    discarding.add_argument("--branches", action="store_true",
                            help="delete their branches too: wf/<run-id>/, and the ones `branch` named while they're still "
                                 "the run's work (a dry run's always go)")
    discarding.add_argument("--runs-dir", **runs_dir)
    discarding.set_defaults(handler=_discard)

    tools = commands.add_parser("tools", help="set up and check the tools agents can use: Slack, Linear, ...")
    tool_commands = tools.add_subparsers(dest="tools_command", required=True, parser_class=_Parser)
    tool_commands.add_parser("list", help="each tool, how it signs in, and whether it's set up").set_defaults(handler=_tools_list)
    tool_commands.add_parser("doctor", help="check every tool").set_defaults(handler=_tools_doctor, tool=None)
    for tool in TOOLS.values():
        actions = tool_commands.add_parser(tool.name, help=f"{tool.title}, through {tool.url}").add_subparsers(
            dest="action", required=True, parser_class=_Parser)
        setup = actions.add_parser("setup", help=f"sign in to {tool.title}; the secret goes to the macOS Keychain")
        setup.add_argument("--port", type=config.flag("tools.signin_port"),
                           help="where to wait for the browser's redirect (default: tools.signin_port in "
                                f"~/.wf/config.toml, else {config.KEYS['tools.signin_port'].default})")
        if tool.token:
            setup.add_argument("--token", action="store_true", help=f"paste a {tool.token} instead")
        if tool.write_scopes:
            setup.add_argument("--write", action="store_true", help="ask for the write scopes too, for nodes with `write: true`")
        setup.set_defaults(handler=_tools_setup, tool=tool.name, token=False, write=False)
        actions.add_parser("doctor", help=f"check that {tool.title} works").set_defaults(handler=_tools_doctor, tool=tool.name)

    runtimes = commands.add_parser("runtimes", help="set up and check the agent runtimes that need it: Cursor's API key")
    runtime_commands = runtimes.add_subparsers(dest="runtime", required=True, parser_class=_Parser)
    cursor_actions = runtime_commands.add_parser("cursor", help="Cursor, through its SDK").add_subparsers(
        dest="action", required=True, parser_class=_Parser)
    cursor_actions.add_parser("signin", help="keep a Cursor API key in the macOS Keychain, and check it").set_defaults(
        handler=_cursor_signin)
    cursor_doctor = cursor_actions.add_parser("doctor", help="check Node, Cursor's SDK, the API key, and how model ids map")
    cursor_doctor.add_argument("--model", action="append", default=[], metavar="ID",
                               help="a Cursor model id, as a workflow names it, to see what it maps to (repeatable)")
    cursor_doctor.set_defaults(handler=_cursor_doctor)

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
        loaded = config.load()
    except config.ConfigError as error:
        for problem in error.problems:
            print(f"wf {args.command}: {problem}", file=sys.stderr)
        return 1
    try:
        with config.pinned(loaded), workspaces.stopping_git_on_exit():
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
    for note in result.notes:
        print(f"{args.file.name}: note: {note}")
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
    current["progress"] = progress.of(run, current.get("status"))
    if args.json:
        print(json.dumps(current, indent=2, ensure_ascii=False, default=str))
    else:
        how_far = current["progress"] and current.get("status") != "succeeded"
        print(_describe(current) + (f"\n  {progress.describe(current['progress'])}" if how_far else ""))
    return 0


def _sessions(args) -> int:
    if args.run_id:
        run = _find(args.run_id, args.runs_dir)
        if run is None:
            return 1
        chosen = [run]
    else:
        chosen = runs.all_runs(Path.cwd(), args.runs_dir)
    found = [session for run in chosen for session in observe.sessions(run)]
    print(json.dumps(found, indent=2, ensure_ascii=False, default=str) if args.json else observe.table(found))
    return 0


def _watch(args) -> int:
    run = _find(args.run_id, args.runs_dir)
    if run is None:
        return 1
    try:
        current = observe.watch(run, back=None if args.from_start else observe.WATCH_BACK, tell=_describe)
    except KeyboardInterrupt:  # the usual way to stop watching
        return 0
    print(_describe(current))
    return 0


def _ui(args) -> int:
    chosen = config.get({"runs_dir": args.runs_dir, "ui.port": args.port})
    if args.all_repos and chosen.runs_dir is None:
        print(f"wf ui: --all-repos needs a runs folder: --runs-dir DIR, {config.KEYS['runs_dir'].variable}, "
              f"or runs_dir in {chosen.file}", file=sys.stderr)
        return 1
    try:
        return ui.serve(ui.Source(Path.cwd(), args.runs_dir, args.all_repos), chosen.ui.port,
                        opened=webbrowser.open if args.open else None)
    except OSError as error:
        print(f"wf ui: can't listen on {ui.HOST}:{chosen.ui.port}: {error.strerror or error}", file=sys.stderr)
        return 1


def _config_show(args) -> int:
    print("\n".join(config.show.lines(config.get({"runs_dir": args.runs_dir}))))
    return 0


def _steer(args) -> int:
    try:
        text = _message(args)
    except ValueError as error:
        print(f"wf steer: {error}", file=sys.stderr)
        return 1
    run = _find(args.run_id, args.runs_dir)
    if run is None:
        return 1
    try:
        path = steer.target(run, args.node, args.item)
        answer = steer.send(run, path, text)
    except steer.SteerError as error:
        print(f"wf steer: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"wf steer: can't use the mailbox {steer.mailbox(run)}: {error.strerror or error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"run": run.id, "node": path, **answer}))
    elif answer["status"] == "none":
        print(f"wf steer: nothing was sent to {path}: {answer['reason']}.", file=sys.stderr)
    else:
        where = f"{path} (attempt {answer['attempt']} on {answer['harness']})"
        print({"delivered": f"Delivered to {where}: the agent reads it at its next step.",
               "queued": f"Queued for {where}: the agent gets it at its next step, or as its next turn. The run's "
                         "events.jsonl says when it does, or why it doesn't.",
               "unconfirmed": f"Unconfirmed for {where}: {answer.get('reason')}."}[answer["status"]])
    return 1 if answer["status"] == "none" else 0


def _message(args, what: str = "message") -> str:
    """wf steer's message, or wf continue's request: the argument, or what --file holds. Raises ValueError."""
    if (args.message is None) == (args.file is None):
        raise ValueError(f"give the {what}, or --file, but not both")
    most = steer.MAX_CHARACTERS + 1  # enough to tell that it's too long
    if args.file is None:
        text = args.message
    elif str(args.file) == "-":
        try:
            text = sys.stdin.read(most)
        except UnicodeDecodeError as error:
            raise ValueError("stdin isn't UTF-8 text") from error
    else:
        try:
            with args.file.open(encoding="utf-8") as file:
                text = file.read(most)
        except OSError as error:
            raise ValueError(f"can't read {args.file}: {error.strerror or error}") from error
        except UnicodeDecodeError as error:
            raise ValueError(f"{args.file} isn't UTF-8 text") from error
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:  # bytes that aren't UTF-8, as the system passes them on
        raise ValueError(f"the {what} isn't UTF-8 text") from error
    if not text.strip():
        raise ValueError(f"the {what} is empty")
    if len(text) > steer.MAX_CHARACTERS:
        raise ValueError(f"the {what} is longer than {steer.MAX_CHARACTERS:,} characters")
    return text


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
        detail = f"waiting for a person: {', '.join(current.get('nodes', []))} ({current.get('answer')})"
    elif status == "failed":
        detail = f"failed: {current.get('node', '?')}: {current.get('error', '')} ({current.get('retry')})"
    elif status == "stopped":
        detail = f"stopped: {current.get('reason')} ({current.get('retry')})"
    elif status == "limit":
        detail = f"paused at a limit: {current.get('reason')} ({current.get('retry')})"
    elif status == "running" and current.get("agents_waiting"):
        detail = "running, waiting for an agent slot: " + ", ".join(w["node"] for w in current["agents_waiting"])
    else:
        detail = status
    return f"{run_id} [{current.get('workflow')}]: {detail}" + (f" (follow-up {n})" if (n := current.get("follow_up")) else "")


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


def _trace(args) -> int:
    run = _find(args.run_id, args.runs_dir)
    if run is None:
        return 1
    found = trace_view.records(run)
    if not found:
        print(f"wf trace: run {run.id} has no trace (runs keep one since wf wrote trace.jsonl)", file=sys.stderr)
        return 1
    chosen = trace_view.select(found, args.node, args.pass_no, args.field, renders=args.var is not None)
    if args.var is not None:
        try:
            path = trace_view.parse_path(args.var)
        except trace_view.PathError as error:
            print(f"wf trace: --var {error}", file=sys.stderr)
            return 1
        matches = trace_view.values(run, chosen, path)
        print(trace_view.printable(json.dumps({"run": run.id, "var": args.var, "values": matches}, indent=2,
                                              ensure_ascii=False, default=str)
                                   if args.json else trace_view.values_text(matches, args.var)))
        return 0
    if args.json:
        records = [trace_view.expanded(run, record) for record in chosen]
        print(trace_view.printable(json.dumps({"run": run.id, "dir": str(run.dir), "records": records}, indent=2,
                                              ensure_ascii=False, default=str)))
    else:
        print(trace_view.printable(trace_view.text(run, chosen, args.field) or "no records match"))
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
        own = [branch for branch, why in kept.items() if why == runs.KEPT]
        note = f"; kept its branches {', '.join(own)} (--branches deletes them)" if own else ""
        note += "".join(f"; kept branch {branch}: {why}" for branch, why in kept.items() if why != runs.KEPT)
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


def _tools_list(args) -> int:
    for tool in TOOLS.values():
        try:
            stored = keychain.load(tool.name)
        except keychain.Unavailable:
            status = "can't tell"  # no keyring to look in
        else:
            status = "not set up" if stored is None else "set up" if keychain.can_write(stored) else "reads only"
        print(f"{tool.name:<9} {status:<11} {tool.url}\n{'':<21} sign-in: {tool.sign_in}")
    return 0


def _tools_setup(args) -> int:
    from .tools import doctor  # loads the MCP client

    tool = TOOLS[args.tool]
    if not sys.stdin.isatty():
        print(f"wf tools {tool.name} setup: run it at a terminal, since it asks you to sign in", file=sys.stderr)
        return 1
    port = config.get({"tools.signin_port": args.port}).tools.signin_port
    try:
        stored = asyncio.run(signin.setup(tool, port=port, token=args.token, write=args.write, say=print, ask=input,
                                          ask_secret=getpass.getpass, open_url=webbrowser.open))
        keychain.save(tool.name, stored)
    except (signin.SetupFailed, signin.Unreachable, keychain.Unavailable) as error:
        print(f"wf tools {tool.name} setup: {error}", file=sys.stderr)
        return 1
    print(f"Saved the {tool.title} sign-in in the macOS Keychain. Checking that it works:")
    found = asyncio.run(doctor.check(tool))
    print(found.text())
    return 0 if found.status == "pass" else 1


def _tools_doctor(args) -> int:
    """Exit code 1 when a tool fails, or when the one tool asked about isn't set up."""
    from .tools import doctor  # loads the MCP client

    async def check_all():
        return await asyncio.gather(*(doctor.check(TOOLS[name]) for name in ([args.tool] if args.tool else TOOLS)))

    found = asyncio.run(check_all())
    print("\n".join(report.text() for report in found))
    return 1 if any(r.status == "fail" or (args.tool and r.status != "pass") for r in found) else 0


def _cursor_signin(args) -> int:
    """Check the key first, and keep it only when Cursor takes it, so a wrong key never replaces a good one."""
    from keyring.errors import KeyringError

    from .harnesses import Unavailable, cursor

    if not sys.stdin.isatty():
        print("wf runtimes cursor signin: run it at a terminal, since it asks for the key", file=sys.stderr)
        return 1
    print("Create a Cursor API key in the Cursor dashboard, and paste it here. It goes to the macOS Keychain, "
          "and the screen doesn't show it.")
    key = getpass.getpass("Cursor API key: ").strip()
    if not key:
        print("wf runtimes cursor signin: no key was given", file=sys.stderr)
        return 1
    try:
        found = asyncio.run(cursor.check(key=key))
    except Unavailable as error:
        print(f"wf runtimes cursor signin: {error}. The Keychain keeps what it had.", file=sys.stderr)
        return 1
    try:
        cursor.save_key(key)
    except KeyringError as error:
        print(f"wf runtimes cursor signin: can't save the key in the Keychain: {error or type(error).__name__}", file=sys.stderr)
        return 1
    print(f"Saved the Cursor API key in the macOS Keychain: {cursor.works(found)}.")
    chosen = config.get()
    if chosen.cursor.api_key_file is not None:
        print(f"Note: cursor.api_key_file in {chosen.file} names {chosen.cursor.api_key_file}, and Cursor sessions take "
              "the API key from there first. Remove api_key_file from [cursor] to use the API key that you saved.")
    return 0


def _cursor_doctor(args) -> int:
    from .harnesses import cursor

    lines, ok = asyncio.run(cursor.doctor(args.model))
    print("\n".join(lines))
    return 0 if ok else 1


def _graph(args) -> int:
    try:
        workflow = spec.load(args.file)
    except spec.SpecError as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 1
    sys.stdout.write(diagram.mermaid(workflow))
    return 0


# ---------- wf continue ----------


def _continue_command(commands, no_input: dict, runs_dir: dict) -> None:
    following = commands.add_parser(
        "continue", help="give a run that succeeded a follow-up request, and run it again from its follow-up node",
        description="Give a run that succeeded a follow-up request. The run goes on from the node that its "
                    "workflow's `follow_up` names, or else from its first agent node with edit or full access "
                    "(wf validate names it), on the same branches, with each `session: continue` node's "
                    "last session, fresh route counters, and a budget of its own. A run that runs, waits or failed "
                    "is refused: wf retry and wf answer continue those.")
    following.add_argument("run_id", metavar="run-id")
    following.add_argument("message", nargs="?", metavar="request", help="what the run should do now (or use --file)")
    following.add_argument("--file", type=Path, metavar="FILE", help="read the request from FILE (- for stdin)")
    following.add_argument("--no-input", **no_input)
    following.add_argument("--detach", action="store_true", help="continue in the background")
    following.add_argument("--json", action="store_true", help="with --detach: print the run id and follow-up number as JSON")
    following.add_argument("--limit", action="append", dest="limits", metavar="NAME=VALUE",
                           help="a limit for this follow-up instead of the file's, counted from its start: cost_usd=20, "
                                "tokens=5000000, duration=2h, or NAME=none for no limit (repeat for several)")
    following.add_argument("--runs-dir", **runs_dir)
    following.set_defaults(handler=_continue)


def _continue(args) -> int:
    try:
        request = _message(args, "request")
    except ValueError as error:
        print(f"wf continue: {error}", file=sys.stderr)
        return 1
    return runner.follow_up(args.run_id, request, args.no_input, args.detach, args.json, args.limits, args.runs_dir)


if __name__ == "__main__":  # `python -m workflow.cli`, as detached runs start it
    sys.exit(main())
