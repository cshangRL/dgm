# 06 — Security review and fixes

Scope: host orchestration (`DGM_outer.py`, `self_improve_step.py`), container
launch/copy helpers (`utils/`, `swe_bench/`, `polyglot/`), prompts, and the
agent tools. The threat model is an agent with arbitrary shell access inside a
container plus untrusted dataset fields and container output crossing into the
host.

## Summary

| # | Severity | Finding | Location | Status | Fix / residual |
|---|---|---|---|---|---|
| 1 | High | `tar.extractall` on Docker archives; BSD `stat -f` made the safe branch dead. Container symlinks became host symlinks. | `utils/docker_utils.py`, `swe_bench/utils.py` | **Fixed** | `stat -c %F`; tar members validated (no links, no absolute/`..`, size/member caps); only the expected regular file or directory is extracted via `utils/security_utils.py`. Verified with a container symlink to `/etc/passwd`. |
| 2 | High | Agent container ran as root with default capabilities, no resource limits, unrestricted network. | `utils/docker_utils.py:build_dgm_container`, `Dockerfile` | **Mitigated** | `cap_drop=["ALL"]`, `no-new-privileges`, `mem_limit`, `pids_limit`, `nano_cpus`. Agent is still root and network is not isolated because it must reach the local LLM relay. |
| 3 | High | Dataset `instance_id` used unvalidated in host paths. | `swe_bench/harness.py`, `polyglot/harness.py` | **Fixed** | `validate_instance_id` requires `name__name` with `[A-Za-z0-9_.-]`, rejects `..`/absolute; paths are built from the validated value. |
| 4 | High | Test-patch sanitizer missed root `conftest.py`/`pytest.ini`/`setup.cfg`/`pyproject.toml` and trusted the `diff --git` header. | `swe_bench/report.py:remove_patches_to_tests` | **Fixed** | Drops the whole diff section if the header **or** `---`/`+++` path is a test directory, `test_*` file, or test-runner config. Header-spoof test passes. |
| 5 | High | Dependencies and the SWE-bench dataset are unpinned. | `requirements.txt`, `requirements_dev.txt`, `swe_bench/harness.py` | **Residual** | A hash-pinned lockfile and a dataset `revision=` are still needed; no lockfile was generated in this change. |
| 6 | Medium | Host LLM key forwarded into untrusted containers and logged; endpoint printed with credentials. | `utils/common_utils.py`, `llm.py`, `tools/bash.py` | **Fixed / mitigated** | Containers get `LOCAL_LLM_API_KEY="local"` unless `LOCAL_LLM_API_KEY_CONTAINER` is set explicitly; logs redact URL userinfo/query; container logs are capped. |
| 7 | Medium | Unbounded `b''.join`, `extractall`, `cat`, and log writes could exhaust host memory/disk. | `utils/docker_utils.py`, `swe_bench/utils.py`, harnesses | **Fixed** | Archive, patch, log, and tar-member caps; `truncate_bytes`; `head`-free size checks via `stat`; streaming log cap. |
| 8 | Medium | Agent-controlled logs and the private `test_patch` share the diagnosis prompt; diagnosis output becomes the next problem statement. | `prompts/self_improvement_prompt.py`, `prompts/diagnose_improvement_prompt.py`, `self_improve_step.py` | **Mitigated** | Explicit untrusted-data warning; diagnosis response is rejected and retried if it copies meaningful private-test lines. Prompt-level injection is still possible. |
| 9 | Medium | `os.system(f"cp -r ...")` with operator-controlled output paths. | `DGM_outer.py`, `self_improve_step.py` | **Fixed** | Replaced with `shutil.copytree` (`dirs_exist_ok=True` where merging is expected). |
| 10 | Medium | `COPY . .` baked `.git`, docs, and run artifacts into the agent image. | `Dockerfile`, `.dockerignore` | **Fixed** | Added `.git/`, `doc/`, `output_test/`, `final_results/`, logs, chat histories, and metadata to `.dockerignore`. |
| 11 | Medium | `cleanup_container` killed a host PID reported by Docker after `stop()` failure (PID-reuse race). | `polyglot/docker_utils.py` | **Fixed** | Uses `container.kill()` and `container.remove(force=True)`; never `os.kill` on a daemon-reported host PID. |
| 12 | Low | `write_to_container` heredoc injection. | `polyglot/docker_utils.py` | **Fixed** | Uploads a tar via `put_archive`; data containing the old delimiter is inert. Verified. |
| 13 | Low | `chmod -R 777` on the testbed. | `polyglot/test_spec.py` | **Fixed** | `chmod -R u+rwX,go+rX`; setup paths are shell-quoted. |
| 14 | Low | `run_evals_cmd.split()` plus thread-global `os.chdir`. | `swe_bench/report.py:run_evals` | **Fixed** | Explicit argv list and `subprocess.run(..., cwd=output_dir)`; no `os.chdir`. |
| 15 | Low | Dataset fields interpolated into container shell commands. | `polyglot/harness.py`, `polyglot/test_spec.py` | **Fixed** | `shlex.quote` for git paths/commits; requirements and test patches are base64-encoded instead of heredocs; `test_commit` is validated. |
| 16 | Low | Editor tool had no path confinement. | `tools/edit.py` | **Fixed** | Resolves symlinks and confines reads/writes to `/testbed` and `/dgm`. Bash remains arbitrary by design. |
| 17 | Low | Host git helpers `apply_patch`/`reset_to_commit` are unsafe if wired to agent input. | `utils/git_utils.py` | **Residual** | Not reachable from the host loop today; do not pass agent patches/commits to these helpers. |
| 18 | Low | SWE-bench eval containers run as root. | `swe_bench/SWE-bench/.../docker_build.py`, `.../docker_utils.py`, `.../test_spec.py` | **Mitigated locally** | Added `no-new-privileges`, `mem_limit`, `pids_limit`; replaced the host-PID kill with `container.kill()`; replaced `chmod -R 777` with `u+rwX,go+rX`; quoted dataset paths. The vendored clone is gitignored, so these local edits are not in the tracked commit. |

## Tunables

| Variable | Default | Applies to |
|---|---|---|
| `DGM_MAX_ARCHIVE_BYTES` | `67108864` (64 MiB) | Docker archive buffers and uncompressed tar extraction |
| `DGM_MAX_PATCH_BYTES` | `16777216` (16 MiB) | `model_patch.diff` / proposed patches |
| `DGM_MAX_LOG_BYTES` | `1048576` (1 MiB) | container output, `find` listings, eval logs |
| `DGM_MAX_TAR_MEMBERS` | `10000` | tar member count |
| `DGM_CONTAINER_MEM_LIMIT` | `8g` | agent container memory |
| `DGM_CONTAINER_PIDS_LIMIT` | `512` | agent container PID cap |
| `DGM_CONTAINER_CPUS` | `4` | agent container CPU cap |
| `DGM_EVAL_MEM_LIMIT` | `8g` | SWE/polyglot eval container memory |
| `DGM_EVAL_PIDS_LIMIT` | `1024` | SWE/polyglot eval container PID cap |

Raise a limit only when a legitimate artifact exceeds it; the defaults are
intended to fail closed rather than let a runaway agent exhaust the host.

## Verification performed

- `py_compile` over every changed module.
- Symlink archive rejected (`Link tar member rejected`).
- Container symlink `/dgm/model_patch.diff -> /etc/passwd` rejected; regular file and directory copies still work.
- `remove_patches_to_tests` drops root `conftest.py` and test paths; a spoofed `diff --git` header with test `---/+++` paths is dropped.
- `get_container_env` does not forward a real `LOCAL_LLM_API_KEY`; `redact_url` strips credentials.
- Heredoc delimiter payload is written literally, not executed.
- Polyglot eval script encodes the test patch as base64; no delimiter injection.

## Residual risks to schedule

1. **Dependency and dataset pinning** — generate hash-pinned lockfiles and pin
   `load_dataset(..., revision=<commit>)`; the dataset `eval_script` executes as
   root in eval containers.
2. **Network isolation** — the agent container needs the local LLM endpoint.
   A dedicated internal network plus an LLM-only proxy would remove LAN and
   cloud-metadata reachability.
3. **Non-root agent** — the image and SWE-bench testbeds assume root. Moving to
   a non-root UID requires ownership changes in `/dgm` and `/testbed`.
4. **Prompt injection** — the warning and leak guard reduce impact, but the
   diagnosis model still sees untrusted logs and private tests in one context.
   A separate diagnosis context without the test patch is the stronger fix.
5. **Vendored SWE-bench clone** — `swe_bench/SWE-bench` is gitignored; local
   hardening there must be reapplied if the clone is recreated.
