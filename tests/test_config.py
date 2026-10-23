"""wf's settings, ~/.wf/config.toml (workflow/config/), in the WF_HOME that conftest makes for each test.

Each key takes its flag, then its environment variable, then the file, then wf's default. wf never
writes the file. A problem stops every command before it starts, and its message names the key or
the variable, the file, and what the key allows. No trace, run folder or output holds the password of metrics.url."""

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from workflow import cli, config, harnesses, runner
from workflow.config import Connection, legacy
from fakes import Fake, submit

ROOT = Path(__file__).parent.parent
SECRET = "hunter2"  # in the password of URL, which no output and no file of a run may hold
URL = "https://wf%40team:hunter2%40x@clickhouse.example.com:8443/wf"
MASKED = "https://wf%40team:***@clickhouse.example.com:8443/wf"


def write(wf_home: Path, text: str) -> Path:
    """config.toml, yours alone, as a file that holds a password must be."""
    file = wf_home / "config.toml"
    file.write_text(dedent(text).lstrip(), encoding="utf-8")
    file.chmod(0o600)
    return file


def problems(**load) -> list[str]:
    with pytest.raises(config.ConfigError) as error:
        config.load(**load)
    return error.value.problems


# ---------- one precedence for every key ----------


# For each key: its text in the file and that value, its variable's text and that value, and a flag's value
SAMPLES = {
    "runs_dir": ('"~/from-file"', Path.home() / "from-file", "/tmp/from-env", Path("/tmp/from-env"), Path("/tmp/from-flag")),
    "max_agents": ("3", 3, "5", 5, None),
    "claude.auth": ('"api-key"', "api-key", "subscription", "subscription", None),
    "cursor.runtime": ('"acp"', "acp", "sdk", "sdk", None),
    "cursor.api_key_file": ('"~/.config/cursor-key"', Path.home() / ".config" / "cursor-key", None, None, None),
    "ui.port": ("9001", 9001, None, None, 9002),
    "tools.signin_port": ("47153", 47153, None, None, 47154),
    "git.signing_timeout": ("2.5", 2.5, None, None, None),
    "paths.codex": ('"/opt/codex/bin/codex"', Path("/opt/codex/bin/codex"), None, None, None),
    "paths.cursor_agent": ('"~/bin/cursor-agent"', Path.home() / "bin" / "cursor-agent", None, None, None),
    "paths.node": ('"/opt/node-22/bin/node"', Path("/opt/node-22/bin/node"), None, None, None),
    "metrics.enabled": (f'true\nurl = "{URL}"', True, None, None, None),
    "metrics.url": (f'"{URL}"', Connection.parse(URL), None, None, None),
}


def shown(loaded: config.Config, name: str) -> tuple[object, str]:
    return loaded.value(name), str(loaded.source(name))


@pytest.mark.parametrize("name", list(config.KEYS))
def test_each_key_takes_its_flag_then_its_variable_then_the_file_then_wf_s_default(name, wf_home, monkeypatch):
    key = config.KEYS[name]
    in_file, from_file, in_env, from_env, flag = SAMPLES[name]
    assert shown(config.load(), name) == (key.default, "default")
    write(wf_home, f"[{key.section}]\n{key.leaf} = {in_file}\n" if key.section else f"{key.leaf} = {in_file}\n")
    loaded = config.load()
    assert shown(loaded, name) == (from_file, "file")
    assert (key.variable is None) == (in_env is None) and (key.flag is None) == (flag is None)
    if key.variable:
        monkeypatch.setenv(key.variable, in_env)
        loaded = config.load()
        assert shown(loaded, name) == (from_env, f"environment ({key.variable})")
    if key.flag:
        assert shown(loaded.given({name: flag}), name) == (flag, f"flag ({key.flag})")
        assert shown(loaded.given({name: None}), name) == shown(loaded, name)  # a flag with the value None


def test_every_key_has_a_sample_and_the_sources_of_today_stay():
    assert list(SAMPLES) == list(config.KEYS)
    assert {name: key.variable for name, key in config.KEYS.items() if key.variable} == {
        "runs_dir": "WF_RUNS_DIR", "max_agents": "WF_MAX_AGENTS", "claude.auth": "WF_CLAUDE_AUTH",
        "cursor.runtime": "WF_CURSOR_RUNTIME"}
    assert {name: key.flag for name, key in config.KEYS.items() if key.flag} == {
        "runs_dir": "--runs-dir", "ui.port": "wf ui --port", "tools.signin_port": "wf tools <name> setup --port"}


def test_without_a_file_each_key_has_wf_s_default_and_wf_writes_no_file(wf_home):
    loaded = config.load()
    assert loaded == config.Config(file=wf_home / "config.toml")
    assert (loaded.runs_dir, loaded.max_agents, loaded.claude.auth, loaded.cursor.runtime, loaded.ui.port,
            loaded.tools.signin_port, loaded.git.signing_timeout, loaded.metrics.enabled, loaded.metrics.url) == (
        None, None, "subscription", "sdk", 8765, 47152, 30, False, None)
    assert cli.main(["config", "show"]) == 0
    assert sorted(path.name for path in wf_home.iterdir()) == []


def test_a_blank_variable_counts_as_not_set_but_a_blank_wf_runs_dir_turns_the_runs_folder_off(wf_home, monkeypatch):
    write(wf_home, 'max_agents = 3\nruns_dir = "~/runs"\n[claude]\nauth = "api-key"\n[cursor]\nruntime = "acp"\n')
    for variable in ("WF_MAX_AGENTS", "WF_RUNS_DIR", "WF_CLAUDE_AUTH", "WF_CURSOR_RUNTIME"):
        monkeypatch.setenv(variable, "  ")
    loaded = config.load()
    assert (loaded.max_agents, loaded.claude.auth, loaded.cursor.runtime, loaded.runs_dir) == (3, "api-key", "acp", None)
    assert [str(loaded.source(name)) for name in ("max_agents", "cursor.runtime", "runs_dir")] == [
        "file", "file", "environment (WF_RUNS_DIR)"]
    assert [name for name, key in config.KEYS.items() if key.blank_is_default] == ["runs_dir"]


def test_a_blank_flag_is_no_flag(capsys, wf_home):
    write(wf_home, "[ui]\nport = 9001\n")
    assert config.flag("ui.port")("  ") is None and config.flag("runs_dir")("") is None
    assert config.flag("tools.signin_port")("47160") == 47160
    with pytest.raises(SystemExit):
        cli.main(["ui", "--port", "port"])
    assert "argument --port: `port` isn't a whole number from 0 to 65535" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["list", "--runs-dir", "~no-such-user-of-wf/runs"])
    assert ("argument --runs-dir: `~no-such-user-of-wf/runs` isn't a path, where ~ is the home folder of a user"
            in capsys.readouterr().err)


def test_a_command_keeps_the_configuration_that_it_started_with(wf_home):
    write(wf_home, "max_agents = 2\n")
    with config.pinned(config.load()):
        write(wf_home, "max_agents = 3\n")
        assert config.get().max_agents == 2
        write(wf_home, "max_agent = 3\n")  # a problem that the next command meets
        assert config.get({"ui.port": 9100}).max_agents == 2 and config.get({"ui.port": 9100}).ui.port == 9100
    with pytest.raises(config.ConfigError):
        config.get()


def test_commands_that_end_in_another_order_than_they_started_leave_nothing_pinned(wf_home):
    first, second = config.pinned(config.load()), config.pinned(config.Config(max_agents=7))
    first.__enter__()
    second.__enter__()
    first.__exit__(None, None, None)
    assert config.get().max_agents == 7
    second.__exit__(None, None, None)
    write(wf_home, "max_agents = 3\n")
    assert config.get().max_agents == 3


def environment_keys(tree: ast.Module) -> list[str | None]:
    """The variable that each read of os.environ or os.getenv in a module names: a string, or a name that
    the module sets to a string. None for a key that the module computes, which a test can't follow."""
    constants = {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
                 and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                 for target in node.targets if isinstance(target, ast.Name)}

    def environ(node: ast.expr) -> bool:
        return isinstance(node, ast.Attribute) and node.attr == "environ" or isinstance(node, ast.Name) and node.id == "environ"

    keys = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args and (
                environ(node.func.value) and node.func.attr in ("get", "pop", "setdefault") or node.func.attr == "getenv"):
            keys.append(node.args[0])
        elif isinstance(node, ast.Subscript) and environ(node.value):
            keys.append(node.slice)
        elif isinstance(node, ast.Compare) and any(environ(one) for one in node.comparators):
            keys.append(node.left)
    return [key.value if isinstance(key, ast.Constant) else constants.get(key.id) if isinstance(key, ast.Name) else None
            for key in keys]


def test_the_tests_collect_where_your_own_configuration_has_problems(tmp_path):
    (tmp_path / "config.yaml").write_text("max_agents: 4\n")
    shell = {**os.environ, "WF_HOME": str(tmp_path), "WF_MAX_AGENTS": "0"}
    done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                           "tests/test_cursor_host.py"], cwd=ROOT, env=shell, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_only_the_config_package_reads_config_toml_or_the_variables_of_its_keys():
    names = {"WF_HOME", *(key.variable for key in config.KEYS.values() if key.variable)}
    for source in (ROOT / "workflow").rglob("*.py"):
        if source.is_relative_to(ROOT / "workflow" / "config"):
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        read = environment_keys(tree)
        assert None not in read and not names & set(read), (source, read)
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names} | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not imported & {"tomllib", "tomli", "toml"}, source
        assert not {"config.toml", "config.yaml"} & {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}, source
    caught = environment_keys(ast.parse("import os\nfrom . import runs\nENV = 'WF_MAX_AGENTS'\nos.environ.get(ENV)\n"
                                        "os.environ[runs.RUNS_DIR]\n'WF_HOME' in os.environ\nos.getenv('WF_CLAUDE_AUTH')\n"))
    assert caught == ["WF_MAX_AGENTS", None, "WF_HOME", "WF_CLAUDE_AUTH"]  # each of them fails the check above


# ---------- unknown keys and wrong kinds ----------


TOP = "The top of the file takes runs_dir and max_agents, and the tables [claude], [cursor], [ui], [tools], [git], [paths] and [metrics]"
PATH = "a path that starts with / or with the ~ of a user"


@pytest.mark.parametrize("text, problem", [
    ('runs_folder = "/tmp/runs"', f"unknown key runs_folder. {TOP}"),
    ("port = 9001", f"unknown key port. {TOP} (port goes under [ui])"),
    ('[metrics]\nuri = "http://127.0.0.1:8123/wf"', "unknown key metrics.uri. [metrics] takes enabled and url"),
    ("[metrics]\nmax_agents = 2", "unknown key metrics.max_agents. [metrics] takes enabled and url (max_agents goes at "
                                  "the top of the file, above the first table)"),
    ('[cursor.sdk]\nruntime = "acp"', "unknown key cursor.sdk. [cursor] takes runtime and api_key_file"),
    ('[grafana]\nurl = "http://127.0.0.1:3000"', f"unknown key grafana. {TOP}"),
    ("ui = 9001", "ui must be a table, [ui], with port"),
    ('"ui.port" = 9001', f'unknown key "ui.port". {TOP} (port goes under [ui])'),
    ('"metrics.url" = "http://wf:hunter2@127.0.0.1:8123/wf"', f'unknown key "metrics.url". {TOP} (url goes under [metrics])'),
])
def test_an_unknown_key_is_refused_with_the_keys_that_its_place_takes(wf_home, text, problem):
    file = write(wf_home, text + "\n")
    assert problems() == [f"{file}: {problem}"]


@pytest.mark.parametrize("text, problem", [
    ('max_agents = "4"', 'max_agents must be a whole number of 1 or more, not `"4"`'),
    ("max_agents = 0", "max_agents must be a whole number of 1 or more, not `0`"),
    ("max_agents = 1979-05-27", "max_agents must be a whole number of 1 or more, not a date or a time"),
    ('runs_dir = "runs"', f'runs_dir must be {PATH}, not `"runs"`'),
    ('runs_dir = "~no-such-user-of-wf/runs"', f'runs_dir must be {PATH}, not `"~no-such-user-of-wf/runs"`'),
    ('[claude]\nauth = "token"', 'claude.auth must be subscription or api-key, not `"token"`'),
    ('[cursor]\nruntime = "cli"', 'cursor.runtime must be sdk or acp, not `"cli"`'),
    ('[cursor]\napi_key_file = ["~/key"]', f"cursor.api_key_file must be {PATH}, not an array"),
    ('[cursor]\napi_key_file = "/tmp/a\\u0000key"', f'cursor.api_key_file must be {PATH}, not `"/tmp/a\\u0000key"`'),
    ("[ui]\nport = 65536", "ui.port must be a whole number from 0 to 65535, not `65536`"),
    ('[ui]\nport = "8765"', 'ui.port must be a whole number from 0 to 65535, not `"8765"`'),
    ("[tools]\nsignin_port = 0", "tools.signin_port must be a whole number from 1 to 65535, not `0`"),
    ("[git]\nsigning_timeout = 0", "git.signing_timeout must be a number of seconds above 0, not `0`"),
    ("[git]\nsigning_timeout = nan", "git.signing_timeout must be a number of seconds above 0, not `nan`"),
    ("[git]\nsigning_timeout = true", "git.signing_timeout must be a number of seconds above 0, not `true`"),
    ("[git]\nsigning_timeout = 1" + "0" * 309, "git.signing_timeout must be a number of seconds above 0, not "
                                               f"`1{'0' * 56}...`"),
    ('[paths]\nnode = "node"', f'paths.node must be {PATH}, not `"node"`'),
    ('[metrics]\nenabled = "yes"', 'metrics.enabled must be true or false, not `"yes"`'),
    ("[metrics]\nurl = 8123", "metrics.url must be a connection string, as https://user:password@host:port/database, "
                              "not a number"),
    ('[metrics]\nurl = {user = "wf", password = "hunter2"}', "metrics.url must be a connection string, as "
                                                            "https://user:password@host:port/database, not a table"),
])
def test_a_value_of_the_wrong_kind_is_refused_with_the_key_the_file_and_what_is_allowed(wf_home, text, problem):
    file = write(wf_home, text + "\n")
    assert problems() == [f"{file}: {problem}"]


@pytest.mark.parametrize("variable, text, problem", [
    ("WF_MAX_AGENTS", "0", "WF_MAX_AGENTS must be a whole number of 1 or more, not `0`"),
    ("WF_CLAUDE_AUTH", "token", "WF_CLAUDE_AUTH must be subscription or api-key, not `token`"),
    ("WF_CURSOR_RUNTIME", "cli", "WF_CURSOR_RUNTIME must be sdk or acp, not `cli`"),
    ("WF_RUNS_DIR", "~no-such-user-of-wf/runs", "WF_RUNS_DIR must be a path, where ~ is the home folder of a user, "
                                                "not `~no-such-user-of-wf/runs`"),
])
def test_a_variable_with_a_wrong_value_is_refused(monkeypatch, variable, text, problem):
    monkeypatch.setenv(variable, text)
    assert problems() == [problem]


def test_every_problem_is_named_at_once_and_stops_every_command(wf_home, monkeypatch, capsys):
    file = write(wf_home, '[ui]\nport = "x"\n[tools]\nsigning_port = 1\n')
    monkeypatch.setenv("WF_CLAUDE_AUTH", "token")
    assert cli.main(["validate", "flow.yaml"]) == 1
    assert capsys.readouterr().err.splitlines() == [
        f"wf validate: {file}: unknown key tools.signing_port. [tools] takes signin_port",
        "wf validate: WF_CLAUDE_AUTH must be subscription or api-key, not `token`",
        f'wf validate: {file}: ui.port must be a whole number from 0 to 65535, not `"x"`']


@pytest.mark.parametrize("data, problem", [
    (b"max_agents =\n", "isn't valid TOML: Invalid value (at line 1, column 13)"),
    (b'[metrics]\nurl = "https://wf:hunter2\x01@h:1/wf"\n', "isn't valid TOML: Illegal character (at line 2, column 26)"),
    (b'[metrics]\nenabled = true\nurl = {"https://wf:hunter2@h:8123/wf" = 1, "https://wf:hunter2@h:8123/wf" = 2}\n',
     "isn't valid TOML: Duplicate inline table key (at line 3, column 78)"),
    (b'["https://wf:hunter2@h:8123/wf"]\n["https://wf:hunter2@h:8123/wf"]\n',
     "isn't valid TOML: Cannot declare a table twice (at line 2, column 32)"),
    (b"max_agents = 2\xff\n", "isn't UTF-8 text"),
    (b"max_agents = 1" + b"0" * 5000 + b"\n", "isn't valid TOML for Python: it has a number with too many digits"),
])
def test_a_file_that_isn_t_toml_is_refused_without_its_text(wf_home, data, problem):
    (wf_home / "config.toml").write_bytes(data)
    assert problems() == [f"{wf_home / 'config.toml'} {problem}"]


# ---------- metrics ----------


@pytest.mark.parametrize("url, problem", [
    ("http://127.0.0.1:8123/wf", "metrics.url lacks the user and the password."),
    ("https://wf:hunter2@clickhouse.example.com/wf", "metrics.url (https://wf:***@clickhouse.example.com/wf) lacks the port"),
    ("https://wf:hunter2@clickhouse.example.com:8443", "(https://wf:***@clickhouse.example.com:8443) lacks the database"),
    ("https://wf:hunter2@clickhouse.example.com:8443/", "(https://wf:***@clickhouse.example.com:8443/) lacks the database"),
    ("wf:hunter2@clickhouse.example.com:8443/wf", "(wf:***@clickhouse.example.com:8443/wf) lacks the scheme"),
    ("https://:hunter2@clickhouse.example.com:8443/wf", "metrics.url lacks the user."),
    ("https://wf@clickhouse.example.com:8443/wf", "metrics.url lacks the password."),
    ("https://wf:hunter2@:8443/wf", "(https://wf:***@:8443/wf) lacks the host"),
    ("clickhouse.example.com", "metrics.url lacks the scheme, the user, the password, the port and the database."),
    ("https://wf:12345/wf", "metrics.url lacks the user and the password."),  # 12345 may be a password, which wf reads as a port
    ("clickhouse://wf:hunter2@clickhouse.example.com:9000/wf", "(clickhouse://wf:***@clickhouse.example.com:9000/wf) has a "
                                                               "scheme other than http and https, and wf speaks ClickHouse's "
                                                               "HTTP interface: a clickhouse:// string names the native TCP port"),
    ("wf:hunter2@http://127.0.0.1:8123/wf", "metrics.url has text before :// that isn't a scheme."),
    ("https://wf:[hunter2]@127.0.0.1:8123/wf", "metrics.url doesn't parse as https://user:password@host:port/database."),
    ("https://wf:12345/hunter2@clickhouse.example.com:8443", "metrics.url has an @ after the host, so a / in the user or the "
                                                             "password isn't percent-encoded and lacks the database."),
    ("https://wf:hunter2/x@clickhouse.example.com:8443/wf", "metrics.url has an @ after the host, so a / in the user or the "
                                                            "password isn't percent-encoded."),  # the host is after the last @
    ("https://wf:hunter2/x@clickhouse.example.com:84430/wf/x", "metrics.url has a port that isn't a number from 1 to 65535, "
                                                               "has an @ after the host, so a / in the user or the password "
                                                               "isn't percent-encoded and has more than one name after the port"),
    ("https://wf:hunter2/x@clickhouse.example.com/wf", "metrics.url has an @ after the host, so a / in the user or the "
                                                       "password isn't percent-encoded and lacks the port."),
    ("https://wf:hunter2#1@clickhouse.example.com:8443/wf", "metrics.url has a ? or a # that isn't percent-encoded."),
    ("https://wf:hunter 2@clickhouse.example.com/wf", "metrics.url has a space or a control character that isn't "
                                                      "percent-encoded and lacks the port."),
    ("https://wf:hunter2@clickhouse.example.com:84430/wf", "metrics.url has a port that isn't a number from 1 to 65535."),
    ("https://wf:hunter2@clickhouse.example.com:84430", "metrics.url has a port that isn't a number from 1 to 65535 and "
                                                        "lacks the database."),
    ("https://wf@clickhouse.example.com:84430/wf", "metrics.url has a port that isn't a number from 1 to 65535 and lacks "
                                                   "the password."),
    ("https://:hunter2@clickhouse.example.com:84430/wf", "metrics.url has a port that isn't a number from 1 to 65535 and "
                                                         "lacks the user."),
    ("https://wf:hunter2@:84430/wf", "metrics.url has a port that isn't a number from 1 to 65535 and lacks the host."),
    ("://wf:hunter2@clickhouse.example.com:8443/wf", "(://wf:***@clickhouse.example.com:8443/wf) lacks the scheme"),
    ("wf:hunter2@http://127.0.0.1:8123", "metrics.url has text before :// that isn't a scheme and lacks the database."),
    ("https://wf:[hunter2]@127.0.0.1:8123", "metrics.url doesn't parse as https://user:password@host:port/database and "
                                            "lacks the database."),
    ("https\uff1a//wf:hunter2@clickhouse.example.com:8443/wf", "metrics.url has a character that isn't ASCII, such as a "
                                                              "full-width colon, in place of a :, /, ?, # or @."),
    ("clickhouse.example.com\uff1a8443/wf", "metrics.url has a character that isn't ASCII, such as a full-width colon, in "
                                            "place of a :, /, ?, # or @ and lacks the scheme, the user and the password."),
    ("https://wf:hunter2\uff20clickhouse.example.com:8443", "metrics.url has a character that isn't ASCII, such as a "
                                                           "full-width colon, in place of a :, /, ?, # or @ and lacks the "
                                                           "database."),
    ("http://wf:hunter2@[::1]\uff0fwf", "metrics.url has a character that isn't ASCII, such as a full-width colon, in place "
                                        "of a :, /, ?, # or @ and lacks the port."),
    ("http://wf\uff1ahunter2@[::1]/wf", "metrics.url has a character that isn't ASCII, such as a full-width colon, in place "
                                        "of a :, /, ?, # or @ and lacks the port."),
    ("https://wf:hunter2@clickhouse.example.com\uff1a8443/w\uff20f", "metrics.url has a character that isn't ASCII, such "
                                                                    "as a full-width colon, in place of a :, /, ?, # or @."),
    ("https://wf:[hunter2]@127.0.0.1:8123/w\uff20f", "metrics.url doesn't parse as https://user:password@host:port/database."),
    ("wf@http://127.0.0.1:8123/wf", "metrics.url has text before :// that isn't a scheme and lacks the password."),
    (":hunter2@http://127.0.0.1:8123/wf", "metrics.url has text before :// that isn't a scheme and lacks the user."),
    ("1http://wf@clickhouse.example.com:8443/wf", "metrics.url has text before :// that isn't a scheme."),  # 1http://wf may be the login
    ("wf\uff1ahunter2@http://127.0.0.1:8123/wf", "metrics.url has text before :// that isn't a scheme and has a character "
                                                 "that isn't ASCII, such as a full-width colon, in place of a :, /, ?, # or @."),
    ("wf:hunter2\uff20http://127.0.0.1:8123/wf", "metrics.url has text before :// that isn't a scheme and has a character "
                                                 "that isn't ASCII, such as a full-width colon, in place of a :, /, ?, # or @."),
    ("wf:hunter2@http://127.0.0.1\uff1a8123/wf", "metrics.url has text before :// that isn't a scheme and has a character "
                                                 "that isn't ASCII, such as a full-width colon, in place of a :, /, ?, # or @."),
    ("wf@http://x\uff20127.0.0.1:8123/wf", "metrics.url has text before :// that isn't a scheme and has a character that "
                                           "isn't ASCII, such as a full-width colon, in place of a :, /, ?, # or @."),
    ("wf@http://127.0.0.1:8123/w\uff20f", "metrics.url has text before :// that isn't a scheme and lacks the password."),
    ("1http://127.0.0.1:8123/w\uff20f", "metrics.url has text before :// that isn't a scheme and lacks the user and the "
                                        "password."),
    ("https://clickhouse.example.com:0/wf", "metrics.url has a port that isn't a number from 1 to 65535 and lacks the user "
                                            "and the password."),  # without an @, no reading of it has a login
    ("https://wf:0/wf", "metrics.url has a port that isn't a number from 1 to 65535 and lacks the user and the password."),
    ("wf:hunter2@clickhouse.example.com:0/wf", "metrics.url has a port that isn't a number from 1 to 65535 and lacks the "
                                               "scheme."),  # without ://, no reading of it has a scheme
    ("clickhouse.example.com:0/wf", "metrics.url has a port that isn't a number from 1 to 65535 and lacks the scheme, the "
                                    "user and the password."),
])
def test_with_metrics_enabled_wf_names_each_part_that_the_url_lacks_and_never_its_password(wf_home, url, problem, capsys):
    file = write(wf_home, f'[metrics]\nenabled = true\nurl = "{url}"\n')
    (found,) = problems()
    assert found.startswith(f"{file}: metrics.enabled is true, and metrics.") and problem in found
    assert found.endswith("Write it as https://user:password@host:port/database, with each @, :, /, ? and # in the user "
                          "and the password percent-encoded")
    assert cli.main(["config", "show"]) == 1
    assert SECRET not in found + capsys.readouterr().err


def test_with_metrics_enabled_the_url_must_be_set(wf_home):
    for text in ("[metrics]\nenabled = true\n", '[metrics]\nenabled = true\nurl = " "\n'):
        file = write(wf_home, text)
        assert problems() == [f"{file}: metrics.enabled is true, so metrics.url must be set to the connection string of "
                              "your ClickHouse, as https://user:password@host:port/database"]


def test_with_metrics_off_or_absent_wf_takes_any_url_and_exports_nothing(wf_home):
    assert config.load().metrics == config.Metrics(enabled=False, url=None)
    write(wf_home, '[metrics]\nurl = "http://127.0.0.1:8123/wf"\n')
    loaded = config.load().metrics
    assert not loaded.enabled and loaded.url.missing == ("the user", "the password")


def test_the_user_and_the_password_of_the_url_are_percent_encoded():
    url = Connection.parse("https://wf%40team:p%40ss%3Aw%2Frd%23x%25@clickhouse.example.com:8443/wf%2Dmetrics")
    assert (url.scheme, url.user, url.password, url.host, url.port, url.database) == (
        "https", "wf@team", "p@ss:w/rd#x%", "clickhouse.example.com", 8443, "wf-metrics")
    assert url.problems == []
    assert url.masked == str(url) == "https://wf%40team:***@clickhouse.example.com:8443/wf%2Dmetrics"
    assert repr(url) == "Connection('https://wf%40team:***@clickhouse.example.com:8443/wf%2Dmetrics')"
    raw = Connection.parse("http://wf:p@ss@[::1]:8123/wf")  # a raw @ reads as Python reads it: the last one ends the login
    assert (raw.password, raw.host, raw.problems, raw.masked) == ("p@ss", "::1", [], "http://wf:***@[::1]:8123/wf")
    assert Connection.parse("HTTPS://wf:pw@h:1/wf").scheme == "https"


@pytest.mark.parametrize("url, mode", [
    (URL, 0o644), (URL, 0o640), (URL, 0o604), (URL, 0o620), (URL, 0o607),
    ("http://alice:hunter2/word@127.0.0.1:8123/wf", 0o644),  # strings that don't parse, so the password may be anywhere
    ("https://wf:[hunter2]@127.0.0.1:8123/wf", 0o644),
    ("wf:hunter2@http://127.0.0.1:8123/wf", 0o644),
    ("https://wf:12345/wf", 0o644),  # strings without a user or a password, where one may stand in another part
    ("https://team/wf:hunter2", 0o644),
    ("http://127.0.0.1:8123/wf", 0o644),
])
def test_a_file_that_sets_metrics_url_must_be_yours_alone(wf_home, url, mode, capsys):
    file = write(wf_home, f'[metrics]\nenabled = false\nurl = "{url}"\n')
    file.chmod(mode)
    assert cli.main(["config", "show"]) == 1
    err = capsys.readouterr().err
    assert err == (f"wf config: {file} sets metrics.url, where the password of ClickHouse goes, and its mode "
                   f"{mode:04o} lets other users open it. Make it yours alone: chmod 600 {file}\n")
    file.chmod(0o600)
    assert cli.main(["config", "show"]) == 0
    assert SECRET not in err + capsys.readouterr().out


def test_each_test_runs_with_umask_022_whatever_the_shell_has(wf_home):
    current = os.umask(0o022)
    os.umask(current)
    assert current == 0o022  # conftest sets it: under umask 002, wf would refuse each config.toml of a test
    (wf_home / "config.toml").write_text("max_agents = 2\n")
    assert config.load().max_agents == 2


def test_a_file_without_metrics_url_can_be_read_by_others(wf_home):
    file = write(wf_home, '[cursor]\nruntime = "acp"\n[metrics]\nenabled = false\nurl = " "\n')
    file.chmod(0o644)
    assert (config.load().cursor.runtime, config.load().metrics.url) == ("acp", None)


def group_of(file: Path, monkeypatch, *, mine: bool = True, primary: tuple = (), members: tuple = ()):
    """The users that the group of `file` has, as wf reads them: `mine` makes it its owner's primary group."""
    load = sys.modules["workflow.config.load"]
    facts = os.stat(file)
    owner = load.pwd.getpwuid(facts.st_uid).pw_name
    people = [SimpleNamespace(pw_name=name, pw_gid=facts.st_gid) for name in ((owner,) if mine else ()) + primary]
    monkeypatch.setattr(load.pwd, "getpwall", lambda: people)
    monkeypatch.setattr(load.grp, "getgrgid", lambda gid: SimpleNamespace(gr_mem=list(members)))


def changeable(file: Path, mode: int) -> str:
    return (f"{file} names the programs that wf runs, and its mode {mode:04o} lets other users change it. "
            f"Make it yours: chmod go-w {file}")


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
def test_a_file_that_other_users_can_change_or_that_another_user_owns_is_refused(wf_home, mode, monkeypatch):
    file = write(wf_home, '[paths]\ncodex = "/opt/codex/bin/codex"\n')
    file.chmod(mode)
    group_of(file, monkeypatch, members=("alice",))  # a group of more users, as staff on macOS
    assert problems() == [changeable(file, mode)]
    file.chmod(0o644)
    assert config.load().paths.codex == Path("/opt/codex/bin/codex")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(file).st_uid + 1)
    assert problems() == [f"{file} belongs to another user, who can change the programs that wf runs. Use a "
                          "config.toml of your own, or set WF_HOME to a folder of yours"]


def test_group_write_is_fine_while_the_group_of_the_file_is_yours_alone(wf_home, monkeypatch):
    file = write(wf_home, '[paths]\ncodex = "/opt/codex/bin/codex"\n')
    file.chmod(0o664)  # umask 002, as with a group for each user
    group_of(file, monkeypatch)
    assert config.load().paths.codex == Path("/opt/codex/bin/codex")
    for others in ({"primary": ("alice",)}, {"members": ("alice",)}, {"mine": False}):
        group_of(file, monkeypatch, **others)  # another user's group too, or a group without you
        assert problems() == [changeable(file, 0o664)]
    group_of(file, monkeypatch)
    file.chmod(0o666)
    assert problems() == [changeable(file, 0o666)]


# ---------- config.yaml ----------


def test_while_config_yaml_is_there_wf_stops_and_names_where_each_of_its_settings_goes(wf_home, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(wf_home, "max_agents = 2\n")  # a good config.toml doesn't help
    old = wf_home / "config.yaml"
    old.write_text("max_agents: 4\ncursor_runtime: acp\ncursor_api_key_file: ~/.config/cursor-key\n"
                   "telemetry: {clickhouse_url: 'http://wf:hunter2@127.0.0.1:8123'}\n")
    (tmp_path / "flow.yaml").write_text("version: 1\nname: tiny\nstart: {input: {}}\nnodes:\n"
                                        "  a: {needs: [start], command: \"true\"}\nend: {output: {}}\n")
    assert cli.main(["run", "flow.yaml"]) == 1
    assert capsys.readouterr().err == (
        f"wf run: {old} is still there, and wf reads its settings only from {wf_home / 'config.toml'} now. Move each of "
        f"its settings to its key in config.toml, then delete {old}:\n"
        "  max_agents: max_agents, at the top of config.toml\n"
        "  cursor_runtime: runtime under [cursor]\n"
        "  cursor_api_key_file: api_key_file under [cursor]\n"
        "  telemetry: no key of config.toml takes it\n")
    assert not (tmp_path / ".wf").exists()
    old.unlink()
    assert cli.main(["run", "flow.yaml"]) == 0


@pytest.mark.parametrize("text, said", [
    ("", "It holds no settings, so delete it"),
    ("max_agents: [\n", "It isn't valid YAML, so wf can't name its settings: move them to config.toml yourself, then delete"),
])
def test_a_config_yaml_without_settings_still_stops_wf(wf_home, text, said):
    (wf_home / "config.yaml").write_text(text)
    (found,) = problems()
    assert found.startswith(f"{wf_home / 'config.yaml'} is still there") and said in found


def test_each_setting_of_config_yaml_has_its_key_in_config_toml():
    assert set(legacy.MOVED) == {"max_agents", "cursor_runtime", "cursor_api_key_file"}
    assert all(name in config.KEYS for name in legacy.MOVED.values())


# ---------- the password of metrics.url, in no output and no run folder ----------


def test_the_password_is_in_no_trace_record_run_folder_or_output(wf_home, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    write(wf_home, f'max_agents = 2\n[metrics]\nenabled = true\nurl = "{URL}"\n')
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", submit({"summary": "done"})).factory)
    (tmp_path / "flow.yaml").write_text(dedent("""
        version: 1
        name: leak
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {}}
        nodes:
          environment: {needs: [start], command: "env"}
          work: {needs: [environment], instruction: "Work with {{ environment.stdout | length }} bytes."}
        end: {output: {summary: "{{ work.summary }}"}}
    """))
    assert cli.main(["run", "flow.yaml"]) == 0
    (run_dir,) = (tmp_path / ".wf" / "runs").iterdir()
    for command in (["status", run_dir.name], ["trace", run_dir.name, "--json"], ["report", run_dir.name], ["config", "show"]):
        assert cli.main(command) == 0
    out, err = capsys.readouterr()
    assert MASKED in out and SECRET not in out + err
    assert (run_dir / "trace.jsonl").read_text() and "PATH=" in next(run_dir.glob("nodes/environment/*/attempt-1.stdout")).read_text()
    for found in [*run_dir.rglob("*"), *(wf_home / "temp").rglob("*")]:
        assert not found.is_file() or SECRET.encode() not in found.read_bytes(), found


# ---------- wf config show ----------


def rows(out: str) -> tuple[str, dict[str, tuple[str, str]]]:
    first, *lines = out.splitlines()
    return first, {name: (value, source) for name, value, source in (re.split(r"\s{2,}", line.strip()) for line in lines)}


def test_wf_config_show_gives_each_key_its_value_and_where_it_comes_from(wf_home, tmp_path, monkeypatch, capsys):
    file = write(wf_home, f"""
        max_agents = 4
        [cursor]
        runtime = "acp"
        [git]
        signing_timeout = 12.5
        [metrics]
        enabled = true
        url = "{URL}"
    """)
    monkeypatch.setenv("WF_CURSOR_RUNTIME", "sdk")
    assert cli.main(["config", "show", "--runs-dir", str(tmp_path / "runs")]) == 0
    out = capsys.readouterr().out
    first, found = rows(out)
    assert first == str(file) and list(found) == list(config.KEYS)
    assert found == {
        "runs_dir": (str(tmp_path / "runs"), "flag (--runs-dir)"),
        "max_agents": ("4", "file"),
        "claude.auth": ("subscription", "default"),
        "cursor.runtime": ("sdk", "environment (WF_CURSOR_RUNTIME)"),
        "cursor.api_key_file": ("none (the macOS Keychain)", "default"),
        "ui.port": ("8765", "default"),
        "tools.signin_port": ("47152", "default"),
        "git.signing_timeout": ("12.5", "file"),
        "paths.codex": ("none (codex on PATH)", "default"),
        "paths.cursor_agent": ("none (cursor-agent on PATH)", "default"),
        "paths.node": ("none (node on PATH)", "default"),
        "metrics.enabled": ("true", "file"),
        "metrics.url": (MASKED, "file"),
    }
    assert SECRET not in out


def test_wf_config_show_says_when_there_is_no_file(wf_home, capsys):
    assert cli.main(["config", "show"]) == 0
    first, found = rows(capsys.readouterr().out)
    assert first == f"{wf_home / 'config.toml'} (there's no such file, so no value comes from it)"
    assert {source for _, source in found.values()} == {"default"}
    assert found["runs_dir"] == ("none (.wf/runs in each repo)", "default") and found["max_agents"][0] == "none (no cap)"


@pytest.mark.parametrize("url, shown", [
    ("https://wf:hunter2/x", "(not shown, because it doesn't parse)"),  # the / that ends the host might be in the password
    ("wf:hunter2@http://127.0.0.1:8123/wf", "(not shown, because it doesn't parse)"),
    ("https://user@host:8443/db@x", "(not shown, because it doesn't parse)"),
    ("http://127.0.0.1:8123/wf", "(not shown, because it lacks the user or the password)"),
    ("https://wf:12345/wf", "(not shown, because it lacks the user or the password)"),
    ("https://team/wf:hunter2", "(not shown, because it lacks the user or the password)"),
    ("https://wfhunter2@clickhouse.example.com:8443/wf", "(not shown, because it lacks the user or the password)"),
])
def test_wf_config_show_hides_a_url_that_doesn_t_parse(wf_home, capsys, url, shown):
    write(wf_home, f'[metrics]\nurl = "{url}"\n')
    assert cli.main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert rows(out)[1]["metrics.url"] == (shown, "file") and SECRET not in out


def test_the_password_of_a_config_is_never_in_its_repr(wf_home):
    write(wf_home, f'[metrics]\nenabled = true\nurl = "{URL}"\n')
    loaded = config.load()
    assert SECRET not in repr(loaded) + str(loaded) + json.dumps(loaded.metrics.url.problems)
    assert loaded.metrics.url.password == "hunter2@x"
