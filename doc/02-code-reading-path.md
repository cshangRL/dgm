# 02 — Code reading path

Read in five passes. Each pass ends with a checkpoint question; if you cannot answer it, you read too fast.

## Pass 0 — Orientation (5 min)

1. `README.md` — setup, file-structure list, the warning about executing model-generated code.
2. `Dockerfile` — one image: `python:3.10-slim`, `COPY . .`, `WORKDIR /dgm`, `CMD tail -f`. This tells you the agent's whole world is `/dgm`.
3. `requirements.txt`, `pytest.ini` — the agent container installs these; `pytest.ini` restricts tests to `tests/`.
4. Skim `doc/01-architecture.md`.

**Checkpoint:** which two directories are the agent allowed to evolve, and which process runs the benchmark?

## Pass 1 — The evolutionary loop (30 min)

Read top-down, entry point first.

1. `DGM_outer.py:main` — argparse defaults are the experiment configuration. Note `--polyglot`, `--shallow_eval`, `--run_baseline`, `--continue_from`.
2. `DGM_outer.py:initialize_run` — archive bootstrap and the `initial/` copy.
3. `DGM_outer.py:choose_selfimproves` — this is the *selection policy*. Read the four branches; note the special entries `solve_empty_patches` / `solve_stochasticity` / `solve_contextlength` are SWE-only.
4. `DGM_outer.py:filter_compiled`, `update_archive`, `get_full_eval_threshold` — the survival rule.
5. `self_improve_step.py:self_improve` — read it as a sequence of side effects, not as logic. Make a table: *step → container command → host artifact*.
6. `self_improve_step.py:run_harness_swe` — note it calls three things: `harness` (produce patches), `make_report` (grade), `get_all_performance` (aggregate), then optionally repeats on the medium subset.

**Checkpoint:** given `archive = ['initial', 'A']` and generation 3, exactly which patches are applied before the agent starts editing, and in what order?

Answer: `get_model_patch_paths` returns `[A/model_patch.diff]` (root→parent); `self_improve` applies them in that order, commits, runs the agent, appends the new `model_patch.diff`, then evaluates with the full list.

## Pass 2 — The agent under evolution (30 min)

1. `coding_agent.py:AgenticSystem` — constructor, `forward()`, `main()`. `code_model` is hard-coded to `'local'`; `forward()` is a single `chat_with_agent` call.
2. `llm_withtools.py:chat_with_agent` — the only tool loop: builds the Chat Completions tool schemas, sends the request, processes **all** `tool_calls` in a turn, appends `role: tool` results, and repeats until the model stops calling tools.
3. `llm_withtools.py:get_response_withtools` — the single request helper, with `backoff` retries and a 3-attempt recursion.
4. `llm_withtools.py:convert_tool_info` — wraps each `tools/*.py` schema as `{type: function, function: {…}}`.
5. `llm.py:create_client` — only accepts `local` / `local/<served-id>`; builds a Chat Completions client for `LOCAL_LLM_BASE_URL`.
6. `llm.py:local_request_options` — `max_tokens` and the `chat_template_kwargs={"enable_thinking": false}` switch.
7. `llm.py:get_response_from_llm` — the non-tool single-shot call used by diagnosis.
8. `llm.py:extract_json_between_markers` — every structured response goes through this; it returns `None` on failure and callers retry.
9. `tools/__init__.py:load_all_tools` — tools are *discovered*, not registered: any `tools/*.py` exposing `tool_info()` and `tool_function` becomes available.
10. `tools/bash.py`, `tools/edit.py` — read `tool_info()` (the schema the model sees) then `tool_function`.

**Checkpoint:** if the local model returns a malformed tool call, where does the error surface?

Answer: `chat_with_agent` logs `Error in chat_with_agent: …` into the thread logger, returns a truncated history, and the attempt ends with an empty/short `self_evo.md` and an empty `model_patch.diff`, which `self_improve` then rejects.

## Pass 3 — The benchmark (25 min)

1. `swe_bench/harness.py:process_entry` — the per-task lifecycle: build env image, copy `/dgm` code, run `eval.sh` (dependency setup), apply patches, run the agent on `/testbed` (9 h timeout), collect `<instance>.json`.
2. `swe_bench/harness.py:harness` — `num_evals` outer loop, `pred_dname/<model>_<i>/`.
3. `swe_bench/report.py:make_report` — `load_predictions` → `preds_to_jsonl` (drops patches that touch test files) → `run_evals` (subprocess into the cloned SWE-bench repo, `chdir` into output dir).
4. `swe_bench/utils.py` — Docker helpers, duplicate of `utils/docker_utils.py`. Read one, skim the other.
5. `utils/evo_utils.py` — `get_model_patch_paths`, `get_all_performance`, `is_compiled_self_improve`, `load_dgm_metadata`. These four define the data model.
6. `utils/git_utils.py` — `diff_versus_commit` (includes untracked files), `apply_patch`, `filter_patch_by_files`, `remove_patch_by_files`.

**Checkpoint:** where does `accuracy_score` come from, and why does it cover 60 tasks when the small subset has 10?

Answer: `get_all_performance` sums `resolved_instances`/`submitted_instances` from the top-level report JSONs; a passing node triggers a medium run and `metadata['overall_performance']` is overwritten with 10+50 = 60.

## Pass 4 — Polyglot and analysis (optional, 20 min)

1. `polyglot/harness.py:process_entry` — compare against the SWE version: no official report step; the harness itself runs `TEST_COMMANDS[language]` and maps exit code to `resolved`/`unresolved`, empty patch to `empty_patch`.
2. `polyglot/harness.py:harness` — strips `polyglot`-named diffs from ancestor patches *in place* (mutates archived files), refuses `num_evals > 1`, writes the report itself.
3. `polyglot/prepare_polyglot_dataset.py` — builds `polyglot_benchmark_metadata.json` from a cloned `polyglot-benchmark` repo; `instance_id = "<language>__<task>"`.
4. `polyglot/constants.py`, `test_spec.py`, `docker_build.py`, `docker_utils.py`, `run_evaluation.py` — SWE-bench infrastructure adapted for multi-language images. Skim; do not try to hold it in memory.
5. `analysis/visualize_archive.py` — reads `dgm_metadata.jsonl` + per-node `metadata.json`, builds a networkx graph, emits Plotly HTML. `plot_progress.py` and `plot_comparison.py` are thin matplotlib wrappers over the same data.

## Pass 5 — Prompts (20 min)

Prompts are code here; they define the mutation operator.

1. `prompts/self_improvement_prompt.py` — `get_diagnose_prompt_swe` / `get_diagnose_prompt_polyglot` select the diagnosis template, embed the current agent code (`get_current_code`) and ancestor patches, and format the failure logs (`find_selfimprove_eval_logs` → `process_selfimprove_eval_logs`).
2. `prompts/diagnose_improvement_prompt.py` — post-hoc scoring of a patch (impact/improvements/regressions/score ∈ [−2, 2]).
3. `prompts/testrepo_prompt.py` — `get_test_description`: how the agent is told to run tests (`/testbed` for SWE, `/dgm` pytest for self-improvement).

## Suggested exercise: trace one failing task

Pick `django__django-10999` (it is in `swe_bench/subsets/small.json` and in `initial/metadata.json`'s unresolved list).

1. `initial/predictions/initial_0/django__django-10999.md` — the agent log.
2. `initial/predictions/initial_0/django__django-10999.json` — the patch it produced.
3. `initial/logs/run_evaluation/initial_0/initial_0/django__django-10999/report.json` — per-test pass/fail (`FAIL_TO_PASS`, `PASS_TO_PASS`).
4. `prompts/self_improvement_prompt.get_eval_log_text` — how that report is rendered for the diagnosis model.
5. `choose_selfimproves` — how this id becomes an `entry`.

Then answer: what text does the diagnosis model see, and what does `coding_agent.py` receive as `problem_statement`?

## Reading heuristics for this repo

- **Follow the file, not the call.** Almost every handoff is a file on disk; grep for the filename instead of chasing imports.
- **Read `tool_info()` before `tool_function`.** The schema is what the model optimizes against; the implementation is usually a thin wrapper.
- **Ignore `except Exception: pass` bodies on first read**, but list them. They are the failure surface.
- **Three copies of Docker helpers** (`utils/docker_utils.py`, `swe_bench/utils.py`, `polyglot/docker_utils.py`). Pick the one used by the module you are reading; do not assume they are identical.
- **Two copies of the agent** (`coding_agent.py`, `coding_agent_polyglot.py`). They differ in the test command table, not in structure.
