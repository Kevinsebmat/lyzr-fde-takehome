"""ReAct planner: observe → think → act → reflect, with four ways to stop.

The graded failure mode is the infinite loop, and the honest lesson is that
one termination condition is not enough. An iteration cap alone lets an agent
burn its entire budget repeating a call that will never work. Loop detection
alone lets it wander forever through slightly different useless actions. So
there are four independent stopping conditions, any one of which ends the run:

    SOLVED           the agent says it has the answer
    MAX_ITERATIONS   the hard ceiling — the backstop, not the plan
    LOOP_DETECTED    the same action and argument repeated
    NO_PROGRESS      self-critique reported no progress N times running
    BUDGET_EXHAUSTED spend ceiling reached mid-run

And every one of them returns a result. The agent never raises on termination:
it degrades to the best answer it can support from what it already gathered,
labelled with why it stopped. Throwing away six tool calls' worth of work
because the seventh didn't happen is a worse outcome than a partial answer
that says it is partial.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from agentcore import LLM, Budget, BudgetExceeded, ParseError, default_llm, tracing
from pydantic import BaseModel, Field

from .tools import Registry, default_registry

log = logging.getLogger("p03.agent")


class Outcome(str, Enum):
    solved = "solved"
    max_iterations = "max_iterations"
    loop_detected = "loop_detected"
    no_progress = "no_progress"
    budget_exhausted = "budget_exhausted"
    failed = "failed"


SYSTEM = """You solve tasks by reasoning and calling tools, one step at a time.

Each turn: state your reasoning, then either call one tool or give the final
answer. Work from what the observations actually say — never assume a tool
result you have not seen.

If an observation shows a tool is failing or returning nothing useful, do not
call it again with the same input. Either try a different approach or finish
with what you have.

Set `done` to true and fill `final_answer` as soon as you can answer. Finishing
early is good; padding the trace with extra calls is not."""

REFLECT_SYSTEM = """You are auditing an agent's last step.

Judge only whether the step moved the task forward. An observation that was an
error, was empty, or repeated an earlier one is not progress. Be blunt: an
agent that believes it is progressing when it is not will loop forever."""


class Step(BaseModel):
    thought: str = Field(min_length=1, description="Reasoning for this step.")
    done: bool = Field(description="True when you can give the final answer now.")
    action: str | None = Field(default=None, description="Tool name, if calling one.")
    action_input: str | None = Field(default=None, description="Argument for the tool.")
    final_answer: str | None = Field(default=None, description="The answer, when done.")


class Reflection(BaseModel):
    progressed: bool = Field(description="Did the last step move the task forward?")
    critique: str = Field(min_length=1, max_length=400)
    can_answer_now: bool = Field(description="Is there enough information to answer?")


@dataclass
class Trace:
    iteration: int
    thought: str
    action: str | None = None
    action_input: str | None = None
    observation: str | None = None
    critique: str | None = None
    progressed: bool = True

    def signature(self) -> str | None:
        """Identity of the action taken, for loop detection."""
        if not self.action:
            return None
        return f"{self.action}::{(self.action_input or '').strip().lower()}"

    def as_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "thought": self.thought,
            "action": self.action,
            "action_input": self.action_input,
            "observation": self.observation,
            "critique": self.critique,
            "progressed": self.progressed,
        }


@dataclass
class ReActResult:
    task: str
    outcome: Outcome
    answer: str
    steps: list[Trace] = field(default_factory=list)
    reason: str = ""
    cost_usd: float = 0.0
    tool_calls: int = 0

    @property
    def solved(self) -> bool:
        return self.outcome is Outcome.solved

    @property
    def degraded(self) -> bool:
        """Terminated early but still produced something usable."""
        return not self.solved and bool(self.answer)

    def summary(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "iterations": len(self.steps),
            "tool_calls": self.tool_calls,
            "degraded": self.degraded,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class ReActAgent:
    llm: LLM = None  # type: ignore[assignment]
    registry: Registry = None  # type: ignore[assignment]
    max_iterations: int = 8
    #: How many times the identical action may repeat before we call it a loop.
    #: 2 allows a legitimate retry; 3 is a pattern.
    max_repeats: int = 2
    #: Consecutive unproductive steps tolerated before giving up.
    max_stalls: int = 2
    reflect: bool = True

    def __post_init__(self):
        if self.llm is None:
            self.llm = default_llm("p03-react-planner", budget=Budget(limit_usd=0.40))
        if self.registry is None:
            self.registry = default_registry()

    def run(self, task: str) -> ReActResult:
        with tracing.run("p03-react-planner", "react", task=task[:120]):
            before = self.llm.budget.spent_usd
            steps: list[Trace] = []
            seen: dict[str, int] = {}
            stalls = 0

            for i in range(1, self.max_iterations + 1):
                try:
                    step = self._think(task, steps)
                except BudgetExceeded as exc:
                    return self._stop(
                        task, Outcome.budget_exhausted, steps,
                        f"spend ceiling reached: {exc}", before,
                    )
                except ParseError as exc:
                    # The planner itself produced unusable output. Stop rather
                    # than loop on a broken planner.
                    log.error("planner output failed validation: %s", exc)
                    return self._stop(
                        task, Outcome.failed, steps,
                        "planner produced malformed output", before,
                    )

                trace = Trace(iteration=i, thought=step.thought)

                if step.done:
                    trace.action = None
                    steps.append(trace)
                    answer = step.final_answer or self._salvage(task, steps)
                    return self._finish(task, Outcome.solved, answer, steps, "", before)

                if not step.action:
                    # Not done, but no action either — nothing will change on
                    # the next turn, so this is a stall, not a step.
                    trace.progressed = False
                    trace.critique = "no action and not finished"
                    steps.append(trace)
                    stalls += 1
                    if stalls >= self.max_stalls:
                        return self._stop(
                            task, Outcome.no_progress, steps,
                            "planner stopped choosing actions without finishing", before,
                        )
                    continue

                trace.action = step.action
                trace.action_input = step.action_input or ""

                signature = trace.signature()
                seen[signature] = seen.get(signature, 0) + 1
                if seen[signature] > self.max_repeats:
                    steps.append(trace)
                    return self._stop(
                        task, Outcome.loop_detected, steps,
                        f"repeated `{step.action}` with the same input "
                        f"{seen[signature]} times", before,
                    )

                trace.observation = self._act(step.action, trace.action_input)
                steps.append(trace)

                if self.reflect:
                    try:
                        reflection = self._critique(task, trace)
                    except (ParseError, BudgetExceeded):
                        # Self-critique is a safety net; if it fails, keep going
                        # on the hard caps rather than aborting a working run.
                        reflection = None
                    if reflection is not None:
                        trace.critique = reflection.critique
                        trace.progressed = reflection.progressed
                        stalls = 0 if reflection.progressed else stalls + 1
                        if stalls >= self.max_stalls:
                            return self._stop(
                                task, Outcome.no_progress, steps,
                                f"no progress for {stalls} consecutive steps: "
                                f"{reflection.critique}", before,
                            )

            return self._stop(
                task, Outcome.max_iterations, steps,
                f"hit the {self.max_iterations}-iteration ceiling", before,
            )

    # ---------- steps ----------

    def _think(self, task: str, steps: list[Trace]) -> Step:
        prompt = (
            f"TASK\n{task}\n\nAVAILABLE TOOLS\n{self.registry.describe()}\n\n"
            f"HISTORY\n{self._render(steps) or '(nothing yet)'}\n\n"
            "What is your next step?"
        )
        step, _ = self.llm.parse(prompt, Step, system=SYSTEM, name="react.think")
        return step

    def _act(self, name: str, arg: str) -> str:
        tool = self.registry.get(name)
        if tool is None:
            # A hallucinated tool name is an observation the agent can recover
            # from, so name the real tools rather than just refusing.
            return (
                f"ERROR: no tool named {name!r}. Available: "
                f"{', '.join(self.registry.names)}."
            )
        return tool(arg)

    def _critique(self, task: str, trace: Trace) -> Reflection:
        prompt = (
            f"TASK\n{task}\n\nSTEP\nthought: {trace.thought}\n"
            f"action: {trace.action}({trace.action_input})\n"
            f"observation: {trace.observation}\n\nDid this move the task forward?"
        )
        reflection, _ = self.llm.parse(
            prompt, Reflection, system=REFLECT_SYSTEM, name="react.reflect"
        )
        return reflection

    def _salvage(self, task: str, steps: list[Trace]) -> str:
        """Best answer supportable by the observations already gathered.

        Called when the loop stops without a final answer. Discarding the work
        already done is worse than a partial answer that says it is partial.
        """
        observations = [s for s in steps if s.observation]
        if not observations:
            return ""
        prompt = (
            f"TASK\n{task}\n\nOBSERVATIONS GATHERED\n{self._render(steps)}\n\n"
            "The agent stopped before finishing. Answer as fully as these "
            "observations support, and say plainly what is still unknown. Do "
            "not invent anything not present above."
        )
        try:
            return self.llm.complete(prompt, name="react.salvage").text.strip()
        except (BudgetExceeded, Exception) as exc:  # noqa: BLE001
            log.warning("could not salvage a partial answer: %s", exc)
            return ""

    def _render(self, steps: list[Trace]) -> str:
        out = []
        for s in steps:
            out.append(f"[{s.iteration}] thought: {s.thought}")
            if s.action:
                out.append(f"    action: {s.action}({s.action_input})")
                out.append(f"    observation: {s.observation}")
            if s.critique:
                out.append(f"    critique: {s.critique}")
        return "\n".join(out)

    # ---------- termination ----------

    def _stop(
        self, task: str, outcome: Outcome, steps: list[Trace], reason: str, before: float
    ) -> ReActResult:
        """Every early exit lands here, and every early exit still answers."""
        log.info("terminating: %s (%s)", outcome.value, reason)
        answer = self._salvage(task, steps)
        if answer:
            answer = f"{answer}\n\n[Partial: {reason}.]"
        else:
            answer = f"I could not complete this task. {reason.capitalize()}."
        return self._finish(task, outcome, answer, steps, reason, before)

    def _finish(
        self, task, outcome, answer, steps, reason, before
    ) -> ReActResult:
        return ReActResult(
            task=task,
            outcome=outcome,
            answer=answer,
            steps=steps,
            reason=reason,
            cost_usd=self.llm.budget.spent_usd - before,
            tool_calls=self.registry.total_calls(),
        )
