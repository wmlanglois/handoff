"""GBNF / JSON-schema builders for grammar-constrained decoding on llama.cpp.

llama.cpp can force output to a grammar during generation, so a small model (spark-4B) becomes
*physically unable* to emit malformed JSON, drop a bracket, or hallucinate a tool argument -- the
constraint is applied token by token, not validated after. Pass the result to call.chat(grammar=...):
a dict is sent as `json_schema`, a str as a raw GBNF `grammar`.

Note: llama.cpp already derives a tool-call grammar when you pass `tools` (so the jury's tool JSON is
constrained natively). These builders are for the cases where we want an explicit, engine-independent
constraint (a fixed enum verdict, a strict tool-call schema).
"""
import json


def enum_grammar(values):
    """GBNF that forces the output to be exactly ONE of the given literal strings (e.g. ACCEPT/REDO)."""
    if not values:
        raise ValueError("enum_grammar needs at least one value")
    alts = " | ".join(json.dumps(str(v)) for v in values)   # JSON-quoted literals
    return f"root ::= {alts}"


def tool_call_schema(tools):
    """A JSON schema for a single valid tool call: {name: <one of the tool names>, arguments: object}.
    `tools` is the OpenAI-style tool list (each has function.name). Send via call.chat(grammar=<this>)
    to force llama.cpp to emit a well-formed tool call and nothing else."""
    names = [t["function"]["name"] for t in tools if t.get("function", {}).get("name")]
    if not names:
        raise ValueError("tool_call_schema needs tools with function.name")
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": names},
            "arguments": {"type": "object"},
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
    }


QUESTION_ANGLES = ["TECHNICAL", "BIG PICTURE", "IMPROVEMENT"]
VERDICT_TAGS = ["SUPPORTED", "OVERCLAIMING", "HALLUCINATING", "UNVERIFIABLE"]


def three_questions_schema():
    """Force the jury's output to be exactly three angled questions -- valid JSON, no free-form drift."""
    return {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 3, "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "angle": {"type": "string", "enum": QUESTION_ANGLES},
                        "question": {"type": "string"},
                    },
                    "required": ["angle", "question"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    }


def verdict_schema():
    """Force the jury's REVIEW verdict to a tag + note."""
    return {
        "type": "object",
        "properties": {
            "tag": {"type": "string", "enum": VERDICT_TAGS},
            "note": {"type": "string"},
        },
        "required": ["tag", "note"],
        "additionalProperties": False,
    }


def challenges_schema():
    """Force the CHALLENGE mode to 2-3 grounded challenge strings."""
    return {
        "type": "object",
        "properties": {"challenges": {"type": "array", "minItems": 1, "maxItems": 3,
                                      "items": {"type": "string"}}},
        "required": ["challenges"],
        "additionalProperties": False,
    }


def json_object_grammar():
    """A minimal GBNF for 'any JSON object' -- a safety net when you only need valid-JSON, not a schema."""
    return (
        'root   ::= object\n'
        'object ::= "{" ws (pair ("," ws pair)*)? ws "}"\n'
        'pair   ::= string ws ":" ws value\n'
        'value  ::= object | array | string | number | "true" | "false" | "null"\n'
        'array  ::= "[" ws (value ("," ws value)*)? ws "]"\n'
        'string ::= "\\"" ([^"\\\\] | "\\\\" .)* "\\""\n'
        'number ::= "-"? [0-9]+ ("." [0-9]+)? ([eE] [-+]? [0-9]+)?\n'
        'ws     ::= [ \\t\\n]*'
    )
