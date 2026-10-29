# Code adapted from https://github.com/SakanaAI/AI-Scientist/blob/main/ai_scientist/llm.py.
import json
import os
import re

import backoff
import openai

from utils.security_utils import redact_url

# The repo only supports a local LLM endpoint (Chat Completions compatible).
LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://mydgx0:8888/v1")
LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "local")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "DeepSeek-V4.1-Flash-1017")
LOCAL_LLM_MAX_TOKENS = int(os.getenv("LOCAL_LLM_MAX_TOKENS", "8192"))
# Reasoning models may spend the whole output budget on reasoning and return an
# empty `content`, which breaks every content-based parser in this repo.
# Disable thinking by default; set LOCAL_LLM_DISABLE_THINKING=0 to keep it.
LOCAL_LLM_DISABLE_THINKING = os.getenv("LOCAL_LLM_DISABLE_THINKING", "1").lower() not in ("0", "false", "no")


def is_local_model(model: str) -> bool:
    """True for the local provider: "local" or "local/<model-id>"."""
    return model == "local" or model.startswith("local/")


def local_model_id(model: str) -> str:
    """Resolve the served model id for the local provider."""
    if model == "local":
        return os.getenv("LOCAL_LLM_MODEL", LOCAL_LLM_MODEL)
    return model.split("/", 1)[1]


def local_request_options() -> dict:
    """Extra Chat Completions kwargs for the local provider."""
    options = {"max_tokens": int(os.getenv("LOCAL_LLM_MAX_TOKENS", LOCAL_LLM_MAX_TOKENS))}
    if os.getenv("LOCAL_LLM_DISABLE_THINKING", "1").lower() not in ("0", "false", "no"):
        # Honored by vLLM/SGLang-style servers; harmless elsewhere.
        options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    return options


def create_client(model: str = "local"):
    """
    Create and return the local LLM client.

    Args:
        model (str): "local" (uses LOCAL_LLM_MODEL) or "local/<served-model-id>".
    Returns:
        Tuple[Any, str]: the client and the model selector (kept with its prefix
        so downstream code can route on it).
    """
    if not is_local_model(model):
        raise ValueError(
            f"Only the local provider is supported; got model={model!r}. "
            "Use 'local' or 'local/<served-model-id>'."
        )
    base_url = os.getenv("LOCAL_LLM_BASE_URL", LOCAL_LLM_BASE_URL)
    api_key = os.getenv("LOCAL_LLM_API_KEY", LOCAL_LLM_API_KEY)
    print(f"Using local LLM endpoint {redact_url(base_url)} with model {local_model_id(model)}.")
    return openai.OpenAI(base_url=base_url, api_key=api_key), model


@backoff.on_exception(backoff.expo, (openai.RateLimitError, openai.APITimeoutError), max_time=120)
def get_response_from_llm(
        msg,
        client,
        model,
        system_message,
        print_debug=False,
        msg_history=None,
        temperature=0.7,
):
    if msg_history is None:
        msg_history = []

    new_msg_history = msg_history + [{"role": "user", "content": msg}]
    response = client.chat.completions.create(
        model=local_model_id(model),
        messages=[
            {"role": "system", "content": system_message},
            *new_msg_history,
        ],
        temperature=temperature,
        **local_request_options(),
    )
    content = response.choices[0].message.content or ""
    new_msg_history = new_msg_history + [{"role": "assistant", "content": content}]

    if print_debug:
        print()
        print("*" * 20 + " LLM START " + "*" * 20)
        print(f'User: {new_msg_history[-2]["content"]}')
        print(f'Assistant: {new_msg_history[-1]["content"]}')
        print("*" * 21 + " LLM END " + "*" * 21)
        print()
    return content, new_msg_history


def extract_json_between_markers(llm_output):
    inside_json_block = False
    json_lines = []

    # Split the output into lines and iterate
    for line in llm_output.split('\n'):
        striped_line = line.strip()

        # Check for start of JSON code block
        if striped_line.startswith("```json"):
            inside_json_block = True
            continue

        # Check for end of code block
        if inside_json_block and striped_line.startswith("```"):
            # We've reached the closing triple backticks.
            inside_json_block = False
            break

        # If we're inside the JSON block, collect the lines
        if inside_json_block:
            json_lines.append(line)

    # If we never found a JSON code block, fallback to any JSON-like content
    if not json_lines:
        # Fallback: Try a regex that finds any JSON-like object in the text
        fallback_pattern = r"\{.*?\}"
        matches = re.findall(fallback_pattern, llm_output, re.DOTALL)
        for candidate in matches:
            candidate = candidate.strip()
            if candidate:
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # Attempt to clean control characters and re-try
                    candidate_clean = re.sub(r"[\x00-\x1F\x7F]", "", candidate)
                    try:
                        return json.loads(candidate_clean)
                    except json.JSONDecodeError:
                        continue
        return None

    # Join all lines in the JSON block into a single string
    json_string = "\n".join(json_lines).strip()

    # Try to parse the collected JSON lines
    try:
        return json.loads(json_string)
    except json.JSONDecodeError:
        # Attempt to remove invalid control characters and re-parse
        json_string_clean = re.sub(r"[\x00-\x1F\x7F]", "", json_string)
        try:
            return json.loads(json_string_clean)
        except json.JSONDecodeError:
            return None
