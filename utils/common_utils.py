import json
import os


def read_file(file_path):
    """
    Read a file and return its contents as a string.
    """
    with open(file_path, 'r') as f:
        content = f.read().strip()
    return content

def load_json_file(file_path):
    """
    Load a JSON file and return its contents as a dictionary.
    """
    with open(file_path, 'r') as file:
        return json.load(file)


# Env vars forwarded from the host into agent/eval containers.
# LOCAL_LLM_API_KEY is intentionally NOT forwarded from the host; containers
# get a non-secret placeholder unless LOCAL_LLM_API_KEY_CONTAINER is set
# explicitly (for a server that actually requires auth).
CONTAINER_ENV_KEYS = [
    "LOCAL_LLM_BASE_URL",
    "LOCAL_LLM_MODEL",
    "LOCAL_LLM_MAX_TOKENS",
    "LOCAL_LLM_DISABLE_THINKING",
]


def get_container_env():
    """
    Build the environment dict passed to `docker exec_run`.

    Unset variables are omitted (Docker rejects None values). If
    LOCAL_LLM_BASE_URL_CONTAINER is set, it overrides LOCAL_LLM_BASE_URL for
    containers only — needed when the endpoint is not reachable from the
    container network (e.g. Docker Desktop on macOS requires
    http://host.docker.internal:<port>/v1).

    LOCAL_LLM_API_KEY_CONTAINER is the only way to send a real key into a
    container. Without it, containers receive the placeholder "local", so a
    host credential cannot be exfiltrated by the agent.
    """
    env_vars = {key: os.getenv(key) for key in CONTAINER_ENV_KEYS}
    container_base_url = os.getenv("LOCAL_LLM_BASE_URL_CONTAINER")
    if container_base_url:
        env_vars["LOCAL_LLM_BASE_URL"] = container_base_url
    env_vars["LOCAL_LLM_API_KEY"] = os.getenv("LOCAL_LLM_API_KEY_CONTAINER", "local")
    return {key: value for key, value in env_vars.items() if value}
