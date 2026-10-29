# 04 — Running and debugging

## Prerequisites

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt          # datasets, openai, backoff, docker, GitPython, pytest…
pip install -r requirements_dev.txt      # matplotlib, networkx, plotly, graphviz (analysis only)
```

```bash
# Docker must be usable without sudo
docker run hello-world
```

```bash
# SWE-bench is a separate clone, pinned to a specific commit
cd swe_bench
git clone https://github.com/princeton-nlp/SWE-bench.git
cd SWE-bench && git checkout dc4c087c2b9e4cefebf2e3d201d27e36 && pip install -e .
cd ../..

# Polyglot benchmark data (needs a configured git identity)
python -m polyglot.prepare_polyglot_dataset
```

Environment variables (the container gets a subset of these forwarded by `self_improve` and the harnesses):

| Variable | Used by |
|---|---|
| `LOCAL_LLM_BASE_URL` | Host and containers; default `http://mydgx0:8888/v1` |
| `LOCAL_LLM_API_KEY` | Host only; default `local` |
| `LOCAL_LLM_API_KEY_CONTAINER` | Host only; forwarded to containers as `LOCAL_LLM_API_KEY`; defaults to `local` |
| `LOCAL_LLM_MODEL` | Host and containers; served model id, default `DeepSeek-V4.1-Flash-1017` |
| `LOCAL_LLM_MAX_TOKENS` | Host and containers; output cap, default `8192` |
| `LOCAL_LLM_DISABLE_THINKING` | Host and containers; default `1` (prevents reasoning-only empty responses) |
| `LOCAL_LLM_BASE_URL_CONTAINER` | Host only; forwarded to containers as `LOCAL_LLM_BASE_URL` (Docker Desktop relay) |

See `05-local-llm.md` for the endpoint and networking setup.

## Commands

```bash
# Full evolutionary run -> ./output_dgm/<timestamp>/
python DGM_outer.py

# Common variants
python DGM_outer.py --max_generation 10 --selfimprove_size 2 --selfimprove_workers 2
python DGM_outer.py --shallow_eval              # 10 tasks only, no medium follow-up
python DGM_outer.py --no_full_eval              # (currently a no-op, see traps)
python DGM_outer.py --post_improve_diagnose     # broken, see traps
python DGM_outer.py --continue_from output_dgm/<run_id>
python DGM_outer.py --polyglot                  # polyglot benchmark instead of SWE-bench
python DGM_outer.py --run_baseline no_darwin    # always branch from the newest node
python DGM_outer.py --run_baseline no_selfimprove  # do not replay ancestor patches
```

```bash
# One mutation attempt, standalone (copies initial/ into ./output_selfimprove)
python self_improve_step.py --entry django__django-10999

# Evaluate an existing patch set on SWE-bench (small by default)
python test_swebench.py --model_patch_paths path/to/model_patch.diff --test_med

# Analyze a run (module form: analysis/ has no __init__.py, so run from the repo root)
python -m analysis.visualize_archive --path output_dgm/<run_id>
python -m analysis.plot_progress --path output_dgm/<run_id>
```

## Output layout

```
output_dgm/<run_id>/
├── dgm_outer.log                 # generation loop log
├── dgm_metadata.jsonl            # one record per generation
├── dgm_tree.html / .svg          # after visualize_archive
├── dgm_analysis.txt
├── dgm_progress.png / .svg       # after plot_progress
├── initial/                      # copied cached baseline
│   ├── metadata.json             # overall_performance (accuracy 0.2 on 60 tasks here)
│   ├── predictions/initial_0/    # <instance>.json, .md, _eval.sh, _docker.log
│   └── logs/run_evaluation/…     # per-instance report.json
└── <child_run_id>/
    ├── metadata.json             # run_id, parent_commit, entry, problem_statement,
    │                             # swe_dnames, overall_performance, model_patch_*,
    │                             # is_compiled, improvement_diagnosis
    ├── model_patch.diff          # the mutation; this is what descendants replay
    ├── self_evo.md               # agent transcript during self-improvement
    ├── self_improve.log          # container command log
    ├── predictions/<run_id>_0/   # per-task agent logs + patches
    └── logs/run_evaluation/…     # official SWE-bench reports
```

Top-level `<run_id>*.json` files in the child directory are the aggregate reports that `get_all_performance` reads; their filenames must contain the run id.

## Failure triage

Work down this list; each step narrows the stage.

| Symptom | Check | Likely cause |
|---|---|---|
| Attempt never appears in `archive` | `metadata.json` missing | Container command exited non-zero (`log_container_output` raised), or the 1.5 h future timeout fired |
| `model_patch_exists` false / `notempty` false | `self_evo.md`, `self_improve.log` | Agent produced no edit. Usually a logged exception in `chat_with_agent`, no tool call, wrong model routing, or context-length exhaustion |
| `overall_performance` missing | `predictions/` and top-level report JSON | SWE-bench clone missing/wrong commit, `make_report` subprocess failed, or Docker build of env images failed |
| `is_compiled` false | `metadata.overall_performance` | One of: required keys absent (eval failed), zero non-empty patches, or `total_submitted_instances < num_swe_issues[0]` |
| `improvement_diagnosis` null | `self_improve.log` | `diagnose_improvement` returned no parseable JSON after 3 attempts |
| Score lower than parent | Compare `metadata.overall_performance` | Evaluation noise (`--eval_noise`), or the mutation regressed the agent |
| Polyglot run hangs | `docker ps` | `num_evals > 1` raises; per-task agent timeout is 600 s and eval 120 s |
| Leaked containers | `docker ps -a | grep dgm-container` | `cleanup_container` is not in `finally` |

### Timeouts and limits

| Limit | Value | Where |
|---|---|---|
| Self-improvement agent | 1800 s | `self_improve` `timeout 1800` |
| SWE-bench task agent | 32 400 s (9 h) | `swe_bench/harness.process_entry` |
| Polyglot task agent | 600 s | `polyglot/harness.process_entry` |
| Polyglot eval command | 120 s | `polyglot/harness.process_entry` |
| Bash tool call | 120 s | `tools/bash.BashSession._timeout` |
| Future wait | 1.5 h | `DGM_outer.main` |
| LLM backoff | 600 s / 120 s | `llm_withtools` / `llm` |

### Reading a silent agent failure

1. Open `<child>/self_evo.md`. If it is empty or ends right after the user instruction, the exception happened before the first assistant message.
2. Reproduce the tool loop in isolation:

   ```bash
   python tools/bash.py "echo hello"
   python -c "from llm_withtools import chat_with_agent; print(chat_with_agent('hello'))"
   ```

3. `chat_with_agent` already logs `Error in chat_with_agent: …`; look for that line in `self_evo.md` / `self_improve.log` before editing anything.
4. `create_client` only accepts `local` or `local/<served-id>`; a different selector raises `ValueError` immediately. The agent and diagnosis models are hard-coded to `'local'`, so the only model-level configuration is `LOCAL_LLM_MODEL`.

### Reproducing an evaluation without the DGM loop

```bash
# Replay the ancestor chain manually
python - <<'PY'
from utils.evo_utils import get_model_patch_paths
print(get_model_patch_paths('.', 'output_dgm/<run>', '<child_run_id>'))
PY

python test_swebench.py \
  --model_name_or_path replay \
  --model_patch_paths p1.diff,p2.diff,p3.diff \
  --test_med
```

### Concurrency

Keep `--selfimprove_workers` at 2 unless you fix the process-global `os.chdir` in `swe_bench/report.run_evals` and `swe_bench/harness.process_entry`, and the module-global `dataset` in `self_improve`. Raising workers mainly increases Docker pressure and interleaving bugs, not throughput.

## Tests

```bash
pytest                      # tests/ only, per pytest.ini
pytest tests/test_bash_tool.py -q
pytest tests/test_edit_tool.py -q
```

These cover the two tools only; there is no test for the evolutionary loop, the harnesses or the LLM layer.

## Safety

The README's warning is accurate: `self_improve` executes model-generated code inside a container, and the agent can edit its own code and run arbitrary shell commands. Run on a disposable machine or a locked-down Docker daemon; do not mount host secrets into the `dgm` image beyond the API keys you intend to expose.

## Known broken paths (verified by reading, not by running)

- `--choose_selfimproves_method score_child_prop` and `best` are rejected by argparse (implicit string concatenation in `choices`).
- `--post_improve_diagnose` reaches `is_compiled_self_improve(metadata)` with `logger=None` and `num_swe_issues=[]`, which raises.
- `full_eval_threshold` / `--no_full_eval` have no effect: the value is computed and passed but never read in `self_improve`.
