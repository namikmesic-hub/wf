"""Spec: the workflow file as Python objects.

load() reads a YAML file into a Workflow: Pydantic models that mirror the file
format in docs/workflow-spec.md, one class per section. Parsing only checks
the shape of the file: which keys exist and what types they hold. Checks that
need the whole file, such as "this `needs` entry names a real node", are in
validate.py.

Worth knowing:
- Every model forbids unknown keys (extra="forbid"), so a typo like
  `instrction:` is an error instead of being silently ignored.
- A node's kind isn't written down: it's whichever of `instruction`,
  `command`, `human` or `workflow` the node has. _node_kind() picks the model
  to parse the node with (Pydantic's "callable discriminator").
- The YAML loader rejects duplicate keys, which PyYAML would otherwise
  resolve by silently keeping the last one. Two nodes with the same id are an
  error.
- A setting a node leaves out comes from `defaults`, then from the built-in
  default. Workflow.setting() does that lookup, so nothing else has to.

Errors come back as Problems (where + message) rather than Pydantic's own
format, so `wf validate` prints one line per problem with its key path.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    PlainValidator,
    PrivateAttr,
    StrictBool,
    Tag,
    ValidationError,
)
from pydantic_core import PydanticCustomError

HARNESSES = ("claude-code", "codex", "cursor")
KIND_KEYS = {"instruction": "agent", "command": "command", "human": "human", "workflow": "workflow"}
RESERVED = frozenset({"start", "end", "run", "history", "workspace", "errors", "item", "index", "session"})
NODE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a workflow file. `where` is a key path like nodes.review.routes[2]."""

    where: str
    message: str
    level: Literal["error", "warning"] = "error"
    file: Path | None = None

    def __str__(self) -> str:
        prefix = f"{self.file.name}: " if self.file else ""
        level = "warning: " if self.level == "warning" else ""
        return f"{prefix}{level}{self.where}: {self.message}" if self.where else f"{prefix}{level}{self.message}"


class SpecError(Exception):
    """The file couldn't be parsed into a Workflow."""

    def __init__(self, problems: list[Problem]):
        super().__init__("\n".join(map(str, problems)))
        self.problems = problems


def seconds(duration: str) -> int:
    """'90s' -> 90, '30m' -> 1800, '1h30m' -> 5400."""
    hours, minutes, secs = (int(part or 0) for part in _DURATION.match(duration).groups())
    return hours * 3600 + minutes * 60 + secs


# ---------- field types with readable errors ----------


def _duration(value: Any) -> str:
    if isinstance(value, str) and value and _DURATION.match(value):
        return value
    raise PydanticCustomError("duration", "expected a duration like 90s, 30m or 2h")


def _name_or_list(value: Any) -> str | list[str]:
    if isinstance(value, str) or (isinstance(value, list) and value and all(isinstance(v, str) for v in value)):
        return value
    raise PydanticCustomError("name_or_list", "expected a name or a list of names")


def _per_harness(value: Any) -> str | dict[str, str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and all(k in HARNESSES and isinstance(v, str) for k, v in value.items()):
        return value
    raise PydanticCustomError(
        "per_harness", "expected a string, or a map from harness ({harnesses}) to string", {"harnesses": ", ".join(HARNESSES)}
    )


def _text(value: Any) -> Any:
    """env values: YAML numbers and booleans become strings, as they would in a shell."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if isinstance(value, (int, float)) else value


def _tool_use(value: Any) -> Any:
    """`slack` is short for {name: slack}, which reads only."""
    if isinstance(value, str):
        return {"name": value}
    if isinstance(value, (dict, BaseModel)):
        return value
    raise PydanticCustomError("tool_use", "expected a tool name, or {name: ..., write: true}")


def _end_value(value: Any) -> str | dict:
    if isinstance(value, (str, dict)):
        return value
    raise PydanticCustomError("end_value", "expected a single field reference like \"{{ node.field }}\", or a schema with `value`")


Duration = Annotated[str, PlainValidator(_duration)]
NameOrList = Annotated[str | list[str], PlainValidator(_name_or_list)]
PerHarness = Annotated[str | dict[str, str], PlainValidator(_per_harness)]
FieldMap = dict[str, dict[str, Any]]
Access = Literal["read-only", "edit", "full"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


# ---------- nodes ----------


class ToolUse(_Model):
    """A tool an agent node may use (`wf tools list`). `write: true` adds the operations that aren't reads."""

    name: str
    write: StrictBool = False  # `write: "no"` is an error, not a quiet false


Tools = list[Annotated[ToolUse, BeforeValidator(_tool_use)]]


class Route(_Model):
    when: str | None = None
    to: NameOrList
    max: int | None = Field(None, ge=1)

    @property
    def targets(self) -> tuple[str, ...]:
        return (self.to,) if isinstance(self.to, str) else tuple(self.to)


class _Node(_Model):
    """Fields every kind of node accepts. None means "not set here"; see Workflow.setting()."""

    kind: ClassVar[str]
    needs: list[str] = []
    routes: list[Route] = []
    for_each: str | None = None
    max_parallel: int | None = Field(None, ge=1)
    max_items: int | None = Field(None, ge=0)
    on_item_error: Literal["fail", "continue"] | None = None
    workspace: str | None = None
    base: str | None = None
    branch: str | None = None
    timeout: Duration | None = None
    retry: int | None = Field(None, ge=0)
    description: str | None = None


class AgentNode(_Node):
    kind: ClassVar[str] = "agent"
    instruction: str
    output: FieldMap | None = None
    harness: NameOrList | None = None
    model: PerHarness | None = None
    effort: PerHarness | None = None
    access: Access | None = None
    web: bool | None = None
    session: Literal["fresh", "continue"] | None = None
    max_turns: int | None = Field(None, ge=1)
    budget_usd: float | None = Field(None, gt=0)
    tools: Tools | None = None


class CommandNode(_Node):
    kind: ClassVar[str] = "command"
    command: str
    env: dict[str, Annotated[str, BeforeValidator(_text)]] = {}
    check: bool | None = None


class HumanNode(_Node):
    kind: ClassVar[str] = "human"
    human: str
    output: FieldMap


class WorkflowNode(_Node):
    kind: ClassVar[str] = "workflow"
    workflow: str
    with_: dict[str, Any] = Field({}, alias="with")


def _node_kind(value: Any) -> str | None:
    if isinstance(value, _Node):
        return value.kind
    if not isinstance(value, dict):
        return None
    found = [kind for key, kind in KIND_KEYS.items() if key in value]
    return found[0] if len(found) == 1 else None


Node = Annotated[
    Annotated[AgentNode, Tag("agent")]
    | Annotated[CommandNode, Tag("command")]
    | Annotated[HumanNode, Tag("human")]
    | Annotated[WorkflowNode, Tag("workflow")],
    Discriminator(
        _node_kind,
        custom_error_type="node_kind",
        custom_error_message="a node needs exactly one of `instruction`, `command`, `human` or `workflow`",
    ),
]


# ---------- the other sections ----------


class HarnessSettings(_Model):
    model: str | None = None
    effort: str | None = None
    max_turns: int | None = Field(None, ge=1)
    budget_usd: float | None = Field(None, gt=0)


class Defaults(_Model):
    """Settings every node inherits. Not `needs`, `routes`, `for_each`, the kind keys,
    or fields that only make sense per node (`output`, `with`, `branch`, `description`)."""

    harness: NameOrList | None = None
    harnesses: dict[Literal["claude-code", "codex", "cursor"], HarnessSettings] = {}
    model: PerHarness | None = None
    effort: PerHarness | None = None
    access: Access | None = None
    web: bool | None = None
    tools: Tools | None = None
    session: Literal["fresh", "continue"] | None = None
    max_turns: int | None = Field(None, ge=1)
    budget_usd: float | None = Field(None, gt=0)
    workspace: Literal["repo", "new", "temp"] | None = None
    base: str | None = None
    timeout: Duration | None = None
    retry: int | None = Field(None, ge=0)
    max_parallel: int | None = Field(None, ge=1)
    max_items: int | None = Field(None, ge=0)
    on_item_error: Literal["fail", "continue"] | None = None
    check: bool | None = None
    env: dict[str, Annotated[str, BeforeValidator(_text)]] = {}


class Limits(_Model):
    cost_usd: float | None = Field(None, gt=0)
    tokens: int | None = Field(None, gt=0)
    duration: Duration | None = None


class Start(_Model):
    input: FieldMap = {}


class End(_Model):
    output: dict[str, Annotated[str | dict, PlainValidator(_end_value)]] = {}


# Built-in defaults, used when neither the node nor `defaults` sets a field
_BUILTIN = {
    "max_parallel": 4,
    "max_items": 20,
    "on_item_error": "fail",
    "workspace": "repo",
    "access": "read-only",
    "web": False,
    "tools": (),
    "session": "fresh",
    "check": True,
}
_TIMEOUT = {"agent": "60m", "command": "60m"}  # human and workflow nodes have none
_RETRY = {"agent": 1}  # others: 0


class Workflow(_Model):
    version: Literal[1]
    name: str = Field(pattern=r"^[a-z0-9-]+$")
    description: str = ""
    defaults: Defaults = Defaults()
    limits: Limits = Limits()
    types: dict[str, FieldMap] = {}
    start: Start
    nodes: dict[str, Node]
    end: End

    _path: Path | None = PrivateAttr(None)

    @property
    def path(self) -> Path | None:
        """The file this was loaded from."""
        return self._path

    def setting(self, node_id: str, name: str) -> Any:
        """A node's effective value for `name`: its own, else `defaults`, else the built-in default."""
        node = self.nodes[node_id]
        if name == "env":  # env maps merge, the node's keys winning
            return {**self.defaults.env, **getattr(node, "env", {})}
        value = getattr(node, name, None)
        if value is None and name in Defaults.model_fields:
            if name != "timeout" or node.kind in _TIMEOUT:
                value = getattr(self.defaults, name)
        if value is not None:
            return value
        if name == "timeout":
            return _TIMEOUT.get(node.kind)
        if name == "retry":
            return _RETRY.get(node.kind, 0)
        return _BUILTIN.get(name)

    def harness_setting(self, node_id: str, harness: str, name: str) -> Any:
        """model, effort, max_turns or budget_usd for one harness: the node's, else
        defaults.harnesses.<harness>, else defaults.<name>. A map is read by harness."""
        return self.harness_setting_at(node_id, harness, name)[1]

    def harness_setting_at(self, node_id: str, harness: str, name: str) -> tuple[str | None, Any]:
        """harness_setting(), with the key path it comes from, like defaults.harnesses.codex.model."""
        for where, value in ((f"nodes.{node_id}.{name}", getattr(self.nodes[node_id], name, None)),
                             (f"defaults.harnesses.{harness}.{name}", getattr(self.defaults.harnesses.get(harness), name, None)),
                             (f"defaults.{name}", getattr(self.defaults, name))):
            if isinstance(value, dict):
                where, value = f"{where}.{harness}", value.get(harness)
            if value is not None:
                return where, value
        return None, None


# ---------- loading ----------


class _Loader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys."""


def _mapping(loader: _Loader, node: yaml.MappingNode) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        try:
            hash(key)
        except TypeError:
            raise yaml.constructor.ConstructorError(None, None, "keys must be simple values", key_node.start_mark) from None
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key `{key}`", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load(path: Path) -> Workflow:
    """Read and parse a workflow file. Raises SpecError with every problem found."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SpecError([Problem("", f"can't read the file: {error.strerror}", file=path)]) from error
    except UnicodeDecodeError as error:
        raise SpecError([Problem("", f"the file isn't valid UTF-8: {error}", file=path)]) from error
    return parse(text, path)


def parse(text: str, path: Path | None = None) -> Workflow:
    """Parse workflow YAML. Raises SpecError with every problem found."""
    try:
        data = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark else ""
        message = getattr(error, "problem", None) or str(error)
        raise SpecError([Problem(where, message, file=path)]) from error
    if not isinstance(data, dict):
        raise SpecError([Problem("", "a workflow file must be a YAML map with version, name, start, nodes and end", file=path)])
    try:
        workflow = Workflow.model_validate(data)
    except ValidationError as error:
        raise SpecError(_problems(error, path)) from error
    workflow._path = path
    return workflow


_MESSAGES = {
    "extra_forbidden": "unknown key",
    "missing": "missing required key",
    "string_pattern_mismatch": None,  # filled in below by location
}


def _problems(error: ValidationError, path: Path | None) -> list[Problem]:
    problems = []
    for e in error.errors():
        loc = list(e["loc"])
        if len(loc) > 2 and loc[0] == "nodes" and loc[2] in KIND_KEYS.values():
            del loc[2]  # the discriminator tag Pydantic adds to the location
        message = _MESSAGES.get(e["type"]) or e["msg"]
        if e["type"] == "string_pattern_mismatch" and loc == ["name"]:
            message = "use lowercase letters, digits and -"
        if e["type"] == "node_kind" and isinstance(e.get("input"), dict):
            found = [k for k in KIND_KEYS if k in e["input"]]
            known = {name for model in (AgentNode, CommandNode, HumanNode, WorkflowNode) for name in model.model_fields}
            unknown = [k for k in e["input"] if k not in known and k != "with"]
            if found:
                message = f"a node has exactly one kind, but this one has {', '.join(f'`{k}`' for k in found)}"
            elif unknown:
                message += f" (unknown keys: {', '.join(f'`{k}`' for k in unknown)})"
        problems.append(Problem(_where(loc), message, file=path))
    return problems


def _where(loc: list) -> str:
    text = ""
    for part in loc:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text
