# 03 — Module reference

Line counts are approximate (from `wc -l`) and are there to tell you how much to budget, not to locate code.

## Top-level map

| File / dir | ~LOC | Role | Read priority |
|---|---|---|---|
| `DGM_outer.py` | 336 | Evolutionary loop entry point; parent/task selection; archive update | 1 |
| `self_improve_step.py` | 448 | One mutation attempt: container, diagnosis, agent run, evaluation | 1 |
| `coding_agent.py` | 147 | The agent under evolution (SWE-bench) | 2 |
| `coding_agent_polyglot.py` | 184 | Same agent, multi-language | 4 |
| `llm.py` | 153 | Local endpoint client, single-shot calls, JSON extraction | 2 |
| `llm_withtools.py` | 210 | Chat Completions tool loop for the local LLM | 2 |
| `tools/` | 31 + 164 + 156 | `bash` and `editor` tools; dynamic registry | 2 |
| `utils/` | ~700 | Docker, git, evolution metadata, eval parsing | 3 |
| `swe_bench/` | ~680 | SWE-bench harness + official evaluation wrapper | 3 |
| `polyglot/` | ~3800 | Polyglot benchmark harness + vendored Aider benchmark | 4 |
| `prompts/` | ~650 | All LLM prompt templates | 5 |
| `analysis/` | ~790 | Archive visualization and progress plots | 4 |
| `tests/` | ~170 | Unit tests for the two tools | — |
| `test_swebench.py` | 67 | Standalone "evaluate a patch on SWE-bench subsets" CLI | 3 |
| `initial/`, `initial_polyglot/` | data | Cached baseline agent results + logs | — |

## Root modules

### `DGM_outer.py`

| Symbol | Contract |
|---|---|
| `initialize_run(output_dir, prevrun_dir, polyglot)` | Returns `(archive, start_gen_num)`. Copies `initial/` or `initial_polyglot/` into `<output_dir>/initial`. |
| `any_exceeding_context_length(output_dir, commit_id, instance_ids)` | True iff a log contains the context-length error string **twice** (once means the retry fixed it). |
| `choose_selfimproves(...)` | Returns `[(parent_commit, entry)]`. `entry` is an instance id or one of the three special strings. |
| `filter_compiled(run_ids, output_dir, num_swe_issues, logger)` | Keeps run ids whose `metadata.json` passes `is_compiled_self_improve`. |
| `get_original_score(output_dir)` | `initial/metadata.json → overall_performance.accuracy_score`. |
| `update_archive(...)` | `keep_all` appends; `keep_better` filters by `initial_score - noise_leeway`. |
| `get_full_eval_threshold(...)` | Second-highest score among nodes with ≥90% of the full 200-task evaluation; floor 0.4. Unused downstream. |
| `main()` | Argument parsing, generation loop, thread pool, `dgm_metadata.jsonl` append. |

Traps:

- **argparse choices bug.** `choices=['random', 'score_prop', 'score_child_prop' 'best']` is Python implicit string concatenation → `'score_child_propbest'`. The default `'score_child_prop'` still works (defaults are not validated), but passing `--choose_selfimproves_method score_child_prop` or `best` fails at parse time. The `best` branch is therefore unreachable via CLI.
- `filter_compiled` calls `load_json_file` without a guard; a run whose `metadata.json` was never written (crash/timeout) raises `FileNotFoundError` and aborts the generation.
- `future.result(timeout=1.5h)` + `future.cancel()` does not stop a running thread; timed-out attempts continue in the background.
- `--polyglot` still names the baseline node `initial` (destination path is hard-coded).

### `self_improve_step.py`

| Symbol | Contract |
|---|---|
| `diagnose_problem(entry, commit, root_dir, out_dir, patch_files, max_attempts, polyglot)` | The local model turns failure logs into JSON → `get_problem_description_prompt` → the string handed to the agent. Retries 3×. Returns `None` on total failure. |
| `diagnose_improvement(...)` | The local model compares pre/post logs and patches → `{impact, improvements, regressions, score}`. |
| `save_metadata(metadata, output_dir)` | Writes `metadata.json` (indent 4). |
| `run_harness_swe(...)` | `harness` → `make_report` → `get_all_performance`; conditionally repeats on `test_task_list_more` and overwrites `overall_performance`. |
| `run_harness_polyglot(...)` | Same shape, polyglot harness; second pass stores `overall_performance_deep`. |
| `self_improve(...)` | The whole attempt; returns `metadata`. |
| `main()` | Standalone single-attempt CLI. |

Traps:

- Module-global `dataset` is reassigned on every call; concurrent attempts race (values are equivalent, so it usually survives).
- `full_eval_threshold` is accepted and never used.
- Every container command goes through `utils.docker_utils.log_container_output`, which **raises** on non-zero exit. A timed-out/crashed agent (`timeout 1800` → 124) aborts the attempt *before* artifacts are copied and before `cleanup_container`, leaking a container and leaving no `metadata.json`.
- `cleanup_container` is not in a `finally`.
- Standalone `main()` runs `cp -r initial/ <output_dir>` unconditionally, and defaults `post_improve_diagnose=True`, which hits the `is_compiled_self_improve` bug below.
- `commit_hash = commit_output.split()[1].strip("[]")` relies on git's `[master abc1234]` format. It works because `.dockerignore` does **not** exclude `.git`, so the image has real history and the commit is not a root commit.

### `coding_agent.py`

| Symbol | Contract |
|---|---|
| `AgenticSystem.__init__` | Stores problem statement, git dir, base commit, test description; `code_model = 'local'`; truncates the chat log. |
| `AgenticSystem.forward` | One instruction + one `chat_with_agent` call. No loop, no retry, no patch selection. |
| `get_current_edits` | `diff_versus_commit` of the working tree. |
| `main()` | Runs `forward`, then writes `diff_versus_commit(base_commit)` to `<outdir>/model_patch.diff`. |

Notes:

- The agent is not told to run tests in `forward()`; the test description is only in the prompt text.
- `model_patch.diff` is written to `--outdir` (default `/dgm/`), so in the self-improve container it lands in `/dgm/model_patch.diff` and is copied out by the host.

### `coding_agent_polyglot.py`

Same shape with three differences:

1. `TEST_COMMANDS` maps `python/rust/go/javascript/cpp/java` to shell command lists.
2. `code_model = 'local'` — both self-improvement and evaluation use the local endpoint.
3. `--language` is required for evaluation; `get_current_edits` returns a message-history fragment rather than a raw string.

### `llm.py`

| Symbol | Contract |
|---|---|
| `create_client(model='local')` | Returns `(client, model)` with a Chat Completions client configured from `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_API_KEY`. Rejects anything that is not `local` / `local/<id>`. |
| `is_local_model` / `local_model_id` | Selector predicate and served-id resolution (`local` → `LOCAL_LLM_MODEL`, `local/<id>` → `<id>`). |
| `local_request_options` | `max_tokens` (`LOCAL_LLM_MAX_TOKENS`) and, unless disabled, `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`. |
| `get_response_from_llm` | Single-shot Chat Completions call with a system message; returns `(content, new_msg_history)`. |
| `extract_json_between_markers` | Parses a fenced JSON block; falls back to regex `{.*?}` and control-character cleanup. Returns `None` on failure. |

### `llm_withtools.py`

| Symbol | Contract |
|---|---|
| `get_response_withtools` | One Chat Completions request with `tools`/`tool_choice`, `backoff` on rate limits/timeouts, and a `max_retry=3` recursion. |
| `convert_tool_info` | Wraps a `tools/*.py` schema as `{type: function, function: {name, description, parameters}}`, with `additionalProperties: false`. |
| `chat_with_agent` | The tool loop: processes **every** `tool_call` in a turn, appends `role: tool` results, repeats until no tool calls remain, and returns `msg_history + new messages`. The logged history uses `<tool_use>` / `Tool Result:` text. |
| `process_tool_call` | Dispatches a parsed tool input to `tools_dict[name]['function']`; returns an error string instead of raising. |

Traps:

- `chat_with_agent` wraps the loop in `except Exception` and only **logs** the message (`Error in chat_with_agent: …`). A failure yields a short/empty history and an empty patch, not a traceback.
- Tool schemas come from `tools/*.py`; a tool module without `tool_info()`/`tool_function` makes `load_all_tools` raise and kills the agent before the first request.
- `get_response_withtools` retries the same request up to 3 times; a deterministic 400 (e.g. context overflow) costs three round trips before it re-raises.
- Tool output is stored as a **user** message starting with `Tool Result:`; history parsers rely on that prefix.

## Tools

### `tools/__init__.py`

`load_all_tools(logging)` imports every `tools/*.py` except `__init__` and requires `tool_info()` + `tool_function`. A broken tool module raises and kills the agent.

### `tools/bash.py`

- `tool_info()` — schema: one `command` string.
- `BashSession` — `asyncio.create_subprocess_shell("/bin/bash -i")`, writes `command; echo '<<exit>>'`, reads until sentinel, 120 s timeout, `filter_error` strips `Inappropriate ioctl` noise.
- `tool_function(command)` — `asyncio.run(...)` wrapper.

**Trap:** `tool_function_call` constructs a **new** `BashSession` per call, so `cd`, exported variables and shell state do **not** persist, despite the tool description claiming they do. Commands must use absolute paths or `cd x && …` in one invocation.

### `tools/edit.py`

- Commands: `view` (file with `cat -n` line numbers, or directory listing 2 levels), `create` (must not exist), `edit` (must exist, whole-file overwrite).
- `validate_path` rejects relative paths.
- `maybe_truncate` clips at 10 000 chars with `<response clipped>`.

**Trap:** `edit` is a full-file overwrite, not a patch; the model must reproduce the whole file. This is why the paper's improvements often target the editor.

## `utils/`

| File | Key symbols | Notes |
|---|---|---|
| `common_utils.py` | `read_file` (strips), `load_json_file` | Trivial; used everywhere. |
| `docker_utils.py` | thread-local `setup_logger`/`safe_log`, `remove_existing_container`, `create_archive`, `build_dgm_container`, `cleanup_container`, `copy_to_container`, `copy_from_container`, `log_container_output` | `build_dgm_container` returns `None` on failure and callers do not check. `log_container_output` raises on non-zero exit. |
| `evo_utils.py` | `load_dgm_metadata`, `get_model_patch_paths`, `get_all_performance`, `is_compiled_self_improve` | `load_dgm_metadata` splits on `'\n{'`, not a JSON parser. |
| `git_utils.py` | `get_git_commit_hash`, `apply_patch`, `diff_versus_commit`, `reset_to_commit`, `filter_patch_by_files`, `remove_patch_by_files` | `diff_versus_commit` appends `--no-index` diffs for untracked files. `apply_patch` only prints on failure, never raises. |
| `eval_utils.py` | `parse_eval_output`, `msg_history_to_report`, `get_report_score` | Legacy SWE log-parsing helpers; not called by the current agent loop. |
| `swe_log_parsers.py` | `MAP_REPO_TO_PARSER` | Per-repo pytest log parsers (django, sympy, sphinx, …). |

**`is_compiled_self_improve` trap:** the signature is `(metadata, num_swe_issues=[], logger=None)` but the body unconditionally does `logger.info(...)` and `num_swe_issues[0]`. `filter_compiled` passes both arguments; `self_improve` calls it as `is_compiled_self_improve(metadata)` for post-improvement diagnosis, which always raises — `AttributeError` when the first check fails (`logger` is `None`), `IndexError` when it succeeds (`num_swe_issues` is empty). Practically: keep `--post_improve_diagnose` off unless you fix this.

## `swe_bench/`

| File | Key symbols | Notes |
|---|---|---|
| `harness.py` | `process_entry`, `harness`, `main` | Per-task container lifecycle. `build_env_images` comes from the installed `swebench` package. Agent timeout 9 h. Copies `coding_agent.py`, `tools/`, `utils/`, `tests/`, `prompts/`, `llm*.py`, `pytest.ini`. |
| `report.py` | `load_predictions`, `remove_patches_to_tests`, `preds_to_jsonl`, `run_evals`, `make_report` | Shells out to `swe_bench/SWE-bench/swebench/harness/run_evaluation.py`. `remove_patches_to_tests` drops any diff touching `test/`, `testing/`, `test_`, `tox.ini`. |
| `utils.py` | Docker + logging helpers | Duplicate of `utils/docker_utils.py`; used by `harness.py`. |
| `subsets/` | `small.json` (10), `medium.json` (50), `big.json` (140) | Task ids only. |
| `ref_agent_results/` | Reference results | — |

`make_report` chdirs into `output_dir` before running the official evaluator, so report files land in the run directory. `get_all_performance` then scans **top-level** `*.json` in that directory whose filename contains the run id.

## `polyglot/`

| File | Role |
|---|---|
| `harness.py` | Polyglot evaluation: per-task container, agent run (`timeout 600`), `TEST_COMMANDS[language]`, exit code → resolved/unresolved/empty_patch; writes the report itself. |
| `prepare_polyglot_dataset.py` | Builds `polyglot_benchmark_metadata.json` from a cloned `polyglot-benchmark` repo (instance ids `lang__task`, base/test commits, files, docs → problem statement). |
| `test_spec.py` | SWE-bench-style `TestSpec` for multi-language tasks. |
| `docker_build.py`, `docker_utils.py`, `constants.py` | Image build/run infrastructure and log dirs (`logs/build_images/…`, `logs/run_evaluation/…`). |
| `run_evaluation.py` | Adapted SWE-bench evaluation driver. |
| `dockerfiles.py` | Generated Dockerfiles per language/task. |
| `subsets/` | `small.json` (10), `medium.json` (50). |

Traps:

- `harness` rewrites ancestor `model_patch.diff` files **in place** (`remove_patch_by_files`) to strip diffs whose filenames contain `polyglot`. This mutates archived artifacts.
- `num_evals > 1` raises.
- Polyglot self-improvement deletes `utils/eval_utils.py` and `utils/swe_log_parsers.py` from the container before running the agent.

## `prompts/`

| File | Key symbols |
|---|---|
| `self_improvement_prompt.py` | `get_diagnose_prompt_swe`, `get_diagnose_prompt_polyglot`, `get_problem_description_prompt`, `find_selfimprove_eval_logs`, `process_selfimprove_eval_logs`, `get_eval_log_text`, `get_current_code`; templates `diagnose_prompt`, `diagnose_prompt_emptypatches`, `diagnose_prompt_stochasticity`, `diagnose_prompt_contextlength`, `coding_agent_summary`, `coding_agent_summary_polyglot`. |
| `diagnose_improvement_prompt.py` | `get_diagnose_improvement_prompt`; system/prompt templates and the `score ∈ [-2, 2]` schema. |
| `testrepo_prompt.py` | `get_test_command` (parses the eval script), `get_test_description` (SWE `/testbed`, DGM `/dgm` pytest, polyglot raw command). |

`find_selfimprove_eval_logs` expects `predictions/<run>/<instance>.md` and `.json`; falls back to `logs/run_evaluation/<run>/<run>/<instance>/report.json`. `process_selfimprove_eval_logs` uses **only the first** log and truncates markdown at 250 000 chars.

## `analysis/`

| File | Output |
|---|---|
| `visualize_archive.py` | `dgm_tree.html` (+ `.svg`), `dgm_analysis.txt`; optional `dgm_tree_halluc.html`, `dgm_analysis_halluc.txt`. Reads `dgm_metadata.jsonl` and per-node `metadata.json`; builds a networkx graph and Plotly figure. |
| `plot_progress.py` | `dgm_progress.png` / `.svg` — best score, archive average, lineage to the final best node. |
| `plot_comparison.py` | Cross-run comparison plot; hard-codes the open-source SoTA line at 0.51. |

## Tests and standalone evaluation

- `tests/conftest.py`, `tests/test_bash_tool.py`, `tests/test_edit_tool.py` — exercise `tools.bash.tool_function` and `tools.edit.tool_function`. `pytest.ini` limits discovery to `tests/`.
- `test_swebench.py` — runs `harness` + `make_report` on small/medium/big or the full dataset. Default is the small subset.

## Traps index

| # | Where | Symptom |
|---|---|---|
| 1 | `llm_withtools.chat_with_agent` | Logged failure → empty patch, no traceback. |
| 2 | `tools.bash.tool_function_call` | New shell per call; `cd`/env do not persist. |
| 3 | `utils.docker_utils.log_container_output` | Non-zero container exit raises mid-attempt; container leaks, no `metadata.json`. |
| 4 | `utils.evo_utils.is_compiled_self_improve` | `logger=None` / empty `num_swe_issues` → `AttributeError` / `IndexError`. |
| 5 | `DGM_outer.main` argparse | `score_child_prop` / `best` rejected due to string concatenation. |
| 6 | `DGM_outer.filter_compiled` | Missing `metadata.json` aborts the generation. |
| 7 | `polyglot.harness` | Mutates ancestor patch files in place. |
| 8 | `swe_bench.report.run_evals` / `harness.process_entry` | `os.chdir` is process-global; unsafe under the thread pool. |
| 9 | `DGM_outer.get_full_eval_threshold` | Computed but never used. |
| 10 | `analysis.visualize_archive` | Writes Plotly HTML from run metadata; treat archived metadata as untrusted input. |
