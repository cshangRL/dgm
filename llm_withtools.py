import copy
import json

import backoff
import openai

from llm import create_client, local_model_id, local_request_options
from tools import load_all_tools


def process_tool_call(tools_dict, tool_name, tool_input):
    try:
        if tool_name in tools_dict:
            return tools_dict[tool_name]['function'](**tool_input)
        else:
            return f"Error: Tool '{tool_name}' not found"
    except Exception as e:
        return f"Error executing tool '{tool_name}': {str(e)}"


@backoff.on_exception(
    backoff.expo,
    (openai.RateLimitError, openai.APITimeoutError),
    max_time=600,
    max_value=60,
)
def get_response_withtools(
    client, model, messages, tools, tool_choice,
    logging=None, max_retry=3
):
    try:
        return client.chat.completions.create(
            model=local_model_id(model),
            messages=messages,
            temperature=0.7,
            tools=tools,
            tool_choice=tool_choice,
            **local_request_options(),
        )
    except Exception as e:
        logging(f"Error in get_response_withtools: {str(e)}")
        if max_retry > 0:
            return get_response_withtools(
                client, model, messages, tools, tool_choice, logging, max_retry - 1
            )
        raise  # Re-raise the exception after logging


def convert_tool_info(tool_info):
    """Convert a tool definition to the Chat Completions schema."""
    schema = copy.deepcopy(tool_info['input_schema'])
    schema.setdefault('additionalProperties', False)
    return {
        'type': 'function',
        'function': {
            'name': tool_info['name'],
            'description': tool_info['description'],
            'parameters': schema,
        },
    }


def chat_with_agent(
    msg,
    model='local',
    msg_history=None,
    logging=print,
):
    """Run the local LLM tool loop and return the full message history."""
    if msg_history is None:
        msg_history = []

    client, client_model = create_client(model)
    all_tools = load_all_tools(logging=logging)
    tools_dict = {tool['info']['name']: tool for tool in all_tools}
    tools = [convert_tool_info(tool['info']) for tool in all_tools]

    api_messages = []
    for previous in msg_history:
        content = previous.get("content", "")
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        api_messages.append({"role": previous.get("role", "user"), "content": text})
    api_messages.append({"role": "user", "content": msg})

    new_msg_history = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": msg,
                }
            ],
        }
    ]
    logging(f"Input: {msg}")

    try:
        response = get_response_withtools(
            client=client,
            model=client_model,
            messages=api_messages,
            tools=tools,
            tool_choice="auto",
            logging=logging,
        )

        while True:
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                break

            api_messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                    for tool_call in tool_calls
                ],
            })

            tool_use_texts = []
            tool_results = []
            for tool_call in tool_calls:
                try:
                    tool_input = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_input = {}
                tool_result = process_tool_call(
                    tools_dict, tool_call.function.name, tool_input
                )
                api_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result,
                })
                tool_use_texts.append(
                    f"<tool_use>\n{{'tool_name': {tool_call.function.name}, 'tool_input': {tool_input}}}\n</tool_use>"
                )
                tool_results.append(tool_result)
                logging(
                    f"Tool Used: {tool_call.function.name}\n"
                    f"Tool Input: {tool_input}\n"
                    f"Tool Result: {tool_result}"
                )

            new_msg_history.append({
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "\n".join(tool_use_texts),
                    }
                ],
            })
            for tool_result in tool_results:
                new_msg_history.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Tool Result: {tool_result}",
                        }
                    ],
                })

            response = get_response_withtools(
                client=client,
                model=client_model,
                messages=api_messages,
                tools=tools,
                tool_choice="auto",
                logging=logging,
            )

        final_response = response.choices[0].message.content or ""
        logging(f"Output: {final_response}")
        new_msg_history.append({
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": final_response,
                }
            ],
        })

    except Exception as e:
        logging(f"Error in chat_with_agent: {e}")

    return msg_history + new_msg_history


if __name__ == "__main__":
    # Test the tool calling functionality
    msg = "hello!"
    chat_with_agent(msg)
