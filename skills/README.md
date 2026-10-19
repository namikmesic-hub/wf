# Skills

Agent skills for working with wf, in the standard `SKILL.md` folder format.

| Skill | For |
|---|---|
| [`wf-workflows`](wf-workflows/SKILL.md) | building, changing and improving workflow files: design patterns, the file format, dry runs, and improving from `wf report` evidence |

The skill that teaches firstmate's first mate to *operate* wf runs (start,
watch, answer, retry) is part of the firstmate integration:
[`integrations/firstmate/wf/SKILL.md`](../integrations/firstmate/wf/SKILL.md).

## Installing

Copy or link the folder wherever your agent loads skills from:

- **Claude Code:** `ln -s "$PWD/skills/wf-workflows" ~/.claude/skills/wf-workflows`
  for every project, or into a project's `.claude/skills/` for one.
- **firstmate:** copy it to your firstmate checkout's `.agents/skills/wf-workflows/`
  and add a trigger line to `.agents/skills/agent-skill-trigger-index/SKILL.md`:
  `- \`wf-workflows\` - load before creating, editing or improving a wf workflow file.`
- **Other agents:** any agent that reads `SKILL.md` folders; otherwise point it at
  `wf-workflows/SKILL.md` from its instructions file (`AGENTS.md`).

The skill expects `wf` on the agent's PATH (`uv tool install --editable /path/to/wf`).
