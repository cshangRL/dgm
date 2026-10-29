# 05 — Local LLM setup (`http://mydgx0:8888/v1`)

The repo supports **only** a local Chat Completions endpoint. `create_client` rejects anything except `local` / `local/<served-id>`, and both agents plus the diagnosis step use the `'local'` selector.

## Configuration

```bash
export LOCAL_LLM_BASE_URL='http://mydgx0:8888/v1'
export LOCAL_LLM_API_KEY='local'                       # ignored by most local servers
export LOCAL_LLM_MODEL='DeepSeek-V4.1-Flash-1017'      # id from /v1/models

# Optional tuning (defaults shown)
export LOCAL_LLM_MAX_TOKENS='8192'
export LOCAL_LLM_DISABLE_THINKING='1'                  # 0 keeps reasoning enabled

# Only if containers cannot reach mydgx0 directly (see Networking):
export LOCAL_LLM_BASE_URL_CONTAINER='http://host.docker.internal:8899/v1'

# Only if the container endpoint requires auth (otherwise containers get "local"):
# export LOCAL_LLM_API_KEY_CONTAINER='...'
```

| Variable | Default | Consumed by | Effect |
|---|---|---|---|
| `LOCAL_LLM_BASE_URL` | `http://mydgx0:8888/v1` | host + containers | endpoint URL |
| `LOCAL_LLM_API_KEY` | `local` | host only | `api_key` for the host Chat Completions client |
| `LOCAL_LLM_API_KEY_CONTAINER` | unset → `local` | host, forwarded as `LOCAL_LLM_API_KEY` | explicit key for containers; set only if the server requires auth |
| `LOCAL_LLM_MODEL` | `DeepSeek-V4.1-Flash-1017` | host + containers | served model id used for the `local` selector |
| `LOCAL_LLM_MAX_TOKENS` | `8192` | host + containers | output cap per request |
| `LOCAL_LLM_DISABLE_THINKING` | `1` | host + containers | sends `chat_template_kwargs={"enable_thinking": false}`; prevents reasoning-only empty responses |
| `LOCAL_LLM_BASE_URL_CONTAINER` | unset | host, forwarded as `LOCAL_LLM_BASE_URL` | different URL for containers (Docker Desktop relay) |

There is no agent/diagnosis model selector anymore: `coding_agent.py`, `coding_agent_polyglot.py` and `self_improve_step.py` all set the model to `'local'`.

## How the calls flow

- `llm.create_client('local')` → a Chat Completions client pointed at `LOCAL_LLM_BASE_URL`.
- `llm.get_response_from_llm(...)` → Chat Completions with a system message (used by diagnosis).
- `llm_withtools.chat_with_agent(...)` → Chat Completions with `tools`/`tool_choice="auto"`; processes every returned `tool_call`, appends `role: tool` results, repeats until the model stops calling tools.
- `utils.common_utils.get_container_env()` forwards `LOCAL_LLM_*` into agent/eval containers, applying the `LOCAL_LLM_BASE_URL_CONTAINER` override.

## Networking: the part that usually breaks

The host can reach `mydgx0:8888`, but Docker containers may not. Verified on this machine:

```bash
# host: works
curl -sS http://mydgx0:8888/v1/models

# container: connection refused (server/firewall rejects the Docker NAT source)
docker run --rm alpine:3.20 wget -qO- --timeout=5 http://mydgx0:8888/v1/models
```

Two ways to fix it:

1. **Open the server to the Docker subnet** (preferred): bind it to `0.0.0.0` on `mydgx0` and allow the Docker bridge/NAT source range. Then `LOCAL_LLM_BASE_URL_CONTAINER` can be omitted.
2. **Relay through the Docker host.** Run a TCP forwarder on this machine and point containers at `host.docker.internal`:

   ```python
   # /tmp/local_llm_relay.py  —  python3 /tmp/local_llm_relay.py 8899 mydgx0 8888
   import asyncio, sys
   listen_port, host, port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])

   async def pipe(r, w):
       try:
           while (b := await r.read(65536)):
               w.write(b); await w.drain()
       except Exception:
           pass
       finally:
           try: w.close()
           except Exception: pass

   async def handle(cr, cw):
       try:
           sr, sw = await asyncio.open_connection(host, port)
       except Exception:
           cw.close(); return
       await asyncio.gather(pipe(cr, sw), pipe(sr, cw))

   async def main():
       server = await asyncio.start_server(handle, "0.0.0.0", listen_port)
       async with server:
           await server.serve_forever()

   asyncio.run(main())
   ```

   ```bash
   python3 /tmp/local_llm_relay.py 8899 mydgx0 8888 &
   export LOCAL_LLM_BASE_URL_CONTAINER='http://host.docker.internal:8899/v1'
   ```

   This exact path was verified: `docker run --rm alpine:3.20 wget -qO- http://host.docker.internal:8899/v1/models` returns the model list. Note that the host itself cannot resolve `host.docker.internal`, which is why the two URLs are separate.

On Linux, `host.docker.internal` is not defined by default. Use the host's bridge address (`172.17.0.1`) with the relay, or add `--add-host=host.docker.internal:host-gateway` to the container run arguments in `utils/docker_utils.build_dgm_container` and the evaluation harnesses.

## Verification

```bash
# 1. Host single-shot + JSON extraction
python - <<'PY'
from llm import create_client, get_response_from_llm, extract_json_between_markers
client, model = create_client('local')
print(get_response_from_llm('Reply with exactly: OK', client, model, 'Be terse.', temperature=0.2)[0])
PY

# 2. Full tool loop (native function calling)
python - <<'PY'
from llm_withtools import chat_with_agent
h = chat_with_agent('Use the bash tool to run: echo local-tool-ok', model='local')
print(h[-1]['content'][0]['text'])
PY

# 3. The diagnosis path that used to fail on solve_contextlength
python - <<'PY'
from self_improve_step import diagnose_problem
ps = diagnose_problem('solve_contextlength', 'initial', '.', '.', patch_files=[])
print(type(ps).__name__, len(ps or ''))
PY

# 4. What containers will receive
python -c "from utils.common_utils import get_container_env; print(get_container_env())"
```

Expected: `OK`; the tool loop shows `Tool Result: local-tool-ok` and a final answer; the diagnosis returns a non-empty `str`; `get_container_env()` contains `LOCAL_LLM_BASE_URL` (the container override if set).

Verified on this checkout: single-shot `OK`, tool loop with a real `bash` call, and `diagnose_problem('solve_contextlength', …)` returning a 5 015-char problem statement in ~25 s.

## Caveats

- **Reasoning models return empty `content` unless thinking is disabled.** Measured with `DeepSeek-V4.1-Flash-1017`: the diagnosis prompt used 14 973 prompt tokens; with `max_tokens=8192` and thinking on, all 8 192 output tokens went to `reasoning_content` (`finish_reason=length`) and `content` was empty, so JSON extraction failed. `LOCAL_LLM_DISABLE_THINKING=1` (default) sends `chat_template_kwargs={"enable_thinking": false}`, after which the same prompt returned valid JSON in 28 s. If your server rejects that parameter, set `LOCAL_LLM_DISABLE_THINKING=0` and raise `LOCAL_LLM_MAX_TOKENS` instead.
- **Context size.** The SWE diagnosis prompt in this checkout is ~55k characters (system 33.5k + logs 22k) for `django__django-10999`; the self-improvement prompt adds the current `coding_agent.py`, `tools/` and `utils/`.
- **Throughput.** Self-improvement and evaluation containers share the endpoint; `--selfimprove_workers 2` with `--num_swe_evals 1` is a reasonable starting point.
- **Tool schema.** The server must accept `tools`/`tool_choice`; the tool loop has no manual `<tool_use>` fallback.
- **No auth.** Most local servers ignore the API key; the client still requires a non-empty one.

## What changed in the code

| File | Change |
|---|---|
| `llm.py` | Local-only: `create_client`, `is_local_model`, `local_model_id`, `local_request_options`, `get_response_from_llm`, `extract_json_between_markers`. |
| `llm_withtools.py` | Local-only Chat Completions tool loop (`chat_with_agent`, `get_response_withtools`, `convert_tool_info`, `process_tool_call`). |
| `coding_agent.py`, `coding_agent_polyglot.py` | `code_model = 'local'`; dead regression helpers removed. |
| `self_improve_step.py` | `diagnose_model = 'local'`; client/prompt preparation wrapped so a failure returns `None` instead of aborting the attempt. |
| `utils/eval_utils.py` | Legacy log-parsing helpers only; no LLM calls. |
| `utils/common_utils.py` | `get_container_env()` forwards only `LOCAL_LLM_*`. |
| `requirements.txt` | `datasets`, `backoff`, `openai` (plus the pre-existing runtime deps). |
| `prompts/tooluse_prompt.py` | Deleted (unused by the native-tool-calling loop). |
