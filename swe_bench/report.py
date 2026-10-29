import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def load_predictions(paths):
    prediction_paths = []
    for path in paths:
        path = Path(path)
        if path.is_file():
            prediction_paths.append(path)
        elif path.is_dir():
            prediction_paths += list(path.glob("*.json"))
        else:
            assert False, path

    predictions = dict()
    for fname in prediction_paths:
        try:
            pred = json.loads(fname.read_text())
        except json.decoder.JSONDecodeError as err:
            raise err

        if "instance_id" not in pred:
            print("Skipping json without instance_id", fname)
            continue

        inst = pred["instance_id"]
        pred["json_fname"] = str(fname)
        predictions[inst] = pred

    return predictions

TEST_DIR_NAMES = {"test", "tests", "testing"}
TEST_INFRA_FILES = {
    "conftest.py",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    "pyproject.toml",
    "noxfile.py",
}


def _strip_diff_prefix(path: str) -> str:
    path = path.strip().split("\t", 1)[0]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    if path.startswith("./"):
        path = path[2:]
    return path


def _is_test_path(path: str) -> bool:
    """True for test directories, test files, and test-runner configuration."""
    path = _strip_diff_prefix(path)
    if path == "/dev/null":
        return False
    parts = Path(path).parts
    if not parts:
        return False
    if any(part in TEST_DIR_NAMES for part in parts[:-1]):
        return True
    if any(part.startswith("test_") for part in parts[:-1]):
        return True
    basename = parts[-1]
    if basename in TEST_INFRA_FILES:
        return True
    if basename.startswith("test_"):
        return True
    return False


def _diff_section_paths(section):
    paths = []
    for line in section:
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line.rstrip("\n"))
            if match:
                paths.extend([match.group(1), match.group(2)])
        elif line.startswith("--- ") or line.startswith("+++ "):
            paths.append(line[4:])
    return paths


def remove_patches_to_tests(model_patch):
    """
    Remove any changes to tests or test-runner configuration from the patch.

    The previous version only looked at the `diff --git` header and only
    matched a handful of directory names, so a root `conftest.py`,
    `pytest.ini`, `setup.cfg`, or `pyproject.toml` could survive and force
    private tests to pass. This version inspects both the header and the
    `---`/`+++` paths and drops the entire diff section if any path is test
    infrastructure.
    """
    lines = model_patch.splitlines(keepends=True)
    preamble = []
    sections = []
    current = None

    for line in lines:
        if line.startswith("diff --git "):
            if current is not None:
                sections.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
        else:
            preamble.append(line)
    if current is not None:
        sections.append(current)

    filtered_lines = list(preamble)
    for section in sections:
        if not any(_is_test_path(path) for path in _diff_section_paths(section)):
            filtered_lines.extend(section)

    return "".join(filtered_lines)

def preds_to_jsonl(dname, predictions):
    dname = Path(dname)

    predictions_jsonl = str(dname / "all_preds.jsonl")
    model_name_or_path = list(predictions.values())[0]["model_name_or_path"]
    with open(predictions_jsonl, "w") as fh:
        for inst, pred in predictions.items():
            assert model_name_or_path == pred["model_name_or_path"]
            minimal_pred = dict(
                model_name_or_path=model_name_or_path,
                model_patch=remove_patches_to_tests(pred["model_patch"]),
                instance_id=pred["instance_id"],
            )
            fh.write(json.dumps(minimal_pred) + "\n")
    return predictions_jsonl

def run_evals(predictions_jsonl, run_id, dataset_name, root_dir, output_dir, num_eval_procs=5):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    predictions_jsonl = str(Path(predictions_jsonl).resolve())
    evaluation_script = Path(root_dir).resolve() / "swe_bench" / "SWE-bench" / "swebench" / "harness" / "run_evaluation.py"
    argv = [
        sys.executable,
        str(evaluation_script),
        "--dataset_name", dataset_name,
        "--predictions_path", predictions_jsonl,
        "--max_workers", str(num_eval_procs),
        "--run_id", run_id,
    ]
    subprocess.run(argv, check=True, cwd=str(output_dir))

def make_report(
        dnames,
        run_ids=None,
        dataset_name="princeton-nlp/SWE-bench_Verified",
        output_dir='./swe_bench',
        dnames_workers=None,
        num_eval_procs=5,
    ):
    """
    Generate reports for multiple directories in parallel.

    Args:
        dnames (list): List of directory names containing predictions.
        run_ids (list): Identifiers for the runs.
        dataset_name (str): Name of the dataset.
    """
    root_dir = os.path.abspath(os.getcwd())  # current working directory should be /dgm
    output_dir = os.path.join(root_dir, output_dir)

    def process_single_dname(dname, run_id):
        dname = Path(os.path.join(root_dir, dname))
        predictions = load_predictions([dname])
        predictions_jsonl = preds_to_jsonl(dname, predictions)
        run_evals(predictions_jsonl, run_id, dataset_name, root_dir, output_dir, num_eval_procs=num_eval_procs)
        print(f"Report generated for {dname}")
    
    # Use ThreadPoolExecutor to parallelize over dnames
    if run_ids is None or len(run_ids) != len(dnames):
        run_ids = [f"{i:03}" for i in range(len(dnames))]
    if dnames_workers is None:
        dnames_workers = len(dnames)
    with ThreadPoolExecutor(max_workers=dnames_workers) as executor:
        executor.map(process_single_dname, dnames, run_ids)

    print("All reports generated.")

def main():
    parser = argparse.ArgumentParser(description="Run evaluations on predictions.")
    parser.add_argument('--dnames', type=str, nargs='+', help="List of directories of predictions to evaluate.")
    parser.add_argument('--run_ids', type=str, nargs='+', default=None, help="Run ID for this evaluation run.")
    parser.add_argument('--dataset_name', type=str, default="princeton-nlp/SWE-bench_Verified", help="Name of the dataset to evaluate on.")
    parser.add_argument('--dnames_workers', type=int, default=None, help="Number of parallel workers to use for processing dnames.")
    parser.add_argument('--num_eval_procs', type=int, default=5, help="Number of parallel processes to use for evaluation.")
    parser.add_argument('--output_dir', type=str, default='./swe_bench', help="Output directory for the reports.")
    args = parser.parse_args()

    make_report(
        args.dnames,
        run_ids=args.run_ids,
        dataset_name=args.dataset_name,
        dnames_workers=args.dnames_workers,
        num_eval_procs=args.num_eval_procs,
        output_dir=args.output_dir,
    )

if __name__ == "__main__":
    main()
