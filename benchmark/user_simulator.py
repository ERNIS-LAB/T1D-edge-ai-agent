"""User simulators for multi-turn benchmark tasks.

Provides deterministic (scripted), LLM-based, and evaluator-based user simulators
to drive turn-by-turn agent evaluation.

The EvaluatorSimulator (tau-bench style) acts as both user and judge:
it has access to ground-truth expectations and can REJECT the agent mid-benchmark
when the agent makes an irrecoverable mistake.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Union

from agent.llm import get_llm


class ScriptedSimulator:
    """Deterministic user simulator driven by a scripted sequence of turns."""

    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.current_index = 0

    def next_message(self, _turn_history: list[dict[str, Any]]) -> str | None:
        """Return the next user message, or None if the script is exhausted."""
        if self.current_index >= len(self.script):
            return None
        turn_spec = self.script[self.current_index]
        self.current_index += 1
        return str(turn_spec.get("user_message", ""))

    def get_turn_spec(self, turn_number: int) -> dict[str, Any] | None:
        """Return the spec for a given turn (1-indexed)."""
        if 1 <= turn_number <= len(self.script):
            return self.script[turn_number - 1]
        return None

    @property
    def max_turns(self) -> int:
        return len(self.script)

    @property
    def turn_count(self) -> int:
        return self.current_index


class LLMSimulator:
    """LLM-based user simulator (optional Phase 2)."""

    SYSTEM_PROMPT = (
        "You are simulating a user interacting with a diabetes-assistant AI. "
        "Given the conversation history so far, respond with ONLY the next message "
        "the user would send. Be concise. If the task appears resolved, reply with "
        "the literal word DONE on its own line."
    )

    def __init__(
        self,
        task_description: str,
        model_name: str,
        system_prompt: str | None = None,
        provider: str | None = None,
    ):
        self.task_description = task_description
        self.model_name = model_name
        self.provider = provider
        self.system_prompt = system_prompt or self.SYSTEM_PROMPT
        self.turn_count = 0
        self._done = False

    def next_message(self, turn_history: list[dict[str, Any]]) -> str | None:
        """Ask the LLM what the user would say next."""
        if self._done:
            return None

        # Build context from turn history
        lines: list[str] = []
        lines.append(f"Task: {self.task_description}\n")
        for turn in turn_history:
            user_msg = turn.get("user_message", "")
            agent_resp = turn.get("final_response", "")
            lines.append(f"User: {user_msg}")
            lines.append(f"Assistant: {agent_resp}\n")

        prompt = "\n".join(lines) + "\nUser:"

        try:
            llm = get_llm(model_name=self.model_name, provider=self.provider)
            result = llm.invoke([("system", self.system_prompt), ("human", prompt)])
            text = str(getattr(result, "content", "")).strip()
        except Exception:
            return None

        # Check for done signal
        if text.upper() == "DONE" or text.startswith("DONE"):
            self._done = True
            return None

        self.turn_count += 1
        return text

    @property
    def max_turns(self) -> int:
        # LLM simulators don't have a fixed script length
        return 999


# ---------------------------------------------------------------------------
# Evaluator action types (tau-bench style)
# ---------------------------------------------------------------------------


@dataclass
class ContinueAction:
    """The evaluator says the agent is on track — continue with the next user message."""

    message: str
    turn: int


@dataclass
class RejectAction:
    """The evaluator says the agent made an irrecoverable mistake — stop the benchmark."""

    reason: str
    expected: str = ""
    actual: str = ""
    turn: int = 0


EvaluatorAction = Union[ContinueAction, RejectAction]


# ---------------------------------------------------------------------------
# Evaluator simulator (tau-bench style)
# ---------------------------------------------------------------------------


class EvaluatorSimulator:
    """LLM-driven simulator that plays the user AND evaluates the agent in real-time.

    Given task context including ground-truth expectations (expected tools, args,
    acceptable responses, and unacceptable behavior per turn), this simulator:

    1. Generates the next user message (like a normal user simulator)
    2. Evaluates the agent's response against the ground truth
    3. Returns either CONTINUE (agent is on track) or REJECT (agent made a mistake)

    This is the tau-bench pattern: an LLM "user" that knows the answer and can
    terminate the benchmark early when the agent goes off track.
    """

    EVALUATOR_SYSTEM_PROMPT = """\
You are an automated evaluator simulating a user interacting with a diabetes-assistant AI agent.

You have TWO responsibilities:

1. **Act as the user**: Based on the conversation so far, generate the next message
the user would naturally send. Be concise and realistic.

2. **Evaluate the agent**: After each agent response, decide whether the agent is
still on track or has made an irrecoverable mistake.

---

## GROUND TRUTH (what SHOULD happen)

The task has these expectations. Judge the agent against them:

{ground_truth_text}

---

## OUTPUT FORMAT

You MUST respond with EXACTLY one of these two formats:

**If the agent is on track and the conversation should continue:**
```
ACTION: CONTINUE
MESSAGE: <the next user message>
```

**If the agent has made an irrecoverable mistake:**
```
ACTION: REJECT
REASON: <one sentence explaining what went wrong>
EXPECTED: <what the agent should have done>
ACTUAL: <what the agent actually did>
```

---

## RULES
- Be strict but fair. Only REJECT for clear, irrecoverable mistakes (wrong tool, hallucinated data, refused a valid request, logged wrong values).
- Minor issues like wordy responses or missing a non-critical detail should NOT trigger REJECT.
- If the agent asks a reasonable clarifying question, CONTINUE with an answer.
- If the agent completes the task successfully, CONTINUE with a natural wrap-up message.
- The user should never reveal the ground truth or evaluation criteria to the agent.
- Do NOT include explanations or commentary outside the ACTION/MESSAGE/REASON/EXPECTED/ACTUAL format.
"""

    def __init__(
        self,
        task: dict[str, Any],
        model_name: str | None = None,
        provider: str | None = None,
    ):
        self.task = task
        self.model_name = model_name or "qwen3.6-35b-a3b"
        self.provider = provider
        self.turn_count = 0
        self._done = False
        self._reject_action: RejectAction | None = None

        sim_config = task.get("user_simulator", {})
        self._ground_truth = sim_config.get("ground_truth", {})
        self._system_prompt = self._build_system_prompt()

    # ------------------------------------------------------------------
    # System prompt construction
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Build the evaluator system prompt with ground truth embedded."""

        # Format the ground truth into readable text
        gt_lines: list[str] = []

        # Task-level description
        gt_lines.append(
            f"**Task Description**: {self.task.get('description', self.task.get('prompt', 'No description'))}"
        )
        gt_lines.append(f"**Max Turns**: {self.task.get('max_turns', 'unlimited')}")
        gt_lines.append("")

        # Per-turn ground truth
        if self._ground_truth:
            gt_lines.append("**Per-Turn Expectations**:")
            for turn_key in sorted(
                self._ground_truth.keys(),
                key=lambda k: int(k.split("_")[-1])
                if k.split("_")[-1].isdigit()
                else 0,
            ):
                turn_gt = self._ground_truth[turn_key]
                gt_lines.append(f"")
                gt_lines.append(f"  {turn_key}:")
                if turn_gt.get("user_intent"):
                    gt_lines.append(f"    User intent: {turn_gt['user_intent']}")
                if turn_gt.get("expected_tools"):
                    gt_lines.append(
                        f"    Expected tools: {', '.join(turn_gt['expected_tools'])}"
                    )
                if turn_gt.get("expected_args"):
                    gt_lines.append(
                        f"    Expected args: {json.dumps(turn_gt['expected_args'])}"
                    )
                if turn_gt.get("acceptable_responses"):
                    gt_lines.append(
                        f"    Acceptable response contains: {', '.join(turn_gt['acceptable_responses'])}"
                    )
                if turn_gt.get("unacceptable_behavior"):
                    gt_lines.append(f"    UNACCEPTABLE (should REJECT):")
                    for ub in turn_gt["unacceptable_behavior"]:
                        gt_lines.append(f"      - {ub}")
        else:
            gt_lines.append(
                "**Per-Turn Expectations**: (none specified — use general task description to judge)"
            )

        ground_truth_text = "\n".join(gt_lines)

        return self.EVALUATOR_SYSTEM_PROMPT.format(ground_truth_text=ground_truth_text)

    # ------------------------------------------------------------------
    # Core evaluation logic
    # ------------------------------------------------------------------

    def next_action(self, turn_history: list[dict[str, Any]]) -> EvaluatorAction | None:
        """Evaluate the latest agent response and decide CONTINUE or REJECT.

        Returns:
            ContinueAction if the agent is on track and the user should respond.
            RejectAction if the agent made an irrecoverable mistake.
            None if the conversation is finished (DONE signal).
        """
        if self._done:
            return None

        # Build the conversation transcript for the evaluator LLM
        transcript = self._build_transcript(turn_history)

        try:
            llm = get_llm(model_name=self.model_name, provider=self.provider)
            result = llm.invoke(
                [
                    ("system", self._system_prompt),
                    ("human", transcript),
                ]
            )
            text = str(getattr(result, "content", "")).strip()
        except Exception as exc:
            # If the evaluator LLM fails, default to CONTINUE so the benchmark
            # can still complete. Log the error in the message.
            self.turn_count += 1
            return ContinueAction(
                message=f"[Evaluator LLM error: {exc}] Please continue.",
                turn=self.turn_count,
            )

        action = self._parse_action(text)

        if action is None:
            # Couldn't parse — treat as DONE to avoid infinite loops
            self._done = True
            return None

        if isinstance(action, RejectAction):
            self._done = True
            self._reject_action = action
            return action

        # ContinueAction
        self.turn_count += 1
        return action

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _build_transcript(self, turn_history: list[dict[str, Any]]) -> str:
        """Build the conversation transcript for the evaluator LLM."""
        lines: list[str] = []
        lines.append(
            "Evaluate the following conversation between a user and a diabetes assistant agent.\n"
        )

        for turn in turn_history:
            user_msg = turn.get("user_message", "")
            agent_resp = turn.get("final_response", "")
            tool_calls = turn.get("tool_calls", [])

            lines.append(f"--- Turn {turn.get('turn_number', '?')} ---")
            lines.append(f"User: {user_msg}")

            if tool_calls:
                tool_names = [c.get("name", "unknown") for c in tool_calls]
                tool_args = [json.dumps(c.get("args", {})) for c in tool_calls]
                lines.append(f"Agent called tools: {', '.join(tool_names)}")
                for name, args in zip(tool_names, tool_args):
                    lines.append(f"  - {name}({args})")

            if agent_resp:
                # Truncate very long responses
                truncated = (
                    agent_resp[:1000] + "..." if len(agent_resp) > 1000 else agent_resp
                )
                lines.append(f"Agent response: {truncated}")

            lines.append("")

        if turn_history:
            lines.append(
                "Based on the above, decide ACTION: CONTINUE or ACTION: REJECT."
            )
        else:
            lines.append(
                "This is the start of the conversation. The user should send the first message based on the ground truth."
            )
            lines.append("Respond with ACTION: CONTINUE and the first user message.")

        return "\n".join(lines)

    def _parse_action(self, text: str) -> EvaluatorAction | None:
        """Parse the evaluator LLM output into an EvaluatorAction."""
        text = text.strip()

        # Extract ACTION line
        action_match = _extract_field(text, "ACTION")
        if not action_match:
            # Try to detect from content
            if "REJECT" in text.upper():
                action_match = "REJECT"
            elif "CONTINUE" in text.upper():
                action_match = "CONTINUE"
            else:
                return None

        action_type = action_match.strip().upper()

        if action_type == "REJECT":
            reason = _extract_field(text, "REASON") or "No reason provided"
            expected = _extract_field(text, "EXPECTED") or ""
            actual = _extract_field(text, "ACTUAL") or ""
            return RejectAction(
                reason=reason,
                expected=expected,
                actual=actual,
                turn=self.turn_count,
            )

        if action_type == "CONTINUE":
            message = _extract_field(text, "MESSAGE")
            if message is None:
                # Fallback: everything after CONTINUE that isn't a field
                message = "Please continue."
            return ContinueAction(
                message=message,
                turn=self.turn_count,
            )

        # DONE signal
        if action_type == "DONE" or text.upper().startswith("DONE"):
            self._done = True
            return None

        return None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def reject_reason(self) -> dict[str, Any] | None:
        """Return the rejection details if the evaluator rejected the agent."""
        if self._reject_action is None:
            return None
        return {
            "reason": self._reject_action.reason,
            "expected": self._reject_action.expected,
            "actual": self._reject_action.actual,
            "turn": self._reject_action.turn,
        }

    @property
    def max_turns(self) -> int:
        return self.task.get("max_turns", 10)

    @property
    def is_done(self) -> bool:
        return self._done


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_field(text: str, field: str) -> str | None:
    """Extract a field value from structured evaluator output.

    Supports formats like:
        FIELD: value
        FIELD: value\nNEXT_FIELD: ...
    """
    # Try exact field match
    pattern = re.compile(rf"^{field}:\s*(.+?)$", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()

    # Try field anywhere in text (less strict)
    pattern2 = re.compile(
        rf"{field}:\s*(.+?)(?:\n[A-Z]+:|$)", re.DOTALL | re.IGNORECASE
    )
    match2 = pattern2.search(text)
    if match2:
        return match2.group(1).strip()

    return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_simulator(
    task: dict[str, Any],
    provider: str | None = None,
    model_override: str | None = None,
) -> ScriptedSimulator | LLMSimulator | EvaluatorSimulator:
    """Build the appropriate simulator for a task.

    provider / model_override let the benchmark CLI route the (LLM-based) chat
    simulator at a different endpoint/model than the one named in the task JSON —
    e.g. the remote OpenCode Zen endpoint. They are ignored by the scripted
    simulator, which makes no LLM calls.
    """
    sim_config = task.get("user_simulator", {})
    sim_type = sim_config.get("type", "scripted")

    if sim_type == "evaluator":
        return EvaluatorSimulator(
            task=task,
            model_name=model_override or sim_config.get("model"),
            provider=provider,
        )

    if sim_type == "llm":
        return LLMSimulator(
            task_description=task.get("description", task.get("prompt", "")),
            model_name=model_override or sim_config.get("model", "qwen3.6-35b-a3b"),
            system_prompt=sim_config.get("system_prompt"),
            provider=provider,
        )

    # Default: scripted
    return ScriptedSimulator(script=sim_config.get("script", []))
