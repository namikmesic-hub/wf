"""Limits: the caps on a whole run's spending and time, and raising them.

A workflow's `limits` (cost_usd, tokens, duration) cap the whole run,
sub-workflows included. When one is reached, attempts already running finish,
nothing new starts, and the run pauses with status `limit` (graph.py: it waits
in wait_for_resume, like a failed run). It isn't over:

    wf retry <run-id> --limit cost_usd=10       continue with a bigger budget
    wf retry <run-id> --limit tokens=none       ...or with no token limit at all
    wf run <file> --limit duration=3h           start with a different limit

A limit given this way replaces the file's for the rest of the run: it is kept
in run.json (runs.Run.set_limits), so every later `wf retry` uses it too. The
run then carries on where it stopped: a step the limit kept from retrying gets
its retry, and a sub-workflow the limit stopped continues its own steps.

This module is plain functions: parse() reads `--limit` arguments, effective()
combines them with the workflow file's, check() says which limit (if any) a
run has reached, and hint() turns that into the --limit option that continues it.
"""

from typing import Any

from pydantic import ValidationError

from .spec import Limits, seconds

NAMES = ("cost_usd", "tokens", "duration")


class LimitError(Exception):
    pass


def parse(pairs: list[str]) -> dict[str, Any]:
    """`["cost_usd=10", "tokens=none"]` -> {"cost_usd": 10.0, "tokens": None}, checked."""
    found: dict[str, Any] = {}
    for pair in pairs:
        name, equals, raw = pair.partition("=")
        if not equals or name not in NAMES:
            raise LimitError(f"expected NAME=VALUE with NAME one of {', '.join(NAMES)}, got `{pair}`")
        if raw.strip().lower() == "none":
            found[name] = None
            continue
        if name == "duration":
            found[name] = raw.strip()  # its format is checked with the others, below
            continue
        try:
            found[name] = float(raw) if name == "cost_usd" else int(raw)
        except ValueError:
            raise LimitError(f"`{pair}`: {name} is {'a number of dollars' if name == 'cost_usd' else 'a whole number'}") from None
    try:
        Limits(**{k: v for k, v in found.items() if v is not None})  # the same checks as the file's
    except ValidationError as error:
        problem = error.errors()[0]
        raise LimitError(f"{problem['loc'][0]}: {problem['msg']}") from None
    return found


def effective(file_limits: Limits, overrides: dict[str, Any]) -> Limits:
    """The limits that apply: the workflow file's, with the run's overrides on top."""
    return Limits(**{**file_limits.model_dump(), **overrides})


def check(limits: Limits, used: dict, elapsed_s: float) -> tuple[str, str] | None:
    """The first limit reached, as (its name, what happened in words), or None."""
    if limits.cost_usd is not None and used.get("cost_usd", 0.0) >= limits.cost_usd:
        return "cost_usd", f"spent ${used['cost_usd']:.2f}, limit ${limits.cost_usd:g}"
    if limits.tokens is not None and used.get("tokens", 0) >= limits.tokens:
        return "tokens", f"used {used['tokens']:,} tokens, limit {limits.tokens:,}"
    if limits.duration is not None and elapsed_s >= seconds(limits.duration):
        return "duration", f"ran for {elapsed_s:.0f}s, limit {limits.duration}"
    return None


def hint(name: str | None) -> str:
    """The option that lets `wf retry` continue a run the limit `name` (from check()) stopped."""
    return f"--limit {name or 'cost_usd'}=<more>"


def describe(limits: Limits) -> str:
    """"cost_usd $10, tokens 2,000,000, no duration limit" style, for messages."""
    parts = [f"cost_usd ${limits.cost_usd:g}" if limits.cost_usd is not None else "no cost limit",
             f"tokens {limits.tokens:,}" if limits.tokens is not None else "no token limit",
             f"duration {limits.duration}" if limits.duration is not None else "no duration limit"]
    return ", ".join(parts)
