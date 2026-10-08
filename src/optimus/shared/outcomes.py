"""What an attempted moderation action did, step by step.

Shared because moderation produces these and the interaction and setup code
explains them to people (:mod:`optimus.shared.explain`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from optimus.contracts.events import Action
from optimus.shared.failures import Failure


class Step(StrEnum):
    """An independently-applied part of an action."""

    DELETE = "delete"
    TIMEOUT = "timeout"
    KICK = "kick"
    BAN = "ban"
    DM = "dm"


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What happened to one step of an action."""

    step: Step
    success: bool
    #: Classified cause when the step did not succeed.
    failure: Failure | None = None
    #: True when a preflight proved the call could not succeed, so no request
    #: was sent. Avoids a guaranteed 403 per scam image during a raid.
    skipped: bool = False
    #: Permission names the bot lacks, in Discord's own wording.
    missing: tuple[str, ...] = ()

    @property
    def recoverable(self) -> bool:
        """Whether this step could succeed later, e.g. after a permission fix."""
        return self.failure is not None and self.failure.recoverable


@dataclass(frozen=True, slots=True)
class ActionResult:
    """The outcome of attempting an action, including every step's result."""

    action: Action
    success: bool
    detail: str | None = None
    #: Per-step outcomes, in execution order. Empty for short-circuit results
    #: (duplicate, rate limited) where no step ran.
    steps: tuple[StepOutcome, ...] = field(default_factory=tuple)

    @property
    def failed_steps(self) -> tuple[StepOutcome, ...]:
        """Steps that did not succeed, excluding the best-effort DM."""
        return tuple(s for s in self.steps if not s.success and s.step is not Step.DM)

    @property
    def succeeded_steps(self) -> tuple[StepOutcome, ...]:
        """Steps that did succeed, excluding the best-effort DM."""
        return tuple(s for s in self.steps if s.success and s.step is not Step.DM)

    @property
    def partial(self) -> bool:
        """Whether the offender was punished but some step still failed.

        This is the case worth surfacing loudly: enforcement happened, so the
        report must not claim total success, but the scam message may survive.
        """
        return bool(self.succeeded_steps) and bool(self.failed_steps)

    @property
    def recoverable_steps(self) -> tuple[StepOutcome, ...]:
        """Failed steps that a later permission fix could still complete."""
        return tuple(s for s in self.failed_steps if s.recoverable)

    @property
    def message_deleted(self) -> bool:
        """Whether the offending message was actually removed.

        Read from the delete step's own outcome rather than inferred from
        ``action`` or ``success``: a ``delete_ban`` whose delete was refused for
        want of Manage Messages still leaves the message (and its image) in
        place, which is exactly the case a moderator needs to look at.
        """
        return any(s.step is Step.DELETE and s.success for s in self.steps)
