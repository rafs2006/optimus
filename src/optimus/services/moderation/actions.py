"""Execution of moderation actions against Discord, guarded for safety.

Every punitive action flows through :class:`ActionExecutor`, which layers four
protections over the raw Discord REST calls:

* a **per-guild token bucket** so a noisy guild cannot exhaust the global rate;
* a **circuit breaker** that fails fast when Discord is unhealthy;
* **exponential backoff with jitter** on transient REST failures;
* an **idempotency key** recorded per ``(guild, message, action)`` so a
  redelivered verdict never double-bans.

Actions are applied as **independent steps**. Deletion and punishment used to
run as one retried unit, so a refused delete (no ``Manage Messages`` in that
channel) aborted the method before the ban was ever attempted -- a scammer kept
posting because the bot could not clean up one channel. Each step now succeeds
or fails on its own, and every outcome is reported, so enforcement degrades
gracefully instead of collapsing.

The Discord surface is abstracted behind :class:`RestActions` so the executor is
testable without a live gateway. DM warnings are rate-limited per user via a
:class:`~optimus.services.moderation.cooldown.Cooldown`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from prometheus_client import Counter, Gauge

from optimus.contracts.events import Action
from optimus.core.backoff import BackoffPolicy
from optimus.core.circuit import CircuitBreaker, CircuitState
from optimus.core.logging import get_logger
from optimus.core.ratelimit import RateLimit, RateLimiter
from optimus.i18n import translate
from optimus.services.moderation import permissions as perms
from optimus.services.moderation.cooldown import Cooldown
from optimus.services.moderation.failures import Failure, FailureKind, classify
from optimus.services.moderation.permissions import PermissionProbe

_log = get_logger(__name__)

#: Failure kinds where waiting and retrying can plausibly change the answer.
#: Permission refusals are excluded on purpose: they cannot resolve inside a
#: backoff window, so retrying them only burns rate limit and delays the report.
_RETRYABLE = frozenset(
    {
        FailureKind.RATE_LIMITED,
        FailureKind.TRANSIENT,
        FailureKind.UNKNOWN,
    }
)


# 0/1/2 encode closed/half_open/open so a dashboard can alert on "> 0".
_CIRCUIT_STATE_CODE = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}

CIRCUIT_STATE = Gauge(
    "optimus_moderation_circuit_state",
    "Discord REST circuit breaker state (0=closed, 1=half_open, 2=open).",
)
CIRCUIT_TRANSITIONS = Counter(
    "optimus_moderation_circuit_transitions_total",
    "Discord REST circuit breaker state transitions.",
    ["from_state", "to_state"],
)


def _observe_circuit_transition(previous: CircuitState, current: CircuitState) -> None:
    """Record a breaker state change as a metric and a structured log line."""
    CIRCUIT_STATE.set(_CIRCUIT_STATE_CODE[current])
    CIRCUIT_TRANSITIONS.labels(from_state=previous.value, to_state=current.value).inc()
    _log.warning(
        "moderation_circuit_state_changed",
        from_state=previous.value,
        to_state=current.value,
    )


def render_dm(locale: str, *, guild: str) -> str:
    """Render the localized DM warning from the i18n catalog (English fallback)."""
    return translate("dm.warning", locale, guild=guild)


class RestActions(Protocol):
    """The minimal Discord REST surface the executor depends on."""

    async def delete_message(self, channel_id: int, message_id: int) -> None: ...

    async def timeout_member(self, guild_id: int, user_id: int, seconds: int) -> None: ...

    async def kick_member(self, guild_id: int, user_id: int, reason: str) -> None: ...

    async def ban_member(
        self, guild_id: int, user_id: int, reason: str, purge_seconds: int = 0
    ) -> None: ...

    async def unban_member(self, guild_id: int, user_id: int, reason: str) -> None: ...

    async def send_dm(self, user_id: int, content: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """A fully-resolved request to apply one action to one detection."""

    guild_id: int
    channel_id: int
    message_id: int
    uploader_id: int
    action: Action
    idempotency_key: str
    guild_name: str = ""
    locale: str = "en"
    timeout_seconds: int = 3600
    reason: str = "Automated scam-image removal"
    #: Discord-native "delete message history" window applied with a ban: the
    #: banned user's messages from the last N seconds are purged in ALL
    #: channels (like the manual ban dialog). 0 disables the purge.
    ban_purge_seconds: int = 0


class Step(StrEnum):
    """An independently-applied part of an action."""

    DELETE = "delete"
    TIMEOUT = "timeout"
    KICK = "kick"
    BAN = "ban"
    DM = "dm"


#: The punitive step each action carries, if any.
_PUNITIVE_STEP: dict[Action, Step] = {
    Action.DELETE_TIMEOUT: Step.TIMEOUT,
    Action.DELETE_KICK: Step.KICK,
    Action.DELETE_BAN: Step.BAN,
}


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


class ActionExecutor:
    """Applies moderation actions with rate-limiting, breaker, backoff, idempotency."""

    def __init__(
        self,
        rest: RestActions,
        rate_limiter: RateLimiter,
        *,
        bot_user_id: int,
        rate: RateLimit,
        idempotency_acquire: object,
        dm_cooldown: Cooldown,
        breaker: CircuitBreaker | None = None,
        backoff: BackoffPolicy | None = None,
        probe: PermissionProbe | None = None,
    ) -> None:
        self._rest = rest
        self._probe = probe
        self._rl = rate_limiter
        self._bot_user_id = bot_user_id
        self._rate = rate
        self._acquire = idempotency_acquire
        self._dm_cooldown = dm_cooldown
        self._breaker = breaker or CircuitBreaker()
        # Attach metrics/logging to whichever breaker is used, including a
        # caller-injected one; add_state_listener is idempotent so this never
        # double-fires if the breaker was already wired with the observer.
        self._breaker.add_state_listener(_observe_circuit_transition)
        self._backoff = backoff or BackoffPolicy(max_attempts=3)
        # CIRCUIT_STATE is a process-global gauge; this assumes one executor per
        # process (the standard single-service-per-process deployment).
        CIRCUIT_STATE.set(_CIRCUIT_STATE_CODE[self._breaker.state])

    def attach_probe(self, probe: PermissionProbe) -> None:
        """Install the permission probe after construction.

        The gateway cache the probe reads only exists once the gateway client is
        created, which happens after the executor is wired. Late attachment is
        therefore the honest option; until it happens the executor simply has no
        probe and attempts every action, which is the previous behaviour.
        """
        self._probe = probe

    async def execute(self, req: ActionRequest) -> ActionResult:
        """Apply ``req`` exactly once, returning the outcome of every step.

        Returns a ``success=False`` result (rather than raising) on rate-limit
        exhaustion, idempotency replay, or REST failure so the caller can record
        an audit row in every case. Steps are independent: a refused delete no
        longer prevents the ban, and the result carries both outcomes.
        """
        if req.action in (Action.NONE, Action.REPORT_ONLY):
            return ActionResult(req.action, success=True, detail="no_enforcement")

        if not await self._acquire(req.idempotency_key):  # type: ignore[operator]
            return ActionResult(req.action, success=False, detail="duplicate")

        if not await self._rl.acquire(f"modact:{req.guild_id}", self._rate):
            return ActionResult(req.action, success=False, detail="rate_limited")

        steps = await self._apply(req)
        failed = tuple(s for s in steps if not s.success and s.step is not Step.DM)
        detail = failed[0].failure.detail if failed and failed[0].failure else None
        if failed:
            # An enforcement failure must be loud, and it must name a cause an
            # admin can act on rather than just an exception class.
            _log.warning(
                "moderation_action_failed",
                action=req.action.value,
                guild_id=req.guild_id,
                channel_id=req.channel_id,
                message_id=req.message_id,
                uploader_id=req.uploader_id,
                failed_steps=[s.step.value for s in failed],
                causes=[s.failure.detail for s in failed if s.failure],
                missing=[name for s in failed for name in s.missing],
            )
        return ActionResult(req.action, success=not failed, detail=detail, steps=steps)

    async def _apply(self, req: ActionRequest) -> tuple[StepOutcome, ...]:
        """Run every step of ``req`` independently, collecting outcomes.

        The delete runs first (it is the visible harm) but its result never
        gates the punitive step: when the bot cannot clean a channel, the
        offender is still removed, which is what stops the campaign.
        """
        outcomes: list[StepOutcome] = [await self._delete(req)]
        punitive = _PUNITIVE_STEP.get(req.action)
        if punitive is not None:
            outcomes.append(await self._punish(req, punitive))
        # Only warn the user if something actually happened to them. Telling
        # someone their message was removed when nothing was enforced is both
        # false and a wasted request during an outage or permission gap.
        if any(o.success for o in outcomes):
            dm = await self._maybe_dm(req)
            if dm is not None:
                outcomes.append(dm)
        return tuple(outcomes)

    async def _delete(self, req: ActionRequest) -> StepOutcome:
        preflight = (
            await perms.preflight_delete(self._probe, req.guild_id, req.channel_id)
            if self._probe is not None
            else perms.PreflightResult(ok=True)
        )
        if not preflight.ok:
            # Skipping is not giving up: the outcome records a recoverable
            # permission gap, so the report names the fix and a later
            # permission grant can complete the cleanup.
            _log.info(
                "moderation_delete_skipped",
                guild_id=req.guild_id,
                channel_id=req.channel_id,
                missing=list(preflight.missing),
            )
            return StepOutcome(
                Step.DELETE,
                success=False,
                failure=preflight.failure,
                skipped=True,
                missing=preflight.missing,
            )
        return await self._attempt(
            Step.DELETE,
            req,
            lambda: self._rest.delete_message(req.channel_id, req.message_id),
        )

    async def _punish(self, req: ActionRequest, step: Step) -> StepOutcome:
        preflight = (
            await perms.preflight_punitive(self._probe, req.guild_id, req.action)
            if self._probe is not None
            else perms.PreflightResult(ok=True)
        )
        if not preflight.ok:
            _log.info(
                "moderation_punitive_skipped",
                guild_id=req.guild_id,
                action=req.action.value,
                missing=list(preflight.missing),
            )
            return StepOutcome(
                step,
                success=False,
                failure=preflight.failure,
                skipped=True,
                missing=preflight.missing,
            )
        return await self._attempt(step, req, lambda: self._punitive_call(req, step))

    def _punitive_call(self, req: ActionRequest, step: Step) -> Awaitable[None]:
        if step is Step.TIMEOUT:
            return self._rest.timeout_member(req.guild_id, req.uploader_id, req.timeout_seconds)
        if step is Step.KICK:
            return self._rest.kick_member(req.guild_id, req.uploader_id, req.reason)
        return self._rest.ban_member(
            req.guild_id, req.uploader_id, req.reason, purge_seconds=req.ban_purge_seconds
        )

    async def _attempt(
        self,
        step: Step,
        req: ActionRequest,
        call: Callable[[], Awaitable[None]],
    ) -> StepOutcome:
        """Run one REST call, retrying only what a retry could actually fix.

        Retrying a permission refusal is pure waste -- the answer cannot change
        within a backoff window -- so only rate limits and Discord-side faults
        are retried. Everything else returns its classified cause immediately.
        """
        root: Failure | None = None
        last: Failure | None = None
        for attempt in range(self._backoff.max_attempts):
            try:
                await self._breaker.call(call)
            except Exception as exc:
                last = classify(exc)
                if root is None and last.kind is not FailureKind.CIRCUIT_OPEN:
                    # Keep the first real cause: once a retry trips the breaker,
                    # "circuit_open" would otherwise bury the actual reason the
                    # call failed, which is what an admin needs to see.
                    root = last
                if last.satisfied:
                    # The goal already holds (message gone, user already
                    # banned): success, not failure.
                    _log.debug(
                        "moderation_step_already_satisfied",
                        step=step.value,
                        guild_id=req.guild_id,
                        cause=last.detail,
                    )
                    return StepOutcome(step, success=True, failure=last)
                if last.kind not in _RETRYABLE:
                    return StepOutcome(step, success=False, failure=root or last)
                if attempt + 1 >= self._backoff.max_attempts:
                    break
                await asyncio.sleep(self._backoff.delay(attempt))
                continue
            return StepOutcome(step, success=True)
        return StepOutcome(step, success=False, failure=root or last)

    async def _maybe_dm(self, req: ActionRequest) -> StepOutcome | None:
        """Best-effort warning DM. Never affects whether enforcement succeeded."""
        if req.uploader_id == self._bot_user_id:
            return None
        if not await self._dm_cooldown.acquire(str(req.uploader_id)):
            return None
        content = render_dm(req.locale, guild=req.guild_name or str(req.guild_id))
        try:
            await self._rest.send_dm(req.uploader_id, content)
        except Exception as exc:
            # Closed DMs are routine; log without a stack trace at debug level so
            # a systematic delivery failure is still observable per guild.
            failure = classify(exc)
            _log.debug(
                "moderation_dm_failed",
                guild_id=req.guild_id,
                error=type(exc).__name__,
                cause=failure.detail,
            )
            return StepOutcome(Step.DM, success=False, failure=failure)
        return StepOutcome(Step.DM, success=True)
