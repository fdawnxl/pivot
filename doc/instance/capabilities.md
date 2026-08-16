# Capability development

Capabilities are small, discoverable scripts under `capabilities/think`, `capabilities/measure`, or `capabilities/work`. Each kind has a dedicated uv project under `environment/<kind>`, so device libraries never enter pivot's interpreter.

## Shared descriptor

Every script responds to `-l` with one JSON object:

```json
{
  "name": "read_temperature",
  "description": "Read the current enclosure temperature in Celsius.",
  "parameters": {
    "type": "object",
    "properties": {
      "feature": {"type": "string", "enum": ["temperature"]}
    },
    "required": ["feature"],
    "additionalProperties": false
  }
}
```

Names must be stable and unique across all capability kinds. Descriptions should tell the model when to use the capability. Parameters use JSON Schema and should reject unsupported fields.

## Think capability

A think capability is an optional method, policy, or domain reasoning guide. It has no executable side effect. `-l` returns the summary descriptor and `-r` returns the full text as a JSON string.

```python
import json
import sys


DESCRIPTOR = {
    "name": "safe_navigation",
    "description": "Plan cautious indoor navigation for a visually impaired user.",
    "parameters": {},
}

BODY = """Prioritize immediate obstacles, describe clock directions, and ask before changing route."""


if "-l" in sys.argv:
    print(json.dumps(DESCRIPTOR))
elif "-r" in sys.argv:
    print(json.dumps(BODY))
else:
    raise SystemExit(2)
```

pivot injects only the summary at first. The model calls `pivot_read_think` to load the full body when needed.

## Measure capability

A measure capability reads one named fact. `-r <feature>` must emit any JSON value.

```python
import json
import sys


DESCRIPTOR = {
    "name": "read_environment",
    "description": "Read one current environmental sensor value.",
    "parameters": {
        "type": "object",
        "properties": {
            "feature": {
                "type": "string",
                "enum": ["temperature", "humidity"],
            }
        },
        "required": ["feature"],
        "additionalProperties": False,
    },
}


def read_feature(name: str) -> object:
    # Replace this fixture value with a dependency D-Bus call or device read.
    return {"feature": name, "value": 24.5, "unit": "celsius"}


if "-l" in sys.argv:
    print(json.dumps(DESCRIPTOR))
elif "-r" in sys.argv:
    index = sys.argv.index("-r")
    print(json.dumps(read_feature(sys.argv[index + 1])))
else:
    raise SystemExit(2)
```

Keep direct reads short. Use a dependency service when initialization is expensive, hardware ownership must be exclusive, or several scripts share one device connection.

## Work capability

A work capability receives its argument object on stdin with `-x` and emits one JSON result.

```python
import json
import sys


DESCRIPTOR = {
    "name": "set_vibration",
    "description": "Pulse the device vibration motor for a bounded duration.",
    "parameters": {
        "type": "object",
        "properties": {
            "duration_ms": {"type": "integer", "minimum": 50, "maximum": 1000}
        },
        "required": ["duration_ms"],
        "additionalProperties": False,
    },
}


if "-l" in sys.argv:
    print(json.dumps(DESCRIPTOR))
elif "-x" in sys.argv:
    arguments = json.load(sys.stdin)
    duration = int(arguments["duration_ms"])
    # Perform the bounded device operation here.
    print(json.dumps({"completed": True, "duration_ms": duration}))
else:
    raise SystemExit(2)
```

Validate arguments again inside the script. Model-provided JSON and external service responses are untrusted input.

## Multimodal results

A measure or work result may carry provider-compatible media:

```json
{
  "content": [
    {"type": "text", "text": "Front camera frame."},
    {
      "type": "image_url",
      "image_url": {"url": "data:image/jpeg;base64,..."}
    }
  ]
}
```

pivot preserves the media in memory and presents it to the next model round in a provider-compatible role. Keep payloads bounded; capabilities have a one MiB output limit by default.

## Dependencies and testing

Declare per-kind packages in the matching environment project:

```toml
[project]
name = "device-measure-environment"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["dbus-next>=0.2.3"]
```

Test each protocol before starting pivot:

```bash
uv run --project "$PIVOT_INSTANCE_PATH/environment/measure" \
  python "$PIVOT_INSTANCE_PATH/capabilities/measure/environment.py" -l

uv run --project "$PIVOT_INSTANCE_PATH/environment/measure" \
  python "$PIVOT_INSTANCE_PATH/capabilities/measure/environment.py" -r temperature
```

Scripts run with the instance as cwd and receive `PIVOT_INSTANCE_PATH`. They may also inherit D-Bus addresses. Do not depend on pivot package internals or its virtual environment.
