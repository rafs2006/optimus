"""Detection worker: decode -> hash -> match -> (swarm) -> verdict.

Stateless per-image logic, independent of the bus runtime. Idempotency is
checked first so retries never re-emit. Decode failures yield a NON_DECISION
(the pipeline never acts on an image it could not safely read). The guild
whitelist is consulted before any scam match. Swarm correlation may escalate a
positive verdict one confidence band and produce a ``swarm_alert``.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from prometheus_client import Counter

from optimus.contracts.events import (
    HashSet,
    ImageFetchedEvent,
    OcrFindings,
    SwarmAlertEvent,
    Verdict,
    VerdictEvent,
)
from optimus.core.config import Sensitivity
from optimus.core.logging import get_logger
from optimus.hashing import perceptual
from optimus.hashing.decoder import DecodedImage, DecodeLimits, decode
from optimus.services.detection.index import HashIndex
from optimus.services.detection.matcher import (
    MatchOutcome,
    WhitelistEntry,
    escalate_band,
    match,
)
from optimus.services.detection.swarm import SwarmCorrelator

_log = get_logger(__name__)

VERDICTS_EMITTED = Counter(
    "optimus_detection_verdicts_total",
    "Verdicts emitted by the detection worker.",
    ["verdict"],
)
DUPLICATE_SKIPPED = Counter(
    "optimus_detection_duplicate_skipped_total",
    "Images skipped because their idempotency key was already claimed.",
)
PAYLOAD_REJECTED = Counter(
    "optimus_detection_payload_rejected_total",
    "Image payloads rejected before decode (resolved as non-decisions).",
    ["reason"],
)
RISK_ESCALATED = Counter(
    "optimus_detection_risk_escalated_total",
    "Hash-clean images escalated to the mod queue by the OCR/QR risk scan.",
    ["risk"],
)


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """The worker's output for one image: a verdict plus optional swarm alert."""

    verdict: VerdictEvent
    swarm_alert: SwarmAlertEvent | None = None


# Async hooks the worker depends on, injected for testability.
GuildIndexFn = Callable[[int], Awaitable[HashIndex]]
GlobalIndexFn = Callable[[], Awaitable[HashIndex]]
WhitelistFn = Callable[[int], Awaitable[list[WhitelistEntry]]]
SensitivityFn = Callable[[int], Awaitable[Sensitivity]]
#: Per-guild ``optin_global_db`` lookup: only opted-in guilds are matched
#: against the shared global hash set.
GlobalOptinFn = Callable[[int], Awaitable[bool]]
IdempotencyAcquire = Callable[[str], Awaitable[bool]]


def all_frame_hashes(image: DecodedImage) -> list[dict[str, int]]:
    """Compute the hash set for every sampled frame."""
    return [perceptual.compute_all(frame) for frame in image.frames]


class DetectionWorker:
    """Per-image detection logic with injected index/whitelist/idempotency hooks."""

    def __init__(
        self,
        *,
        guild_index: GuildIndexFn,
        global_index: GlobalIndexFn,
        whitelist: WhitelistFn,
        sensitivity: SensitivityFn,
        idempotency_acquire: IdempotencyAcquire,
        global_optin: GlobalOptinFn | None = None,
        swarm: SwarmCorrelator | None = None,
        limits: DecodeLimits | None = None,
        risk_scan: Callable[[bytes], OcrFindings | None] | None = None,
    ) -> None:
        self._guild_index = guild_index
        self._global_index = global_index
        # None (e.g. legacy wiring in tests) preserves the old always-on
        # behavior; production wiring always passes the per-guild lookup.
        self._global_optin = global_optin
        self._whitelist = whitelist
        self._sensitivity = sensitivity
        self._acquire = idempotency_acquire
        self._swarm = swarm
        self._limits = limits
        self._risk_scan = risk_scan

    async def handle(self, event: ImageFetchedEvent) -> DetectionResult | None:
        """Process one fetched image; return a verdict, or ``None`` if a duplicate."""
        if not await self._acquire(event.idempotency_key):
            DUPLICATE_SKIPPED.inc()
            return None

        # A malformed/oversized inline payload must resolve as a non-decision, not
        # raise — an exception here would nak the message and redeliver the same
        # bad payload until ``max_deliver``, a pure waste under a hostile flood.
        try:
            data = base64.b64decode(event.data_b64, validate=True)
        except ValueError:  # includes binascii.Error (malformed base64)
            PAYLOAD_REJECTED.labels(reason="decode").inc()
            _log.warning("detection_payload_rejected", reason="decode")
            return DetectionResult(verdict=self._verdict(event, _non_decision()))
        # Decode (subprocess wall-wait) and perceptual hashing (numpy/Python,
        # up to max_frames frames) are both blocking and CPU/IO-bound. Run them
        # off the event loop so the NATS consumer loop and health server stay
        # responsive while one image is processed.
        frames = await asyncio.to_thread(self._decode_and_hash, data)
        if frames is None:
            return DetectionResult(verdict=self._verdict(event, _non_decision()))

        guild_idx = await self._guild_index(event.guild_id)
        # optin_global_db is enforced *here*, at match time: a guild that has
        # not opted in is never compared against hashes other communities
        # promoted — consuming the shared set is symmetric with contributing.
        use_global = self._global_optin is None or await self._global_optin(event.guild_id)
        global_idx = await self._global_index() if use_global else HashIndex([])
        whitelist = await self._whitelist(event.guild_id)
        sensitivity = await self._sensitivity(event.guild_id)

        outcome, primary = self._best_frame_outcome(
            frames, guild_idx, global_idx, whitelist, sensitivity
        )

        # Second lane: images the hash index has never seen. OCR/QR risk-scan
        # them, and escalate high/critical findings to AMBIGUOUS -- which the
        # policy routes to the mod queue and never auto-acts on. Runs before
        # the swarm correlator so a cross-guild wave of the same OCR-flagged
        # image is correlated exactly like a hash-flagged one. Whitelisted
        # images and hash matches skip the (expensive) scan entirely.
        ocr: OcrFindings | None = None
        if (
            self._risk_scan is not None
            and not outcome.whitelisted
            and outcome.verdict in (Verdict.CLEAN, Verdict.NON_DECISION)
        ):
            findings = await asyncio.to_thread(self._risk_scan, data)
            if findings is not None and findings.risk_level in ("high", "critical"):
                ocr = findings
                outcome = MatchOutcome(
                    verdict=Verdict.AMBIGUOUS,
                    confidence=0.9 if findings.risk_level == "critical" else 0.75,
                    distances=outcome.distances,
                )
                RISK_ESCALATED.labels(risk=findings.risk_level).inc()
                _log.info(
                    "detection_risk_escalated",
                    guild_id=event.guild_id,
                    message_id=event.message_id,
                    risk_level=findings.risk_level,
                    risk_score=findings.risk_score,
                    signals=findings.signals,
                )

        swarm_alert: SwarmAlertEvent | None = None
        if (
            self._swarm is not None
            and not outcome.whitelisted
            and outcome.verdict in (Verdict.SCAM, Verdict.AMBIGUOUS)
        ):
            obs = await self._swarm.observe(primary["phash"], event.guild_id)
            if obs.is_swarming:
                new_verdict, new_conf = escalate_band(outcome.verdict, outcome.confidence)
                outcome = MatchOutcome(
                    verdict=new_verdict,
                    confidence=new_conf,
                    matched_hash_id=outcome.matched_hash_id,
                    matched_source=outcome.matched_source,
                    campaign_id=outcome.campaign_id,
                    distances=outcome.distances,
                )
                swarm_alert = SwarmAlertEvent(
                    correlation_id=event.correlation_id,
                    occurred_at=datetime.now(UTC),
                    phash=primary["phash"],
                    distinct_guilds=obs.distinct_guilds,
                    window_seconds=self._swarm.window_seconds,
                    sample_guild_ids=[event.guild_id],
                )

        verdict_event = self._verdict(event, outcome, hashes=primary, ocr=ocr)
        VERDICTS_EMITTED.labels(verdict=verdict_event.verdict.value).inc()
        return DetectionResult(verdict=verdict_event, swarm_alert=swarm_alert)

    def _decode_and_hash(self, data: bytes) -> list[dict[str, int]] | None:
        """Decode image bytes and hash every sampled frame (blocking; off-loop)."""
        image = decode(data, self._limits)
        if image is None:
            return None
        return all_frame_hashes(image)

    def _best_frame_outcome(
        self,
        frames: list[dict[str, int]],
        guild_idx: HashIndex,
        global_idx: HashIndex,
        whitelist: list[WhitelistEntry],
        sensitivity: Sensitivity,
    ) -> tuple[MatchOutcome, dict[str, int]]:
        """Match every frame; return the most incriminating outcome and its frame.

        The frame is returned alongside the outcome because downstream callers
        need the hashes of the frame that *drove* the verdict, not an arbitrary
        one: the swarm correlator observes its phash and the verdict event
        reports it. For a multi-frame image (e.g. an animation whose first frame
        is innocuous but a later frame is the scam) frame 0 is the wrong source.
        Ties within a verdict band are broken toward higher confidence.
        """
        rank = {Verdict.SCAM: 0, Verdict.AMBIGUOUS: 1, Verdict.CLEAN: 2, Verdict.NON_DECISION: 3}
        best: MatchOutcome | None = None
        best_frame = frames[0]
        for candidate in frames:
            outcome = match(
                candidate,
                guild_index=guild_idx,
                global_index=global_idx,
                whitelist=whitelist,
                sensitivity=sensitivity,
            )
            if outcome.whitelisted:
                return outcome, candidate  # whitelist always wins, immediately
            if best is None or (rank[outcome.verdict], -outcome.confidence) < (
                rank[best.verdict],
                -best.confidence,
            ):
                best, best_frame = outcome, candidate
        if best is None:
            best = MatchOutcome(verdict=Verdict.CLEAN, confidence=1.0)
        return best, best_frame

    def _verdict(
        self,
        event: ImageFetchedEvent,
        outcome: MatchOutcome,
        *,
        hashes: dict[str, int] | None = None,
        ocr: OcrFindings | None = None,
    ) -> VerdictEvent:
        hash_set = (
            HashSet(
                phash=hashes["phash"],
                dhash=hashes["dhash"],
                whash=hashes["whash"],
                ahash=hashes["ahash"],
            )
            if hashes is not None
            else None
        )
        return VerdictEvent(
            correlation_id=event.correlation_id,
            occurred_at=datetime.now(UTC),
            guild_id=event.guild_id,
            channel_id=event.channel_id,
            message_id=event.message_id,
            attachment_id=event.attachment_id,
            uploader_id=event.uploader_id,
            idempotency_key=event.idempotency_key,
            verdict=outcome.verdict,
            confidence=outcome.confidence,
            hashes=hash_set,
            matched_hash_id=outcome.matched_hash_id,
            matched_source=outcome.matched_source,
            ocr=ocr,
            source_url=event.source_url,
            distances=outcome.distances,
        )


def _non_decision() -> MatchOutcome:
    return MatchOutcome(verdict=Verdict.NON_DECISION, confidence=0.0)
