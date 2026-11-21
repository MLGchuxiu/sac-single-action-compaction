"""Prompts: system instruction, task framing, and the four state-update variants.

`state_v1` is the paper's Appendix G.1 handoff-recap template verbatim.
`state_v2`..`state_v4` are the iteration ladder described in the report: a
structured semantic state, then a deterministic action digest, then bounded
adaptive budgets plus a history-recall affordance.
"""
from __future__ import annotations

SYSTEM_PROMPT = """You solve the supplied task by interacting with a computer through tools.
Work step by step: inspect relevant evidence, act, observe the result, and verify that it meets the task's requirements.
Keep command output focused: read slices of large files instead of dumping them.
When the task is complete, call the finish tool with your final answer."""

SYSTEM_PROMPT_COMPACT = """You solve the supplied task by interacting with a computer through tools.
Work step by step: inspect relevant evidence, act, observe the result, and verify that it meets the task's requirements.
Keep command output focused: read slices of large files instead of dumping them.
When the task is complete, call the finish tool with your final answer.
You are running under single-action compaction: after every action your working conversation is replaced by a compact
semantic state. Everything you will need later must be written into that state, so record exact values, paths and
next steps as you go."""

# --------------------------------------------------------------------------- #
# Task framing
# --------------------------------------------------------------------------- #

TASK_TEMPLATE = """<task>
{task}
</task>
{input_details}
Available tools: bash, read_file, write_file, edit_file, list_dir, grep, view_image, finish.

How to work: inspect the evidence you need, make one focused change or read per step, and verify your work.
When done, call finish with the final answer."""

TASK_TEMPLATE_WITH_STATE = """<task>
{task}
</task>
{input_details}
<semantic_state>
{state}
</semantic_state>

The block above is the compacted state of your earlier work in this task. Treat it as your own memory:
established facts are verified observations, "remaining plan" is what you still owe. Continue from it.
Available tools: bash, read_file, write_file, edit_file, list_dir, grep, view_image, finish."""

# --------------------------------------------------------------------------- #
# State-update instructions (one per iteration)
# --------------------------------------------------------------------------- #

STATE_V1 = """Stop work. Write only a concise handoff recap; do not run commands or call tools.
The conversation will reset to the system prompt, task, and this recap.
Record:
1. Objective - one line describing the required result.
2. Established facts - exact paths, relevant symbols or commands, observed values,
   and important errors/output. Separate observations from unverified ideas.
3. Applied changes - each changed/created file, what changed, and whether verified.
4. Verification - exact checks run, outcomes, failures, obstacles, and workarounds.
5. Dead ends - failed approaches that should not be repeated.
6. Remaining plan - outstanding subgoals, in execution order; the immediate next
   command or edit; and the condition for completion.
If needed output was truncated, give a narrow command to retrieve it."""

STATE_V2 = """Stop work and write the updated semantic state. Do not run commands or call tools.

Output exactly these sections, in this order, and nothing else:

OBJECTIVE: one line.
FACTS:
- key: value  (exact numbers, paths, symbol names, command outputs that matter)
CHANGES:
- path: what changed (verified: yes/no)
VERIFIED:
- check: outcome
DEAD_ENDS:
- approach: why it failed
NEXT: the single next command or edit, and the completion condition.

Rules: keep every exact value you will need later; never invent an observation;
do not restate the task text; stay under {budget} tokens."""

STATE_V3 = """Stop work and write the updated semantic state. Do not run commands or call tools.

You will receive a machine-generated ACTION DIGEST of the step you just took. Merge it into the state:
every digest value that matters for the task must survive in your FACTS section.

Output exactly these sections, in this order, and nothing else:

OBJECTIVE: one line.
FACTS:
- key: value
CHANGES:
- path: what changed (verified: yes/no)
VERIFIED:
- check: outcome
DEAD_ENDS:
- approach: why it failed
NEXT: the single next command or edit, and the completion condition.

Rules: preserve exact values (numbers, paths, identifiers) verbatim; drop anything
the digest does not confirm as observed; do not restate the task text; stay under
{budget} tokens."""

STATE_V4 = """Stop work. Write only a concise handoff recap; do not run commands or call tools.
The conversation will reset to the system prompt, task, and this recap.

ANCHORS (copy verbatim, never paraphrase): the task's non-negotiable requirements - exact output
paths, required section names, required JSON keys, exact thresholds, and any identifier you must
reproduce later. If a requirement was stated in the task, it belongs here.

Record:
1. Objective - one line describing the required result.
2. Established facts - exact paths, relevant symbols or commands, observed values, and important
   errors/output. The ACTION DIGEST below lists what you just observed; every value that matters
   must appear here verbatim. Separate observations from unverified ideas.
3. Applied changes - each changed/created file, what changed, and whether verified.
4. Verification - exact checks run, outcomes, failures, obstacles, and workarounds.
5. Dead ends - failed approaches that should not be repeated.
6. Remaining plan - outstanding subgoals, in execution order; the immediate next command or edit;
   and the condition for completion.

A `recall` tool is available in later steps: it searches your full archived transcript by keyword.
Use this recap for what you must not lose, and recall only for details you chose not to record.
Stay under {budget} tokens."""


# --------------------------------------------------------------------------- #
# Iteration 5-8: checklist, inline state, cache/gating, combined
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT_INLINE = """You solve the supplied task by interacting with a computer through tools.
Work step by step: inspect relevant evidence, act, observe the result, and verify that it meets the task's requirements.
Keep command output focused: read slices of large files instead of dumping them.
When the task is complete, call the finish tool with your final answer.

You are running under single-action compaction. EVERY reply must start with a <state>...</state> block that
rewrites your full recap (objective, anchors, exact facts, changes, verification, open items, next step) so a
fresh conversation can continue from it. The harness keeps only that block; the rest of the transcript is
discarded after each step. After the block, call your tools."""

CHECKLIST_PLAN = """Before you start, write the checklist of subgoals this task requires.
Output 3-8 short imperative items, one per line, no numbering, no commentary.
Each item must be independently checkable. Do not include the final answer.
"""

_STATE_V4_BODY = STATE_V4

STATE_V5 = _STATE_V4_BODY.replace(
    "A `recall` tool is available in later steps",
    "The CHECKLIST above is maintained outside your state: when you finish an item call `check` with its index.\n"
    "A `recall` tool is available in later steps",
)

STATE_V6 = STATE_V5 + """

Also: end every reply with a <state>...</state> block holding the full updated recap, written BEFORE your tool
calls. The harness stores it and drops the rest of the transcript, so this block is your only memory."""

STATE_V7 = STATE_V6 + """

Re-reading a file you already read returns a cache notice instead of the content; the content is already in your
recap. Keep FACTS precise enough that you never need the file again."""

STATE_V8 = STATE_V7 + """

Budgets shrink early and grow with progress. Prefer fewer, higher-value facts over completeness."""


# --------------------------------------------------------------------------- #
# Iteration 10-12: evidence-preserving state, claim registry, metric provenance
# --------------------------------------------------------------------------- #

STATE_V10 = """Stop work and write the updated semantic state. Do not run commands or call tools.

A machine LEDGER of everything you have already done is attached below. It is exact and authoritative:
never contradict it, and never repeat an action it lists as already done.

Output exactly these sections, in this order, and nothing else:

OBJECTIVE: one line.
FACTS:
- key: value  (exact numbers, paths, identifiers; only what you actually observed)
IMAGES_OBSERVED:
- <image path>: what you saw in it - objects, counts, labels, positions. One line each. Never guess.
CHANGES:
- path: what changed (verified: yes/no)
OPEN_QUESTIONS:
- question: the observation that would settle it
NEXT: the single next action, and the completion condition.

Rules: an image you viewed is evidence - record what it showed before it leaves your context.
If the ledger and your memory disagree, the ledger wins. Stay under {budget} tokens."""

STATE_V11 = """Stop work and write the updated semantic state. Do not run commands or call tools.

The attached machine LEDGER is exact and authoritative. Your state must be evidence-first: every
claim in it must name the observation that supports it, and every subgoal must be either verified
or listed as open.

Output exactly these sections, in this order, and nothing else:

OBJECTIVE: one line.
VERIFIED_FACTS:
- fact  <- evidence (which image / command / file, and what it showed)
CLAIMS:
- subject = value  <- evidence; method=<visual|script|read|...>
IMAGES_OBSERVED:
- <image path>: what you saw (counts, labels, positions). Never guess.
OPEN_QUESTIONS:
- question: the observation that would settle it
NEXT: the single next action, and the completion condition.

Rules: a claim without evidence is not allowed in VERIFIED_FACTS. A rule of thumb
("one item per row") is not evidence. Register every CLAIMS entry with note(...).
Stay under {budget} tokens."""

STATE_V12 = STATE_V11.replace(
    "Rules: a claim without evidence is not allowed in VERIFIED_FACTS.",
    """METRIC_PROVENANCE:
- claim: method, threshold or parameter, what that method can miss, and the independent cross-check.

Rules: a number produced by a detector/script is a proxy, not an observation: always record what the
threshold excludes and cross-check it against a different method before concluding. A claim without
evidence is not allowed in VERIFIED_FACTS.""",
)

INLINE_STATE_HINT = (
    "Remember: end this reply with a <state>...</state> block containing the full updated recap "
    "(objective, anchors, facts, changes, verification, open items, next step), then make your tool calls."
)

STATE_V13 = STATE_V12 + """

Evidence freshness: a conclusion about what an image shows is only valid while that image is still in
your context. If the ledger says the last image view was several steps ago, view it again before you
state the count. A detector's threshold tells you what it kept, never what exists."""
STATE_INSTRUCTIONS = {"v1": STATE_V1, "v2": STATE_V2, "v3": STATE_V3, "v4": STATE_V4,
                      "v5": STATE_V5, "v6": STATE_V6, "v7": STATE_V7, "v8": STATE_V8,
                      "v9": STATE_V6, "v10": STATE_V10, "v11": STATE_V11, "v12": STATE_V12,
                      "v13": STATE_V13}

INITIAL_STATE = "No prior work exists. This is the first action of the task."

STATE_NUDGE = (
    "Your previous response contained no tool call and no answer. "
    "Either call a tool to make progress, or call finish with your final answer."
)

EoCC_INSTRUCTION = """Stop work. Write only a concise handoff recap; do not run commands or call tools.
The conversation will reset to the system prompt, task, and this recap.
Preserve what your next stretch needs:
1. Current progress and best answer/result, separating verified facts from assumptions.
2. Relevant evidence with exact values and source locations; changes already made, if any, with paths and verification status.
3. What was solved, what worked, what remains unresolved, what failed, and useful commands already tried.
4. Remaining plan - outstanding subgoals, in execution order; the immediate next command or edit; and the condition for completion.
If important output was truncated, name a narrow command to retrieve the missing part.
Do not invent observations or repeat the full task or document manifest."""


def state_instruction(version: str, budget: int) -> str:
    template = STATE_INSTRUCTIONS.get(version, STATE_V1)
    return template.replace("{budget}", str(budget))


def build_task_message(task: str, input_details: str = "", state: str | None = None) -> str:
    if state is None:
        return TASK_TEMPLATE.format(task=task, input_details=input_details).strip()
    return TASK_TEMPLATE_WITH_STATE.format(task=task, input_details=input_details, state=state).strip()