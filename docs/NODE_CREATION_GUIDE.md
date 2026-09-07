# Workflow Node Creation Guide (Production)

> **Purpose**: A production-ready, UI-first guide to ship new **workflow-builder nodes** by **adding a folder**—no internal persistence details, no deployment secrets, and no direct API references.

This guide explains how to add a new **Node** to the visual workflow builder so it can be dropped into any workflow, wired up with input/output edges, and configured from a form the UI generates for you. Nodes are added the same way AI Agents are: **by adding a folder**, this time under:

- `nodes/<category>/<node_code>/`

It is intentionally **implementation-agnostic**:

- No DB / internal platform logic
- No environment variable names or deployment secrets beyond what's needed to explain the drop-in mount pattern
- No external paths
- No direct API endpoint references (configure/connect from the UI)

> **Companion doc**: if you're building a new integration (Gmail-style, Slack-style, "call an external vendor API") rather than a workflow-graph building block, you want an **AI Agent**, not a Node — see the AI Agent Plugin Creation Guide instead. Nodes are for deterministic, no-LLM building blocks: transform this list, filter these items, split a batch, run this code, start on a schedule.

---

### Table of contents

- [What a Node is (and isn't)](#what-a-node-is-and-isnt)
- [The two kinds of "node" in this system — read this first](#1-the-two-kinds-of-node-in-this-system--read-this-first)
- [Folder contract (what must exist on disk)](#2-folder-contract-what-must-exist-on-disk)
- [The `node.json` manifest](#3-the-nodejson-manifest)
- [Create a new Node — step by step](#4-create-a-new-node--step-by-step)
- [The `execute()` contract in detail](#5-the-execute-contract-in-detail)
- [Branching: the `route` ↔ edge `condition` convention](#6-branching-the-route--edge-condition-convention)
- [Multi-pass / loop nodes (stateful across visits)](#7-multi-pass--loop-nodes-stateful-across-visits)
- [Trigger nodes (starting a workflow on an event)](#8-trigger-nodes-starting-a-workflow-on-an-event)
- [Conditional config fields (no `param_options` for nodes)](#9-conditional-config-fields-no-param_options-for-nodes)
- [Drop-in folder enablement checklist](#10-drop-in-folder-enablement-checklist)
- [Safe update rules (production)](#11-safe-update-rules-production)
- [Common causes of "node not loading"](#12-common-causes-of-node-not-loading)
- [Copy-from-real-examples](#13-copy-from-real-examples-already-in-this-repo)

### Quick glossary

- **Node**: one building block in a workflow graph — the user drags it onto the canvas, configures its fields, and wires its inputs/outputs to other nodes.
- **Node folder**: `nodes/<category>/<node_code>/` containing `node.json` + Python code.
- **`node_id`**: the node's unique identity, derived from its own location on disk — it must equal `"<category_folder>.<node_folder>"` exactly (lowercased). You don't invent this separately; it's implied by where you put the folder.
- **`category`**: a free-form grouping label (e.g. `"Data Transformation"`, `"Flow"`, `"Trigger Nodes"`, `"Code"`) used purely to group nodes in the UI's node picker. It has no effect on how the node executes.
- **`outputPorts`**: the named output(s) a node can route to. Most nodes have exactly one; a branching/loop node declares more than one and picks between them at runtime.
- **`route`**: the string a node's `execute()` can return to say "send this run's output down the edge whose `condition` matches this string" — the one mechanism every branching/multi-output node uses.

---

## What a Node is (and isn't)

A Node is a **deterministic, no-LLM** unit of work inside a workflow graph: reshape a list, filter items, deduplicate, run a small script, split into batches, or start a workflow on a schedule. It receives plain data in, does one well-defined thing, and returns plain data out (plus, optionally, a routing decision). It never calls an LLM, never streams reasoning events, and never receives `company_id`/`user_id`/auth context directly — if a node needs an external account, that's handled by a companion trigger class (see [Section 8](#8-trigger-nodes-starting-a-workflow-on-an-event)), not the node's own `execute()`.

If what you're building needs to reason over natural language, call a vendor API on behalf of a connected account, or make judgment calls about ambiguous input, you want an **AI Agent** (see the AI Agent Plugin Creation Guide), which a workflow can also call as a step — not a Node.

---

## 1) The two kinds of "node" in this system — read this first

There are two genuinely different things both called "a node," and it's important to know which one you're building before you start:

### A) Folder-based, pluggable nodes (what this guide covers)

Deterministic data/flow-control building blocks discovered from `nodes/<category>/<node_code>/node.json`, each backed by a Python class implementing one `execute()` method. **This is the extensible system** — dropping in a new folder is enough to make a new node type appear in the UI, no engine changes required. Today's real examples are all data-transformation, code-execution, batch-looping, and schedule-trigger nodes.

### B) Engine-native node kinds (NOT extensible via a folder)

A small, closed set of node behaviors are hardcoded directly into the workflow execution engine and matched either by the node's graph-level `"type"` (e.g. `trigger`, `end`, `fetch_data`, `for_each`, `condition`) or by a special-cased `tool` name on an otherwise generic action node:

- `if_else_node` — binary true/false branch
- `switch_node` — multi-way branch by rule
- `wait_node` — pause the run and resume later on a schedule
- `end_workflow_node` — mark the workflow as finished without erroring
- `fetch_data` (`ntype`) — the generic "run an AI Agent as a step" node type
- `condition` (`ntype`) — the built-in true/false evaluator
- `for_each` (`ntype`) — the built-in loop-over-a-list controller

**You cannot add a new node of these kinds by adding a folder.** If your idea is genuinely a new flavor of "branch," "pause," or "end," that requires changing the execution engine itself, not this plugin system — flag that to whoever owns `workflow_executor_service.py` rather than trying to shoehorn it into `nodes/`. Everything else — a new data transform, a new code-runner variant, a new schedule/event trigger, a new batching strategy — fits the folder-based system in this guide comfortably.

---

## 2) Folder contract (what must exist on disk)

### Operator prerequisite (so folders are actually loaded)

> **UI-first rule**: If something doesn't appear, don't debug from code first. Restart the running processes and hard-refresh the UI. Folder-based nodes only show up after reload.

Your deployment must be configured so the **node registry loader** reads the `nodes/` root. After adding or changing folders/files, **restart** the relevant running processes and then **refresh the UI**.

**Containerized/Docker deployments — external mount root:** in addition to the repo-bundled `nodes/` directory, a deployment can also mount an **external** node root outside the repo (configured per-deployment, same pattern as the AI Agent plugin system's external mount). Both roots are scanned and merged; on a `node_id` collision, the **repo-bundled node always wins** and the external one is skipped with a warning. This is the supported way to drop a new node into a running Docker deployment without rebuilding the image — copy it into the deployment's external mount, then restart/refresh as above.

### Required layout

```text
nodes/
  <category_folder>/
    <node_code>/
      node.json            ← manifest (this exact filename)
      entrypoint.py         ← default filename; defines create_node()
      <node_code>_node.py   ← your BaseNode subclass
      __init__.py            ← empty, required for Python package import
      icon.svg               ← optional but recommended
```

**Critical rule — `node_id` is derived from the path, not chosen freely:** the folder path `nodes/<category_folder>/<node_code>/` implies `node_id` must be exactly `"<category_folder>.<node_folder>"` (lowercased). Get the folder names right first; the manifest's `node_id` field must then match them exactly, or the whole node is rejected at load time.

A node folder can hold more Python modules than the minimum above (e.g. a separate module for a companion trigger class — see [Section 8](#8-trigger-nodes-starting-a-workflow-on-an-event)).

---

## 3) The `node.json` manifest

```json
{
  "node_id": "<category_folder>.<node_folder>",
  "category": "Human-Readable Category",
  "display_name": "Human Friendly Node Name",
  "description": "One sentence: what this node does.",
  "icon": "icon.svg",
  "entrypoint": "entrypoint.py",
  "config": {
    "outputPorts": [
      { "portId": "output", "portName": "Output" }
    ],
    "inputSchema": {
      "type": "object",
      "properties": {
        "someField": { "type": "string", "description": "What this configures." }
      },
      "required": ["someField"]
    },
    "requiresRawInput": false
  }
}
```

**Field reference:**

| Field | Required | Notes |
|---|---|---|
| `node_id` | yes | Must exactly equal `"<category_folder>.<node_folder>"`, lowercased. Mismatches fail validation — this is checked against the folder the manifest was actually found in, not just "some string." |
| `category` | yes | Free-form grouping label for the UI's node picker. No execution effect. |
| `display_name` | yes | Shown in the UI. |
| `description` | no | Defaults to empty string if omitted, but always write one — it's what a user sees when picking the node. |
| `icon` | no | Relative `.svg` filename in the same folder. |
| `entrypoint` | no | Defaults to `entrypoint.py`. Must point to a file that exists. |
| `config.outputPorts` | yes | Non-empty array of `{ "portId": "...", "portName": "..." }`. Almost every node has exactly one port; declare more only if the node genuinely branches or loops (see [Section 6](#6-branching-the-route--edge-condition-convention)/[7](#7-multi-pass--loop-nodes-stateful-across-visits)). |
| `config.inputSchema` | yes | A JSON Schema object (`"type": "object"` + `"properties"`) describing the node's configurable fields — this is what the UI renders as the node's settings form. |
| `config.requiresRawInput` | no | Boolean, default `false`. Set `true` when the node needs the *raw* predecessor output rather than a pre-normalized/flattened version (used by per-item nodes like a filter that needs to inspect each item individually). |

**What's deliberately *not* supported** (don't try to add these — they're not read by anything):

- No `is_active` flag — remove/rename the folder to disable a node instead.
- No `output_schema` — only `outputPorts` (routing) and `inputSchema` (config form) exist; there's no declared shape for what `execute()` returns beyond the `execute()` contract itself.
- No `param_options`/dependent-dropdown mechanism like AI Agents have — see [Section 9](#9-conditional-config-fields-no-param_options-for-nodes) for the actual way to do conditional fields here.

---

## 4) Create a new Node — step by step

### Step A: pick the category and node code

Decide the category folder (reuse an existing one — e.g. `data_transformation`, `flow`, `code`, `trigger` — unless you're introducing a genuinely new grouping) and a short, stable node code (lowercase + underscores). Together they fix your `node_id`.

### Step B: create the folder and `node.json`

```text
nodes/
  data_transformation/
    <node_code>/
      node.json
```

```json
{
  "node_id": "data_transformation.<node_code>",
  "category": "Data Transformation",
  "display_name": "Human Friendly Name",
  "description": "One sentence: what it does.",
  "icon": "icon.svg",
  "entrypoint": "entrypoint.py",
  "config": {
    "outputPorts": [{ "portId": "output", "portName": "Output" }],
    "inputSchema": {
      "type": "object",
      "properties": {
        "field": { "type": "string", "description": "..." }
      },
      "required": ["field"]
    }
  }
}
```

### Step C: implement the node class

Create `nodes/<category>/<node_code>/<node_code>_node.py`:

```python
from __future__ import annotations

from typing import Any, Dict

from services.nodes.base_node import BaseNode


class <NodeClassName>(BaseNode):
    def execute(
        self,
        node_outputs: Dict[str, Any],
        previous_output: Any,
        run_state: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        field_value = config.get("field")
        items = previous_output if isinstance(previous_output, list) else [previous_output]

        # Deterministic transform only — no LLM calls, no vendor API calls, no auth.
        transformed = [item for item in items if item is not None]

        return {"success": True, "result": transformed}
```

See [Section 5](#5-the-execute-contract-in-detail) for the full contract, including error handling and the optional `route` key.

### Step D: create `entrypoint.py`

```python
from __future__ import annotations

from .<node_code>_node import <NodeClassName>


def create_node():
    return <NodeClassName>()
```

**Rules:**

- Keep the function name **exactly** `create_node`, and keep it a plain sync function (not async).
- Whatever it returns is strictly validated: must be an instance of `BaseNode` with a callable `execute()` method, or the node fails to load.
- Use relative imports inside the node folder.

### Step E: restart and verify

Restart the relevant process, refresh the UI, and confirm the node appears in the workflow builder's node picker under the category you chose, with the fields from `inputSchema` rendered as its settings form.

---

## 5) The `execute()` contract in detail

`BaseNode.execute(...)` is a **plain synchronous method**, not an async generator and not a coroutine — unlike an AI Agent's `run()`, a node does its work and returns once. No event streaming.

**Parameters:**

- **`node_outputs`** (`Dict[str, Any]`): every previously-executed node's output *this run*, keyed by node id. Use this if your node genuinely needs to look further back than its immediate predecessor (uncommon — most nodes only need `previous_output`).
- **`previous_output`** (`Any`): the immediate predecessor node's output. This is what most nodes should actually operate on.
- **`run_state`** (`Dict[str, Any]`): a mutable dict that **persists by identity across re-visits of this same node id within the same run** — this is how a loop/batch node keeps track of "which page am I on" across iterations (see [Section 7](#7-multi-pass--loop-nodes-stateful-across-visits)). For a simple one-shot node, ignore it.
- **`config`** (`Dict[str, Any]`): this node's resolved configuration, matching the fields declared in `node.json`'s `inputSchema`.

**Return value** — a dict:

```python
{
    "success": bool,       # required
    "result": Any,         # required on success — normalized into the node's `output`/`items`
    "error": Optional[str],# set on failure; becomes the node's error message
    "route": Optional[str],# only for branching/multi-output nodes — see Section 6
    # any other keys are merged into the stored output dict as extra fields
}
```

- On `success: False`, the node's stored output becomes `{"output": None, "items": [], "error": <your message>}` — the workflow's downstream error-handling picks this up the same way it would an AI Agent step's failure. Give a clear, non-technical `error` message.
- On success, `result` is normalized to a list and stored as both `output` and `items` so downstream nodes/AI Agent steps can reference it via the same list-indexing placeholder conventions used elsewhere in the platform (e.g. referencing `items[0]` of this node's output from a later step).
- No `company_id`, `user_id`, or auth token reach `execute()` — if your node needs those, it isn't a deterministic node in this sense; consider whether it should be an AI Agent tool step instead, or (for event-driven start-of-workflow behavior) a trigger node (see [Section 8](#8-trigger-nodes-starting-a-workflow-on-an-event)).
- Never raise an unhandled exception — catch what you can and return `{"success": False, "error": "..."}` instead, so the workflow gets a clean, actionable failure rather than crashing the run.

---

## 6) Branching: the `route` ↔ edge `condition` convention

A node that needs to send its output down one of *several* outgoing edges (rather than all of them) does so with exactly one mechanism: return a **`route`** string from `execute()`, and the workflow engine follows whichever outgoing edge has a matching `condition` value.

```python
def execute(self, node_outputs, previous_output, run_state, config) -> Dict[str, Any]:
    if some_check(previous_output):
        return {"success": True, "result": previous_output, "route": "matched"}
    return {"success": True, "result": previous_output, "route": "unmatched"}
```

Declare every possible route as an entry in `node.json`'s `outputPorts` (for the UI to render as distinct connection points), and make sure the strings you return from `execute()` are exactly the port ids you declared:

```json
"outputPorts": [
  { "portId": "matched", "portName": "Matched" },
  { "portId": "unmatched", "portName": "Unmatched" }
]
```

**Rules:**

- If you omit `route` (or return `None`), the node's output flows to **every** outgoing edge — this is the default, single-output behavior and is correct for the overwhelming majority of nodes. Only add `route` logic when the node genuinely needs to pick a path.
- The matching is a plain string comparison against each outgoing edge's `condition` value — there's no fuzzy matching, so keep your route strings stable once a workflow depends on them (renaming a route string after users have wired up edges against it will silently break those workflows).
- This is the same underlying mechanism the engine's own built-in `switch_node` uses (it returns the winning rule's id, or the literal string `"fallback"` when nothing matches) — you're not inventing a new pattern, just reusing the one primitive every multi-output node in this system shares.

---

## 7) Multi-pass / loop nodes (stateful across visits)

Some nodes need to run more than once per workflow execution — e.g. "take this list, emit it 50 items at a time, and keep looping back into the same downstream steps until the whole list is processed." This is where `run_state` matters: it's handed back to you, unchanged, every time the engine re-enters this exact node id during the same run.

Pattern:

```python
def execute(self, node_outputs, previous_output, run_state, config) -> Dict[str, Any]:
    if "items" not in run_state:
        # First visit this run: initialize.
        run_state["items"] = list(previous_output or [])
        run_state["batch_size"] = int(config.get("batchSize", 10))
        run_state["offset"] = 0

    items = run_state["items"]
    offset = run_state["offset"]
    batch_size = run_state["batch_size"]
    batch = items[offset: offset + batch_size]
    run_state["offset"] = offset + batch_size

    if run_state["offset"] >= len(items):
        return {"success": True, "result": batch, "route": "done"}
    return {"success": True, "result": batch, "route": "loop"}
```

Declare both outgoing routes in `outputPorts` (`"done"` and `"loop"` in the example above), and wire the `"loop"` edge back into whatever step should run again per-batch, and the `"done"` edge into whatever should run once all batches are processed. This is exactly the shape a real batching node in this repo uses — see [Section 13](#13-copy-from-real-examples-already-in-this-repo).

---

## 8) Trigger nodes (starting a workflow on an event)

A **trigger node** starts a workflow when something happens externally (e.g. on a schedule, or on a vendor webhook) — conceptually similar to an AI Agent's trigger system, but wired in as a node folder under `nodes/trigger/<node_code>/` rather than under an agent's own folder.

A trigger node folder typically contains **two separate classes**:

1. **A `BaseNode` subclass** (e.g. `<node_code>_node.py`) — this is what runs *inside* the graph once the workflow has started, if the trigger's own output needs to be read/reshaped as a normal node step. It follows the exact same `execute()` contract as any other node.
2. **A separate trigger-agent class** (e.g. `<node_code>_trigger_agent.py`) that implements the `subscribe(...)` / `execute_trigger(...)` contract — the same pattern used by AI Agent triggers (see the AI Agent Plugin Creation Guide's triggers section). This is where `company_id`/`user_id`/auth *do* apply, because starting a workflow on an event requires the platform to register a subscription and later resolve an inbound event back to a specific workflow.

**Why the split matters:** `BaseNode.execute()` deliberately has no auth/context parameters, because the node-registry system is only for deterministic, already-have-the-data transforms. Anything that needs to *acquire* data from an external source on a schedule or event needs the richer trigger contract, so that responsibility is factored into its own class rather than bent onto `BaseNode`.

If you're building a schedule-based trigger (e.g. "start this workflow every day at 9am" or "start this workflow once at a specific time"), follow the real example in [Section 13](#13-copy-from-real-examples-already-in-this-repo) closely — it demonstrates both classes side by side in one folder.

> **Note on mid-workflow pausing:** a trigger node starts a workflow. Pausing a workflow that's *already running* (e.g. "wait 2 hours, then continue") is a different, engine-native mechanism (`wait_node` — see [Section 1](#1-the-two-kinds-of-node-in-this-system--read-this-first)) and is not something you build as a folder-based node.

---

## 9) Conditional config fields (no `param_options` for nodes)

Unlike AI Agents, **nodes have no `param_options`/dependent-dropdown mechanism** — there's no way to say "fetch live options for this field from an API based on another field's value." If you need that kind of dynamic, data-backed dropdown, the capability belongs on an AI Agent, not a node.

What nodes *do* support is standard **JSON Schema conditional validation** inside `inputSchema`, using `allOf` + `if`/`then`, to show/require different static fields depending on another field's chosen value:

```json
"inputSchema": {
  "type": "object",
  "properties": {
    "scheduleType": { "type": "string", "enum": ["One Time", "Recurring"] },
    "timeOfDay": { "type": "string" },
    "frequency": { "type": "string", "enum": ["Daily", "Weekly"] },
    "byDay": { "type": "array", "items": { "type": "string" } }
  },
  "required": ["scheduleType"],
  "allOf": [
    {
      "if": { "properties": { "scheduleType": { "const": "One Time" } } },
      "then": { "required": ["timeOfDay"] }
    },
    {
      "if": { "properties": { "scheduleType": { "const": "Recurring" } } },
      "then": { "required": ["frequency"] }
    },
    {
      "if": { "properties": { "frequency": { "const": "Weekly" } } },
      "then": { "properties": { "byDay": {} } }
    }
  ]
}
```

Use this whenever a node's settings form should reveal/require different static fields based on another field — it covers the large majority of "this field depends on that field" cases for nodes, just without the ability to populate a dropdown from a live external lookup.

---

## 10) Drop-in folder enablement checklist

### A) Add the node folder

- Copy `nodes/<category>/<node_code>/` into the target instance's `nodes/` directory (or its external mount, for a Docker deployment that doesn't rebuild the image — see [Section 2](#2-folder-contract-what-must-exist-on-disk)).

### B) Restart and verify in UI

- The node should appear in the workflow builder's node picker, grouped under its `category`.
- Its `inputSchema` fields should render as the node's settings form when added to a canvas.
- If it declares more than one `outputPorts` entry, the canvas should show that many connection points, and you should be able to wire different edges with different `condition` values to each.

### C) Wire it into a workflow and test

- Add the node between two others, configure its fields, run the workflow, and confirm the output it produces is what downstream steps expect (check the shape under `output`/`items`).
- For a branching node, test every route your `execute()` can return, and confirm each one's wired edge actually fires.
- For a loop node, confirm it correctly stops looping (returns the "done" route) once its termination condition is met — an incorrectly-terminating loop node will run forever within that execution.

---

## 11) Safe update rules (production)

- **Do not change `node_id`** after workflows have been built using this node — it breaks every existing workflow that references it. Since `node_id` is derived from the folder path, this means: don't rename the category or node-code folders of a node already in use.
- **Do not remove or rename an `outputPorts` entry's `portId`** that existing workflows already have edges wired against — those edges reference the port id, and a rename orphans them.
- **Do not change what `result` contains in a backward-incompatible way** (e.g. renaming a key downstream steps rely on) — treat this the same as changing an AI Agent's output shape: consumers and UI expectations need to move together.
- **Never call an external API or use an LLM inside `execute()`** — if the node's responsibility grows into needing either, it no longer belongs in this system; move that responsibility into an AI Agent (for LLM/vendor-API work) or a trigger-agent class (for event/schedule-driven work), and keep `execute()` itself deterministic.
- **Never let `execute()` raise** — always catch and return `{"success": False, "error": "..."}` instead, so a bug in one node fails that node cleanly rather than crashing the whole run.

---

## 12) Common causes of "node not loading"

- **Invalid JSON** in `node.json` (trailing commas, comments).
- **`node_id` doesn't match its folder path** — must be exactly `"<category_folder>.<node_folder>"`, lowercased. This is the single most common mistake when copying an existing node as a starting point and forgetting to update its manifest.
- **Missing or invalid `config.outputPorts`** — must be a non-empty array of `{portId, portName}` objects with non-empty strings.
- **Missing or invalid `config.inputSchema`** — must be an object with `"type": "object"` and a `"properties"` dict, even if that dict is empty.
- **Missing `entrypoint.py`**, or it doesn't define a plain (non-async) `create_node()` function.
- **`create_node()` doesn't return a `BaseNode`** with a callable `execute()` — the loader validates this strictly and skips the node (with a warning) if it fails.
- **Duplicate `node_id`** across two folders (including one repo-bundled and one from an external mount) — the first one loaded wins; the duplicate is skipped with a warning, so if your new node silently doesn't appear, check whether an existing node already claims that id.
- **Import errors** in your Python files — prefer relative imports inside the node folder, mirroring the AI Agent plugin convention.

---

## 13) Copy-from-real-examples (already in this repo)

If you want working reference folders to copy (with "how it works" notes), start with these:

### Simple, single-output transform nodes

- **Basic list-limiting node** — the smallest possible shape: `node.json` + one small `_node.py` + `entrypoint.py`, single `outputPorts` entry, no branching, no loop state. Best starting point for your first node.
- **Field-reshaping node** (split a list into multiple output fields) — same simple shape, but with several related, non-conditional config fields — good for seeing a slightly richer `inputSchema` that still has no `allOf`/conditional logic.

### Per-item conditional evaluation

- **Filter-style node** — sets `config.requiresRawInput: true` and reuses the same structured-conditions JSON-Schema shape (`if_else_node_conditions`) that the engine's built-in `if_else_node`/`switch_node` use, so its settings form matches the platform's condition-builder UI exactly. Good template if your node needs to evaluate a boolean expression per item.

### Configurable / code-execution node

- **Script-execution node** — an enum field switches between "run once for all items" vs "run once per item" modes, plus a `"type": "code_editor"` field for a multi-line script. Good template for any node that needs a richer, non-text-input config field.

### Branching / looping (multi-output, stateful)

- **Batch-splitting node** — declares two `outputPorts` (`done`/`loop`), uses `run_state` across repeated visits to track its position through a list, and returns `"route"` to alternate between looping back into the batch-processing branch and falling through to the completion branch once exhausted. This is the canonical template for [Section 6](#6-branching-the-route--edge-condition-convention) and [Section 7](#7-multi-pass--loop-nodes-stateful-across-visits).

### Trigger node (schedule-based)

- **Schedule-trigger node** — the only trigger-flavored node folder in the repo today. Contains a `BaseNode` subclass for in-graph use alongside a separate trigger-agent class implementing `subscribe(...)`/`execute_trigger(...)`, and a heavily conditional `inputSchema` (`allOf`/`if`/`then` switching between one-time vs. recurring schedules, and between daily/weekly recurrence fields) — the best real template for both [Section 8](#8-trigger-nodes-starting-a-workflow-on-an-event) and [Section 9](#9-conditional-config-fields-no-param_options-for-nodes).

### What has no folder-based example (because it can't — see Section 1)

- Condition/if-else branching, multi-way switch branching, wait/pause, and end-of-workflow nodes are all engine-native, not folder-based. Don't go looking for (or try to add) a `nodes/` folder for any of these — study `workflow_executor_service.py`'s handling of `if_else_node`, `switch_node`, `wait_node`, and `end_workflow_node` instead if you need to understand or change that behavior, and treat it as a different, non-pluggable system from everything else in this guide.
