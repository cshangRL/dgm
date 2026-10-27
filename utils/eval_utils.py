from utils.swe_log_parsers import MAP_REPO_TO_PARSER


def parse_eval_output(instance_id, eval_output):
    try:
        if instance_id == 'dgm':
            repo = 'dgm'
        else:
            # Convert e.g. "scikit-learn__scikit-learn-12421" to "scikit-learn/scikit-learn"
            repo = "-".join(instance_id.replace("__", "/").split("-")[:-1])

        log_parser = MAP_REPO_TO_PARSER[repo]
        # Parse the evaluation output
        return log_parser(eval_output)

    except Exception:
        return {}


def msg_history_to_report(instance_id, msg_history):
    """
    Get test report from the message history.

    The local agent history is already in the generic format:
    [{"role": ..., "content": [{"type": "text", "text": ...}]}].
    """
    for msg in reversed(msg_history):
        # Check if the message is from the user
        if msg['role'] == 'user':
            # Check if the message contains the tool result
            content = msg['content'][0]['text']
            if 'Tool Result:' in content:
                report = parse_eval_output(instance_id, content)
                # Only return the report if it is not empty
                if report:
                    return report
    return {}


def get_report_score(test_report):
    """
    Get the score from the test report.
    """
    # Percentage of passed tests
    passed_count = sum([1 for v in test_report.values() if v == 'PASSED'])
    total_count = len(test_report)
    return passed_count / total_count if total_count > 0 else 0
