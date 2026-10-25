"""Observe existing pure usage adapters without importing application/network code.

Run: python docs/research/probe_usage_accounting.py
Synthetic provider responses; not an end-to-end provider or billing test.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    source = root / "src/student_execution_os/agent/providers.py"
    data = source.read_bytes()
    tree = ast.parse(data.decode("utf-8"))
    names = {"_usage_int", "_openai_usage", "_anthropic_usage"}
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in selected} != names:
        raise RuntimeError("Usage adapter ownership changed; review this probe")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    namespace: dict = {}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)

    class Response:
        def __init__(self, value): self.value = value
        def json(self): return self.value

    anthropic_fixture = {"usage": {"input_tokens": 50, "output_tokens": 20,
        "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}}
    openai_fixture = {"usage": {"prompt_tokens": 1250, "completion_tokens": 20,
        "total_tokens": 1270, "prompt_tokens_details": {"cached_tokens": 1000}}}
    output = {
        "synthetic": True,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source": "src/student_execution_os/agent/providers.py",
        "anthropic_fixture": anthropic_fixture,
        "anthropic_observed": namespace["_anthropic_usage"](Response(anthropic_fixture)),
        "anthropic_documented_total_input": 1250,
        "openai_fixture": openai_fixture,
        "openai_observed": namespace["_openai_usage"](Response(openai_fixture)),
        "missing_usage_observed": namespace["_openai_usage"](Response({})),
        "network_requests": 0,
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SYSTEM_PROMPT" for t in node.targets):
            prompt = ast.literal_eval(node.value)
            output["system_prompt_size"] = {"characters": len(prompt), "utf8_bytes": len(prompt.encode("utf-8")), "lines": len(prompt.splitlines()), "model_tokens": None}
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
