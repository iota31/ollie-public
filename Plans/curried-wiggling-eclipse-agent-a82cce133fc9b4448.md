# Findings: Claude Code custom-command / skill / agent conventions on this machine

Read-only inspection of `~/.claude` (user-scope) and `./.claude` (repo-scope). Goal: identify the right installation scope for a generic, reusable multi-model review command.

---

## 1. Scope landscape on this machine

### User scope — `~/.claude/` (cross-project, applies everywhere)

| Path | Purpose |
|---|---|
| `~/.claude/skills/` | **Canonical home for user-level skills.** 50+ skill dirs (RedTeam, Research, Thinking/Council, Fabric, Evals, Utilities, etc.). Each contains a required `SKILL.md` with YAML frontmatter (`name`, `description`). |
| `~/.claude/agents/` | **Canonical home for user-level subagent definitions.** 28 `.md` files (Algorithm.md, Engineer.md, Architect.md, ClaudeResearcher.md, GeminiResearcher.md, GrokResearcher.md, CodexResearcher.md, PerplexityResearcher.md, etc.). These are the existing "multi-model reviewer" pattern Tushar already has wired up. |
| `~/.claude/USER/SKILLCUSTOMIZATIONS/` | Per-skill user overrides. Skills check this dir at runtime and apply any `PREFERENCES.md` over defaults. Currently only a README, no per-skill subdirs. |
| `~/.claude/plugins/cache/` | Marketplace-installed plugins (frontend-design, code-review, ralph-loop, vercel, telegram, context7, etc.). Has its own command/skill namespaces (e.g. `vercel:deploy`, `vercel:bootstrap`). Not the right scope for a custom one-off. |
| `~/.claude/plugins/data/` | Plugin metadata; not for custom installs. |
| `~/.claude/settings.json` | Global harness settings (statusline, hooks, model, permissions). No user-defined commands section here. |
| `~/.claude/settings.local.json` | Local-only permission allowlist (read in full; benign — Tailscale/gh/curl/whisper/etc.). |
| `~/.claude/.skills-disabled/frontend-design/` | Disabled skill with `SKILL.md`. Demonstrates an existing frontmatter-only skill that can be moved aside. |

### Repo scope — `./.claude/`

| Path | Purpose |
|---|---|
| `./.claude/settings.local.json` | Repo-specific permission allowlist (Tailscale SSH to box, etc.). |
| `./.claude/worktrees/` | Per-agent and `ollie-mc-wt` worktree dirs. Each contains its own `.claude/settings.local.json` for that worktree. |

**No in-repo `skills/` or `agents/` directories exist.** This codebase has never used repo-scoped Claude skills/commands — Tushar's pattern is consistently user-scope.

---

## 2. How the existing "multi-model reviewer" pattern is implemented

Tushar already has a working multi-model review pattern: a set of named researcher subagents in `~/.claude/agents/`, each delegating to a different vendor/model:

- `~/.claude/agents/ClaudeResearcher.md`
- `~/.claude/agents/GeminiResearcher.md`
- `~/.claude/agents/GrokResearcher.md`
- `~/Claude/agents/CodexResearcher.md`
- `~/.claude/agents/PerplexityResearcher.md`

These are plain markdown subagent definitions (not slash-commands). Higher-level orchestration lives in skills like `RedTeam` (`~/.claude/skills/RedTeam/SKILL.md` — "32 parallel expert agents" decompose-then-attack workflow) and `Thinking/Council` (`~/.claude/skills/Thinking/Council/SKILL.md` — council-style debate).

### Conventions visible from these files

- **Skill format:** Required `SKILL.md` with YAML frontmatter (`name`, `description`, optional `effort`, `argument-hint`, `license`). Body uses `## Workflow Routing` table, `## Quick Reference`, sub-resources under `Workflows/`, `references/`, `assets/`.
- **Progressive disclosure:** SKILL.md stays lean; heavy reference goes to `Workflows/*.md` (see RedTeam's `Workflows/ParallelAnalysis.md`, `Workflows/AdversarialValidation.md`).
- **Customization hook:** Many skills (e.g. RedTeam) explicitly check `~/.claude/USER/SKILLCUSTOMIZATIONS/<SkillName>/` at runtime and apply overrides.
- **Subagent format:** Plain markdown with system-prompt-style body. Discovered by the Task tool and invokable by `name`.
- **No slash-command convention.** None of the inspected files use `commands/` directories or `name: foo` slash-command frontmatter — the user-level pattern is **skills + subagents**, not commands.

---

## 3. Skill-creator reference (for the new command)

- `~/.claude/skills/skill-creator/SKILL.md` — canonical guide. Confirms: every skill needs `SKILL.md` + frontmatter (`name`, `description`); description written in third person; optional `scripts/`, `references/`, `assets/`.
- Key principle the new command should follow: "If files are large (>10k words), include grep search patterns in SKILL.md"; keep core procedure in SKILL.md, push detail to `references/` to avoid context bloat.

---

## 4. Recommended installation scope for a generic reusable multi-model review command

**Recommendation: USER scope at `~/.claude/skills/<name>/SKILL.md`** (NOT repo-scoped, NOT under `~/.claude/commands/`).

### Why user-scope skill, specifically

1. **Matches the established pattern.** All 50+ existing skills live in `~/.claude/skills/`. Tushar's habit (visible in `gitStatus` history of this conversation) is to keep the toolbox global, not per-repo.
2. **Cross-project by design.** A "generic reusable multi-model review command" is meant to be invoked anywhere (any repo, any worktree). Repo-scoped `.claude/skills/` would force duplication across every new project.
3. **Composes with existing subagents.** The user already has `ClaudeResearcher.md`, `GeminiResearcher.md`, `GrokResearcher.md`, `CodexResearcher.md`, `PerplexityResearcher.md` in `~/.claude/agents/`. A new skill at user scope can invoke them directly via the Task tool without redefining the model fan-out — minimal new code.
4. **No slash-command scaffolding needed.** There's no `commands/` directory in either `~/.claude/` or the repo. The natural invocation surface here is `/skill-name` (auto-derived from the skill dir name) — same as `ponytail`, `RedTeam`, `Research`, etc.
5. **Customization hook already wired.** Drop a `~/.claude/USER/SKILLCUSTOMIZATIONS/<name>/PREFERENCES.md` later if Tushar wants per-skill overrides — convention is already in place.
6. **Works inside worktrees.** All `ollie-mc-wt` and `agent-*` worktrees in `ollie/.claude/worktrees/` inherit user-scope skills automatically (each carries its own `settings.local.json` but loads skills from the user dir).

### Exact target paths

- **Primary (new skill body):**
  `~/.claude/skills/<skill-name>/SKILL.md`
- **Sub-resources (optional, follow progressive-disclosure convention):**
  `~/.claude/skills/<skill-name>/references/prompts.md` — per-model prompt templates
  `~/.claude/skills/<skill-name>/Workflows/parallel-review.md` — orchestration recipe
  `~/.claude/skills/<skill-name>/scripts/fanout.sh` (only if deterministic glue is needed)
- **Per-skill override (defer, not needed on first install):**
  `~/.claude/USER/SKILLCUSTOMIZATIONS/<skill-name>/PREFERENCES.md`
- **Not recommended:**
  `./.claude/skills/...` (repo-scoped — wrong granularity for a generic command)
  `~/.claude/commands/...` (no such convention in this environment)
  `~/.claude/plugins/...` (that's marketplace-installed, not user-authored)

---

## 5. Reusable assets already in place (don't reinvent)

When designing the new skill, lean on what's already there:

- `~/.claude/agents/ClaudeResearcher.md`
- `~/.claude/agents/GeminiResearcher.md`
- `~/.claude/agents/GrokResearcher.md`
- `~/.claude/agents/CodexResearcher.md`
- `~/.claude/agents/PerplexityResearcher.md`
- `~/.claude/agents/Algorithm.md`, `Engineer.md`, `Architect.md`, `QATester.md`, `Pentester.md` (perspective-pool if the review needs specialist lenses)
- `~/.claude/skills/RedTeam/` — reference for adversarial/synthesis structure
- `~/.claude/skills/Thinking/Council/` — reference for council/debate structure
- `~/.claude/skills/skill-creator/SKILL.md` — canonical authoring guide

---

## 6. Constraints surfaced by memory

- [Delegate model allowlist](feedback_delegate_models.md) restricts delegated work to: 5.6 Sol, Grok Build, or MiniMax. **Never GPT-5.4.** Any multi-model fan-out in the new skill must respect this allowlist.
- [Commit in small frequent increments](feedback_commit_frequency.md) — install the skill as one focused commit when the time comes.
- [Scope to exactly what's asked](feedback_scope_to_request.md) — keep the skill lean; push prompt templates to `references/`, not SKILL.md.
- [verification-before-completion](feedback_verify_delegated_box_work.md) — when the skill is installed, independently verify it's discoverable (`ls ~/.claude/skills/`) and that the Task tool can route to the underlying subagents, before claiming success.

---

## 7. Suggested next steps (not executed — plan mode)

1. Decide a skill name (e.g. `multimodel-review`, `council-review`, or something domain-flavored).
2. Draft `SKILL.md` frontmatter: `name`, `description` (third-person, trigger phrases), `argument-hint` (e.g. `"[target]"`), `license`, `effort: high`.
3. Author the body in the shape of existing skills: workflow-routing table, quick-reference, then a single default workflow that fans out to existing researcher subagents and synthesizes.
4. Put per-model prompt differences in `references/prompts.md` (progressive disclosure).
5. Optional: a `Workflows/parallel-review.md` for the orchestration recipe.
6. After install, verify with `ls ~/.claude/skills/` and a dry-run invocation.