# claude-obsidian — Claude + Obsidian Wiki Vault

This folder is both a Claude Code plugin and an Obsidian vault.

**Plugin name:** `claude-obsidian` (v1.7+ "Compound Vault" — see [docs/compound-vault-guide.md](docs/compound-vault-guide.md); v1.8+ adds methodology modes — see [docs/methodology-modes-guide.md](docs/methodology-modes-guide.md))
**Skills:** `/wiki`, `/wiki-ingest`, `/wiki-query`, `/wiki-lint`, `/wiki-cli` (v1.7), `/wiki-retrieve` (v1.7, opt-in), `/wiki-mode` (v1.8)
**Vault path:** the active vault under `/vaults/<name>` (open in Obsidian directly). The resolver locates it via its `.obsidian/` marker (walking up from CWD), independent of the plugin install — **distinct from the plugin at `/opt/claude-obsidian`**. See the Maintenance Policy below for the split-install layout. (In a classic unified install where the plugin lives inside the vault, "the vault" is simply this directory.)

## Maintenance Policy — Patching the Plugin (split-install deployment)

This fork is deployed as a **split install**: the plugin lives at `/opt/claude-obsidian`
(shared, root-owned), vaults live at `/vaults/<name>`, and the container image is
built from `/bootstrap/Dockerfile`, which **clones the plugin from this fork's `main`**
(`https://github.com/felix1005/claude-obsidian`; upstream:
`https://github.com/AgriciDaniel/claude-obsidian`).

**Any future fix to the plugin MUST follow all three steps**, in this order, to
maximize portability:

1. **Patch the live install** at `/opt/claude-obsidian` so the *running* container
   benefits immediately. Needs root: use `sudo` (present in rebuilt images), or
   from the host `docker exec -u root <container> …`. Preferred — pull the fix
   straight from the fork rather than hand-editing:
   ```bash
   git config --global --add safe.directory /opt/claude-obsidian
   git -C /opt/claude-obsidian fetch https://github.com/felix1005/claude-obsidian main
   git -C /opt/claude-obsidian checkout FETCH_HEAD -- <changed files>
   ```
2. **Raise a PR on the fork and merge it** (`gh`, branch → `felix1005/.../main`).
   PRs stay on the **fork only** — do NOT open PRs against the upstream
   `AgriciDaniel` repo. Upstream is referenced for provenance/rebasing only; this
   fork's `main` is the source of truth the deployment builds from.
3. **Confirm the next image build catches it.** `/bootstrap/Dockerfile` re-clones
   from the fork on every build, so after `docker build -t claude-obsidian /bootstrap`
   + container recreate, fresh containers ship the patch automatically. Pin
   `--branch <tag>` in the Dockerfile for reproducible builds once a tag is cut.

Note on `~/.claude/CLAUDE.md`: `entrypoint.sh` appends this file to the user-level
`CLAUDE.md` **once per `~/.claude` volume** (guarded by
`~/.claude/.claude-obsidian-installed`). Existing deployments keep their current
`CLAUDE.md`; only a **fresh volume** re-imports this content from the fork — which
is why this policy lives here, in the fork.

Rationale: step 1 unblocks the current session, step 2 makes the fix durable and
reviewable on the fork, step 3 makes it portable across every future rebuild and
fresh deployment. Skipping step 1 leaves the live container broken; skipping
steps 2–3 means the fix is silently wiped on the next rebuild.

## What This Vault Is For

This vault demonstrates the LLM Wiki pattern — a persistent, compounding knowledge base for Claude + Obsidian. Drop any source, ask any question, and the wiki grows richer with every session.

## Vault Structure

```
.raw/           source documents — immutable, Claude reads but never modifies
wiki/           Claude-generated knowledge base
_templates/     Obsidian Templater templates
_attachments/   images and PDFs referenced by wiki pages
```

## How to Use

Drop a source file into `.raw/`, then tell Claude: "ingest [filename]".

Ask any question. Claude reads the index first, then drills into relevant pages.

Run `/wiki` to scaffold a new vault or check setup status.

Run "lint the wiki" every 10-15 ingests to catch orphans and gaps.

## Cross-Project Access

To reference this wiki from another Claude Code project, add to that project's CLAUDE.md:

```markdown
## Wiki Knowledge Base
Path: /path/to/this/vault

When you need context not already in this project:
1. Read wiki/hot.md first (recent context, ~500 words)
2. If not enough, read wiki/index.md
3. If you need domain specifics, read wiki/<domain>/_index.md
4. Only then read individual wiki pages

Do NOT read the wiki for general coding questions or things already in this project.
```

## Plugin Skills

| Skill | Trigger |
|-------|---------|
| `/wiki` | Setup, scaffold, route to sub-skills |
| `ingest [source]` | Single or batch source ingestion |
| `query: [question]` | Answer from wiki content |
| `lint the wiki` | Health check |
| `/save` | File the current conversation as a structured wiki note |
| `/autoresearch [topic]` | Autonomous research loop: search, fetch, synthesize, file |
| `/canvas` | Visual layer: add images, PDFs, notes to Obsidian canvas |
| `/wiki-cli` (v1.7) | Obsidian CLI transport wrapper; default mutation path on desktop |
| `/wiki-retrieve` (v1.7) | Hybrid contextual + BM25 + cosine-rerank retrieval (opt-in via `bash bin/setup-retrieve.sh`) |
| `/wiki-mode` (v1.8) | Methodology modes (LYT / PARA / Zettelkasten / Generic). Set via `bash bin/setup-mode.sh`; consumed by wiki-ingest / save / autoresearch for routing new pages |
| `/think` (v1.9) | The 10-principle thinking loop (OBSERVE-OBSERVE-LISTEN-THINK-CONNECT-CONNECT-FEEL-ACCEPT-CREATE-GROW) as an invocable workflow. Apply to architectural decisions, audits, post-mortems, ambiguous user requests. Every other skill has a "How to think" appendix mapping this framework to its specific work |

## Transport (v1.7+)

`scripts/detect-transport.sh` writes `.vault-meta/transport.json` on first run and refreshes weekly. Skills consult it before mutating the vault. Fallback chain: Obsidian CLI → mcp-obsidian → mcpvault → filesystem (always-available floor). Decision tree: [wiki/references/transport-fallback.md](wiki/references/transport-fallback.md).

## Concurrency (v1.7+)

`scripts/wiki-lock.sh` provides per-file advisory locks for safe multi-writer ingest. Every wiki page write should be guarded by `wiki-lock acquire`/`release`. Stale-after default is 60s; cross-process release allowed by design. The PostToolUse hook defers `git add` while locks are held. Closes the latent multi-writer corruption hole from v1.6.

## Methodology Modes (v1.8+)

Pick an organizational style for the vault via `bash bin/setup-mode.sh`. Four modes available: **generic** (v1.7 default — no opinion), **LYT** (Linking Your Thinking — MOCs + atomic notes), **PARA** (Projects/Areas/Resources/Archives), **Zettelkasten** (timestamped IDs, flat, dense linking). The mode is written to `.vault-meta/mode.json` (gitignored by default; `git add -f` to commit). `wiki-ingest`, `save`, and `autoresearch` consult `python3 scripts/wiki-mode.py route <type> "<name>"` before filing new pages — no special-casing needed in the consumer skills. Full guide: [docs/methodology-modes-guide.md](docs/methodology-modes-guide.md). Closes priority gap 5 from the May 2026 compass artifact.

## Pre-commit verifier (v1.7.1+)

After staging changes for a non-trivial workstream but BEFORE running `git commit`, dispatch the `verifier` agent (`agents/verifier.md`). It reads `git diff --cached`, applies the /best-practices six-cut + agent kernel, and returns findings in four tiers (BLOCKER / HIGH / MEDIUM / LOW) with file:line citations. The agent has read-only tools (Read, Grep, Glob, Bash) — it can inspect but never modify, so its output is purely advisory. This closes the loop the v1.7 audit revealed: code went worker → commit with no separate verifier pass, which is how BLOCKER B1 (data-egress consent gap) slipped through. See `docs/audits/v1.7.0-audit-2026-05-17.md` §10 for the retrospective.

## MCP (Optional)

If you configured the MCP server, Claude can read and write vault notes directly.
See `skills/wiki/references/mcp-setup.md` for setup instructions.

## Release Blog Post

After cutting a new release (git tag + `gh release create`), run:

```
/release-blog
```

This generates a blog post on https://agricidaniel.com/blog/, handles cover image generation, SEO metadata, FAQ schema, internal linking, sitemap/llms.txt updates, Vercel deployment, and Google indexing.
