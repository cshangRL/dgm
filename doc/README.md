# DGM code-reading guide

Docs for reading the Darwin Gödel Machine repo (`/Users/dongsheng/retry/dgm`). Read in this order:

| # | File | What it gives you | Time |
|---|------|-------------------|------|
| 1 | [`01-architecture.md`](01-architecture.md) | The loop, the container topology, the artifacts | 10 min |
| 2 | [`02-code-reading-path.md`](02-code-reading-path.md) | Exact order to open files, what to look for in each | 20 min |
| 3 | [`03-module-reference.md`](03-module-reference.md) | Per-file map: key functions, inputs, side effects, traps | reference |
| 4 | [`04-running-and-debugging.md`](04-running-and-debugging.md) | Commands, env vars, output layout, failure triage | reference |
| 5 | [`05-local-llm.md`](05-local-llm.md) | Local LLM configuration, networking, and reasoning-model caveats | when running the loop |
| 6 | [`06-security.md`](06-security.md) | Security findings, fixes applied, and residual risks | reference |

## One-paragraph mental model

`DGM_outer.py` is a host-side evolutionary loop. Each generation it picks parent nodes from an archive and a task that the parent failed, then runs `self_improve_step.self_improve` in a worker thread. That function boots the `dgm` Docker image, replays the ancestor patches into `/dgm`, asks the local LLM to turn a failure log into a GitHub-issue-style problem statement, and runs `coding_agent.py` inside the container. The coding agent (local LLM + `bash`/`editor` tools) edits `/dgm` itself, and its diff becomes `model_patch.diff`. The patched agent is then benchmarked by running it on SWE-bench tasks in *separate* per-instance containers; the resulting `accuracy_score` decides whether the node enters the archive.

## The two code bases you must not confuse

- **The agent under evolution** = `coding_agent.py` + `llm_withtools.py` + `tools/` + `prompts/` + `utils/` (patched by evolution).
- **The harness that evolves it** = `DGM_outer.py` + `self_improve_step.py` + `swe_bench/` / `polyglot/` (never patched; runs on the host).

`coding_agent.py`'s own docstring-free structure is small on purpose: one class, one `forward()`, one model call. All the interesting behavior lives in `llm_withtools.chat_with_agent` (the tool loop) and in the prompt text.

## Three facts that explain most of the code

1. **Paths are namespaced by container.** Host paths are relative to the repo; inside the agent container the repo is `/dgm`; inside an evaluation container the task repo is `/testbed`. Nearly every `os.path.join` you see is one of those three.
2. **Every artifact is a file on the host.** Nothing is passed in memory between stages: patches, chat logs, eval reports and metadata are written under `output_dgm/<run_id>/<child_run_id>/` and re-read by the next stage.
3. **The score is re-measured, never inherited.** A child's `accuracy_score` comes from running *its own patched agent* on benchmark tasks; the parent's score is only used for parent selection and archive filtering.

## Conventions used in these docs

- `file.py:function` means "open that file and find that function"; line numbers are deliberately omitted because they drift.
- **Trap** = behavior that looks wrong or surprising when you read the code and will bite you when you run it.
- **Dead** = code that exists but is not reachable from any entry point.
