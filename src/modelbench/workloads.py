"""Test workloads: token-calibrated prompts for each case.

Input sizes are calibrated with tiktoken so 'short / medium / long / XL' mean the
same thing across models. Output sizes are requested via max_output_tokens and a
task that naturally fills the budget (continued writing / enumeration), so TPS is
measured over a stable, non-trivial decode.
"""

from __future__ import annotations

from dataclasses import dataclass

import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")

# A filler paragraph that is ~60 tokens; repeated/built-on to hit target input sizes.
_BLOCK = (
    "Retrieval-augmented generation combines a retriever over a vector index with a "
    "generator that conditions on retrieved chunks. Latency is dominated by the "
    "pre-fill of long contexts and by auto-regressive decoding of the answer. In an "
    "agent loop, each step issues a model call, so per-call time-to-first-token and "
    "decode throughput directly determine task wall-clock time. "
)


def _ntok(text: str) -> int:
    return len(_ENC.encode(text))


def build_input(target_tokens: int) -> str:
    """Return a prompt body of approximately target_tokens tokens."""
    out = []
    n = 0
    i = 0
    while n < target_tokens:
        # vary slightly so it isn't pure repetition
        out.append(f"[passage {i}] " + _BLOCK)
        n = _ntok("".join(out))
        i += 1
    return "".join(out)


@dataclass
class Workload:
    case: str
    input_text: str
    max_output_tokens: int
    instructions: str  # the task appended after the input body
    tools: list[dict] | None = None
    tool_choice: str | None = None
    effort: str | None = None  # reasoning effort: low | medium | high (None = provider default)


def _mk(case: str, in_tok: int, out_tok: int, instructions: str) -> Workload:
    body = build_input(in_tok) if in_tok > 0 else ""
    return Workload(
        case=case,
        input_text=body,
        max_output_tokens=out_tok,
        instructions=instructions,
    )


# Output-bearing instruction: forces the model to actually produce ~out tokens.
def _gen_instruction(out_tok: int) -> str:
    return (
        f"\n\nTASK: Write a detailed continuous analysis of the passages above, about "
        f"{out_tok} tokens long. Do not stop early; enumerate concrete points in full "
        f"sentences and keep writing until you reach the target length."
    )


def standard_workloads() -> list[Workload]:
    return [
        _mk("S", 50, 200, _gen_instruction(200)),
        _mk("M", 1000, 500, _gen_instruction(500)),
        _mk("L", 4000, 1000, _gen_instruction(1000)),
        _mk("XL", 8000, 2000, _gen_instruction(2000)),
    ]


_AGENT_TOOLS = [
    {
        "type": "function",
        "name": "search_knowledgebase",
        "description": "Search the video knowledge base for chunks relevant to a query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "top_k": {"type": "integer", "description": "Number of chunks to return."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
]


def agent_workload() -> Workload:
    """A realistic agent step: system role + tool schema + a user request that must
    trigger a single, schema-valid function call."""
    system = (
        "You are an orchestration agent for a video knowledge base. You may call the "
        "provided tools to retrieve information. Decide the single best next action. "
        "Always respond by calling exactly one tool with valid arguments. "
        + build_input(1200)  # pad system+context to ~2k to mimic real agent context
    )
    user = (
        "Find the segments where the speaker explains how the retriever is evaluated, "
        "and get the top 5 most relevant chunks."
    )
    return Workload(
        case="agent",
        input_text=system + "\n\nUSER: " + user,
        max_output_tokens=300,
        instructions="\n\nRespond with a single tool call.",
        tools=_AGENT_TOOLS,
        tool_choice="required",
    )


def cache_workload() -> Workload:
    """Long static prefix reused across repeats; the harness sends the identical
    request several times so context-caching (if any) kicks in on attempts 2..N."""
    prefix = build_input(6000)
    return Workload(
        case="cache",
        input_text=prefix,
        max_output_tokens=200,
        instructions="\n\nTASK: In one short paragraph, summarize the passages above.",
    )


def all_workloads() -> list[Workload]:
    return [*standard_workloads(), agent_workload(), cache_workload()]
