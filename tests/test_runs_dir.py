"""Run folders outside the repo: WF_RUNS_DIR and --runs-dir, on every command that makes or finds runs.

The workflow files live next to the repo, not in it, so a run that keeps its
folder elsewhere leaves `git status` empty.
"""

import json
import shlex
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from workflow import cli, runner, runs


def git(*args: str, cwd=".") -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def make_repo(folder: Path) -> Path:
    folder.mkdir(parents=True)
    git("init", "-q", cwd=folder)
    (folder / "README").write_text("hello\n")
    git("add", "README", cwd=folder)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "first", cwd=folder)
    return folder


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(make_repo(tmp_path / "repo"))
    monkeypatch.setattr(runner, "interactive", lambda: False)
    return tmp_path / "repo"


ASK = """
version: 1
name: ask
start: {input: {branch: {type: string}}}
nodes:
  make: {needs: [start], command: "true", workspace: new, branch: "{{ start.branch }}"}
  approve: {needs: [make], human: "Ship it?", output: {ok: {type: boolean}}}
end: {output: {ok: "{{ approve.ok }}", branch: "{{ workspace.make.branch }}"}}
"""

FLAKY = """
version: 1
name: flaky
start: {input: {flag: {type: string}}}
nodes:
  once: {needs: [start], command: "test -e {{ start.flag | quote }}"}
end: {output: {}}
"""


def write(path: Path, text: str) -> str:
    path.write_text(dedent(text).lstrip())
    return str(path)


def json_out(capsys):
    return json.loads(capsys.readouterr().out)


def answers(tmp_path: Path, run_id: str, given: list[str], capsys) -> str:
    """An answers file approving what the run waits on."""
    capsys.readouterr()
    assert cli.main(["status", run_id, "--json", *given]) == 0
    (question,) = json_out(capsys)["questions"]
    return write(tmp_path / "answers.json", json.dumps({question["key"]: {"ok": True}}))


def test_with_wf_runs_dir_a_run_writes_nothing_in_the_repo(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WF_RUNS_DIR", str(tmp_path / "runs"))
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert cli.main(["run", write(tmp_path / "ask.yaml", ASK), "branch=fm/task"]) == 2
    (folder,) = (tmp_path / "runs" / runs.repo_key(repo.resolve())).iterdir()
    assert runs.Run.at(folder).repo == repo.resolve()
    assert cli.main(["answer", folder.name, answers(tmp_path, folder.name, [], capsys)]) == 0
    assert json.loads((folder / "output.json").read_text()) == {"ok": True, "branch": "fm/task"}
    assert not (repo / ".wf").exists()
    assert (repo / ".git" / "info" / "exclude").read_text() == exclude
    assert git("status", "--porcelain", "--ignored") == ""
    assert git("branch", "--list", "fm/task") == "fm/task"  # the branch it made is all that's new


def test_every_command_finds_runs_in_the_runs_folder_it_is_given(repo, tmp_path, capsys):
    given = ["--runs-dir", str(tmp_path / "runs")]
    assert cli.main(["run", write(tmp_path / "ask.yaml", ASK), "branch=fm/task", *given]) == 2
    (folder,) = runs.runs_folder(repo, tmp_path / "runs").iterdir()
    run_id = folder.name
    capsys.readouterr()
    assert cli.main(["status", run_id]) == 1  # not in ./.wf/runs
    assert f"no run `{run_id}`" in capsys.readouterr().err
    assert cli.main(["wait", run_id, "--timeout", "1", *given]) == 0
    assert json_out(capsys)["status"] == "waiting"
    assert cli.main(["list", "--json", *given]) == 0
    assert [r["run"] for r in json_out(capsys)] == [run_id]
    assert cli.main(["answer", run_id, answers(tmp_path, run_id, given, capsys), *given]) == 0
    capsys.readouterr()
    assert cli.main(["report", run_id, "--json", *given]) == 0
    assert json_out(capsys)["runs"][0]["run"] == run_id
    assert cli.main(["discard", run_id, *given]) == 0
    assert "kept its branches fm/task" in capsys.readouterr().out
    assert not folder.exists()
    assert not (repo / ".wf").exists()


def test_retry_continues_a_failed_run_from_the_runs_folder(repo, tmp_path, capsys):
    given = ["--runs-dir", str(tmp_path / "runs")]
    flag = tmp_path / "flag"
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={flag}", *given]) == 1
    (folder,) = runs.runs_folder(repo, tmp_path / "runs").iterdir()
    flag.touch()
    assert cli.main(["retry", folder.name, *given]) == 0
    assert json.loads((folder / "status.json").read_text())["status"] == "succeeded"


def test_a_detached_run_and_answer_find_a_runs_folder_given_by_option(repo, tmp_path, capsys):
    given = ["--runs-dir", "../runs"]  # relative to where wf runs; the background driver gets it resolved
    assert cli.main(["run", write(tmp_path / "ask.yaml", ASK), "branch=fm/task", "--detach", "--json", *given]) == 0
    run_id = json_out(capsys)["run"]
    assert cli.main(["wait", run_id, "--timeout", "30", *given]) == 0
    assert json_out(capsys)["status"] == "waiting"
    assert cli.main(["answer", run_id, answers(tmp_path, run_id, given, capsys), "--detach", *given]) == 0
    capsys.readouterr()
    assert cli.main(["wait", run_id, "--timeout", "30", *given]) == 0
    assert json_out(capsys)["output"] == {"ok": True, "branch": "fm/task"}
    assert not (repo / ".wf").exists()


@pytest.mark.parametrize("before, after", [("runs", "elsewhere"), (None, "runs")])
def test_a_detached_run_finds_its_folder_after_runs_dir_changes_in_config_toml(repo, tmp_path, monkeypatch, capsys,
                                                                                 wf_home, before, after):
    def runs_dir(name: str | None) -> None:
        (wf_home / "config.toml").write_text(f'runs_dir = "{tmp_path / name}"\n' if name else "")

    runs_dir(before)
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={tmp_path}", "--detach", "--json"]) == 0
    run_id = json_out(capsys)["run"]
    runs_dir(after)  # while the background driver starts
    if before is None:
        monkeypatch.setenv("WF_RUNS_DIR", "")  # the run is in .wf/runs
    assert cli.main(["wait", run_id, "--timeout", "30", *(["--runs-dir", str(tmp_path / before)] if before else [])]) == 0
    assert json_out(capsys)["status"] == "succeeded"


def test_two_repos_with_the_same_name_keep_their_runs_apart(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WF_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(runner, "interactive", lambda: False)
    flow = write(tmp_path / "flaky.yaml", FLAKY)
    seen = {}
    for side in ("a", "b"):
        monkeypatch.chdir(make_repo(tmp_path / side / "proj"))
        assert cli.main(["run", flow, f"flag={tmp_path}"]) == 0
        capsys.readouterr()
        assert cli.main(["list", "--json"]) == 0
        seen[side] = [r["run"] for r in json_out(capsys)]
    assert len(seen["a"]) == len(seen["b"]) == 1 and seen["a"] != seen["b"]
    keys = sorted(p.name for p in (tmp_path / "runs").iterdir())
    assert len(keys) == 2 and all(k.startswith("proj-") for k in keys)


def test_a_run_folder_without_a_recorded_repo_is_in_its_repo(repo, tmp_path, capsys):
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={tmp_path}"]) == 0
    (folder,) = (repo / ".wf" / "runs").iterdir()
    (folder / "run.json").write_text("{}")
    assert runs.Run.at(folder).repo == repo.resolve()


def test_the_commands_wf_prints_for_a_run_find_its_runs_folder(repo, tmp_path, monkeypatch, capsys):
    given = ["--runs-dir", str(tmp_path / "runs")]
    flag = tmp_path / "flag"
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={flag}", *given]) == 1
    (folder,) = runs.runs_folder(repo, tmp_path / "runs").iterdir()
    hint = capsys.readouterr().out.split("continue with: ")[1].strip()
    assert hint == f"wf retry {folder.name} --runs-dir {(tmp_path / 'runs').resolve()}"
    assert cli.main(["status", folder.name, "--json", *given]) == 0
    assert json_out(capsys)["retry"] == hint
    monkeypatch.setenv("WF_RUNS_DIR", str(tmp_path / "runs"))  # the same, where it's typed without the setting too
    assert cli.main(["status", folder.name, "--json"]) == 0
    assert json_out(capsys)["retry"] == hint
    monkeypatch.delenv("WF_RUNS_DIR")
    flag.touch()
    assert cli.main(shlex.split(hint)[1:]) == 0  # the printed command works as it is
    assert cli.main(["run", write(tmp_path / "ask.yaml", ASK), "branch=fm/task", *given]) == 2
    assert f"Answer with: wf retry {runs.all_runs(repo, tmp_path / 'runs')[-1].id} --runs-dir " in capsys.readouterr().out
    assert cli.main(["status", runs.all_runs(repo, tmp_path / "runs")[-1].id, "--json", *given]) == 0
    assert json_out(capsys)["answer"].endswith(f"<answers.json> --runs-dir {(tmp_path / 'runs').resolve()}")


def test_a_run_in_the_repo_while_wf_runs_dir_points_elsewhere_says_how_to_find_it(repo, tmp_path, monkeypatch):
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={tmp_path / 'nope'}"]) == 1
    (run,) = runs.all_runs(repo)
    monkeypatch.setenv("WF_RUNS_DIR", str(tmp_path / "runs"))
    assert run.command("retry") == f"WF_RUNS_DIR= wf retry {run.id}"  # a blank WF_RUNS_DIR is no setting


def test_a_blank_runs_folder_setting_is_no_setting(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("WF_RUNS_DIR", "  ")
    assert runs.runs_folder(repo) == repo.resolve() / ".wf" / "runs"
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={tmp_path}", "--runs-dir", ""]) == 0
    assert len(list((repo / ".wf" / "runs").iterdir())) == 1
    assert sorted(p.name for p in repo.iterdir()) == [".git", ".wf", "README"]


def test_the_option_wins_over_wf_runs_dir(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("WF_RUNS_DIR", str(tmp_path / "from-env"))
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={tmp_path}", "--runs-dir", str(tmp_path / "given")]) == 0
    assert len(list(runs.runs_folder(repo, tmp_path / "given").iterdir())) == 1
    assert not (tmp_path / "from-env").exists()


def test_runs_dir_in_config_toml_keeps_run_folders_outside_the_repo_unless_the_variable_or_the_option_names_another(
        repo, tmp_path, monkeypatch, wf_home):
    (wf_home / "config.toml").write_text('runs_dir = "~/wf-test-runs"\n')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    flow = write(tmp_path / "flaky.yaml", FLAKY)
    assert cli.main(["run", flow, f"flag={tmp_path}"]) == 0
    assert len(list((tmp_path / "home" / "wf-test-runs" / runs.repo_key(repo.resolve())).iterdir())) == 1
    monkeypatch.setenv("WF_RUNS_DIR", str(tmp_path / "from-env"))
    assert cli.main(["run", flow, f"flag={tmp_path}"]) == 0
    assert cli.main(["run", flow, f"flag={tmp_path}", "--runs-dir", str(tmp_path / "given")]) == 0
    assert [len(list((tmp_path / name).iterdir())) for name in ("from-env", "given")] == [1, 1]
    assert not (repo / ".wf").exists()


def test_a_blank_wf_runs_dir_turns_off_the_runs_folder_of_config_toml(repo, tmp_path, monkeypatch, wf_home, capsys):
    flag = tmp_path / "flag"
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={flag}"]) == 1
    (run,) = runs.all_runs(repo)
    (wf_home / "config.toml").write_text(f'runs_dir = "{tmp_path / "runs"}"\n')
    hint = run.command("retry")
    assert hint == f"WF_RUNS_DIR= wf retry {run.id}"
    monkeypatch.setenv("WF_RUNS_DIR", "")  # as the printed command sets it
    assert run.command("retry") == hint  # what it prints then is for a new shell, which has no blank variable
    capsys.readouterr()
    assert cli.main(shlex.split(hint)[2:]) == 1
    assert f"continue with: {hint}\n" in capsys.readouterr().out
    flag.touch()
    assert cli.main(shlex.split(hint)[2:]) == 0
    assert run.status()["status"] == "succeeded"


def test_a_run_folder_in_a_runs_folder_must_record_its_repo(repo, tmp_path):
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={tmp_path}", "--runs-dir", str(tmp_path / "runs")]) == 0
    (folder,) = runs.runs_folder(repo, tmp_path / "runs").iterdir()
    (folder / "run.json").write_text("{}")
    with pytest.raises(ValueError, match="doesn't say which repo the run is in"):
        runs.Run.at(folder)  # three folders up is the runs folder's parent, not a repo


def test_a_runs_folder_inside_the_repo_is_kept_out_of_git_status(repo, tmp_path):
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={tmp_path}", "--runs-dir", "runs"]) == 0
    assert git("status", "--porcelain") == ""
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert f"/runs/{runs.repo_key(repo.resolve())}/" in exclude and ".wf/" not in exclude


def test_a_runs_folder_wf_cannot_make_fails_the_run_with_a_message(repo, tmp_path, capsys):
    (tmp_path / "a-file").write_text("")
    assert cli.main(["run", write(tmp_path / "flaky.yaml", FLAKY), f"flag={tmp_path}", "--runs-dir", str(tmp_path / "a-file")]) == 1
    assert f"wf run: can't make the run folder in {(tmp_path / 'a-file').resolve()}/" in capsys.readouterr().err
