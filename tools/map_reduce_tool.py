"""
Open Manus Map-Reduce Tool
Replicates the Manus.im `map` tool for parallel processing.
Spawns multiple sub-agent tasks and aggregates results into a CSV/JSON file.
"""
import json
import os
import uuid
import csv
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from tools.registry import registry

def check_requirements() -> bool:
    return True  # No external dependencies required

def map_reduce(
    name: str,
    prompt_template: str,
    inputs: list,
    output_schema: list,
    target_count: int = None,
    task_id: str = None
) -> str:
    """
    Executes a large number of homogeneous subtasks in parallel using a thread pool
    and aggregates the results into a JSON file.

    This replicates the Manus.im `map` tool behavior.

    Args:
        name: A unique name for this map operation (used for the output filename).
        prompt_template: A template string where {{input}} is replaced with each input item.
        inputs: A list of input strings to process.
        output_schema: A list of field definitions for the expected output.
        target_count: Expected number of subtasks (for validation).
        task_id: Optional task ID.

    Returns:
        JSON string with the path to the aggregated results file.
    """
    try:
        from run_agent import AIAgent

        workspace = os.path.expanduser(os.environ.get("HERMES_WORKSPACE_DIR", "~/.hermes/workspace"))
        os.makedirs(workspace, exist_ok=True)

        results = []
        results_lock = threading.Lock()
        max_workers = min(len(inputs), int(os.environ.get("MAP_REDUCE_MAX_WORKERS", "5")))
        model = os.environ.get("MAP_REDUCE_MODEL", os.environ.get("LLM_MODEL", "anthropic/claude-opus-4.6"))

        schema_description = "\n".join([
            f"- {f.get('name', f.get('title', 'field'))}: {f.get('description', '')} (type: {f.get('type', 'string')})"
            for f in output_schema
        ])

        def process_input(idx, input_item):
            prompt = prompt_template.replace("{{input}}", str(input_item))
            full_prompt = (
                f"{prompt}\n\n"
                f"IMPORTANT: Your response MUST be a valid JSON object with EXACTLY these fields:\n"
                f"{schema_description}\n"
                f"Return ONLY the JSON object, no other text."
            )
            try:
                agent = AIAgent(
                    model=model,
                    max_iterations=15,
                    quiet_mode=True,
                    skip_memory=True,
                )
                response = agent.chat(full_prompt)
                # Try to extract JSON from response
                json_match = None
                for line in response.split('\n'):
                    line = line.strip()
                    if line.startswith('{') and line.endswith('}'):
                        try:
                            json_match = json.loads(line)
                            break
                        except:
                            pass
                if not json_match:
                    # Try to find JSON block
                    import re
                    match = re.search(r'\{[^{}]+\}', response, re.DOTALL)
                    if match:
                        try:
                            json_match = json.loads(match.group())
                        except:
                            pass
                if json_match:
                    json_match['_input'] = input_item
                    json_match['_index'] = idx
                    return json_match
                else:
                    return {'_input': input_item, '_index': idx, '_error': 'Could not parse JSON response', '_raw': response[:500]}
            except Exception as e:
                return {'_input': input_item, '_index': idx, '_error': str(e)}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_input, i, inp): i for i, inp in enumerate(inputs)}
            for future in as_completed(futures):
                result = future.result()
                with results_lock:
                    results.append(result)

        # Sort by original index
        results.sort(key=lambda x: x.get('_index', 0))

        # Save to JSON
        output_file = os.path.join(workspace, f"map_results_{name}_{uuid.uuid4().hex[:8]}.json")
        with open(output_file, 'w') as f:
            json.dump(results, f, indent=2)

        # Also save to CSV if schema is provided
        if output_schema:
            csv_file = output_file.replace('.json', '.csv')
            fieldnames = ['_input'] + [f.get('name', f.get('title', f'field_{i}')) for i, f in enumerate(output_schema)]
            with open(csv_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(results)

        success_count = sum(1 for r in results if '_error' not in r)
        error_count = len(results) - success_count

        return json.dumps({
            "success": True,
            "results_file": output_file,
            "csv_file": csv_file if output_schema else None,
            "total": len(inputs),
            "completed": success_count,
            "errors": error_count,
            "message": f"Map-reduce '{name}' complete. {success_count}/{len(inputs)} tasks succeeded. Results saved to {output_file}"
        })

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        })


registry.register(
    name="map_reduce",
    toolset="productivity",
    schema={
        "name": "map_reduce",
        "description": (
            "Spawns parallel subtasks and aggregates results. Use when a step involves "
            "performing similar operations on 5 or more independent items (e.g., research "
            "100 companies, process 50 files). Each subtask receives one input from the "
            "`inputs` list, substituted into the `prompt_template` at the {{input}} placeholder. "
            "Results are aggregated into a JSON/CSV file. Returns the path to the results file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "A unique snake_case name for this operation (e.g., 'find_ceo_emails')."
                },
                "prompt_template": {
                    "type": "string",
                    "description": "The prompt for each subtask. Use {{input}} as the placeholder for each input item."
                },
                "inputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The list of input strings to process in parallel."
                },
                "output_schema": {
                    "type": "array",
                    "description": "Schema defining the expected output fields for each subtask.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string"},
                            "description": {"type": "string"}
                        }
                    }
                },
                "target_count": {
                    "type": "integer",
                    "description": "Expected number of subtasks (for validation)."
                }
            },
            "required": ["name", "prompt_template", "inputs", "output_schema"]
        }
    },
    handler=lambda args, **kw: map_reduce(
        name=args.get("name", "map_task"),
        prompt_template=args.get("prompt_template", ""),
        inputs=args.get("inputs", []),
        output_schema=args.get("output_schema", []),
        target_count=args.get("target_count"),
        task_id=kw.get("task_id")
    ),
    check_fn=check_requirements,
    requires_env=[],
)
