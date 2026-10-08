"""Audit-log reasons for enforcement actions.

Every enforcement action optimus takes has exactly one cause: a posted image
matched a known scam/phishing fingerprint. Discord's audit log is the only
place a moderator can later see *why* a member was removed, so that one cause
needs to be stated the same way every time, by every code path.

Before this module the automated path never set a reason at all and fell back
to :class:`~.actions.ActionRequest`'s bare default, while the manual review
path wrote its own differently-worded string -- two spellings of one cause, and
neither carried the match evidence. Both paths now build their reason here.

Two properties matter and are enforced by tests:

* **A stable prefix.** Every reason starts with :data:`REASON_PREFIX`, so
  filtering an audit-log export for optimus's actions is a prefix match rather
  than a guess. The prefix is deliberately English on every server: an audit
  log is a forensic record read by operators and shared across servers, and a
  localised prefix would make it ungreppable.
* **A hard length bound.** Discord caps ``X-Audit-Log-Reason`` at 512
  characters *after* URL encoding, and rejects the whole request when the
  header is over-long. Truncating an audit reason is strictly better than
  losing the ban, so :func:`clamp` measures the encoded length and trims.

The evidence clauses are ordered most- to least-useful, because truncation eats
the tail: what happened, then how sure, then which fingerprint, then where.
"""

from __future__ import annotations

from urllib.parse import quote

#: Discord's documented ceiling for ``X-Audit-Log-Reason``, measured on the
#: URL-encoded value rather than the raw string.
AUDIT_REASON_LIMIT = 512

#: Leading text shared by every reason this module produces. Never localise or
#: reword this without updating the operator docs -- it is what operators grep
#: audit-log exports for.
REASON_PREFIX = "Scam image"

_SEP = " \u00b7 "


def clamp(reason: str, limit: int = AUDIT_REASON_LIMIT) -> str:
    """Return ``reason`` shortened until its URL-encoded form fits ``limit``.

    Encoded length is what Discord measures, and a single non-ASCII character
    can expand to nine encoded characters, so counting raw characters would let
    an over-long header through. Trimming one character at a time would be
    quadratic on a pathological input, so this narrows by the observed overflow
    ratio first and then walks the last few characters off.
    """
    if len(quote(reason)) <= limit:
        return reason
    cut = reason
    while cut and len(quote(cut)) > limit:
        overflow = len(quote(cut)) - limit
        # Drop at least one character, and proportionally more while far over.
        cut = cut[: max(0, len(cut) - max(1, overflow // 3))]
    return cut


def _join(*clauses: str | None) -> str:
    return clamp(_SEP.join(c for c in clauses if c))


def _hash_clause(matched_hash_id: str | None) -> str | None:
    return f"hash {matched_hash_id}" if matched_hash_id else None


def auto_reason(
    *,
    confidence: float | None = None,
    matched_hash_id: str | None = None,
    matched_source: str | None = None,
    message_id: int | None = None,
) -> str:
    """Reason for an action optimus took on its own, with no moderator in the loop.

    Deliberately carries no detection id: in the automated path the detection
    row is written *after* enforcement returns, so no id exists yet at the
    moment the ban is issued. Naming one here would mean inventing it. The
    fingerprint id and message id are what make the action traceable instead,
    and both are already on the verdict.
    """
    return _join(
        f"{REASON_PREFIX} \u2014 optimus auto-enforced",
        None if confidence is None else f"conf {confidence:.2f}",
        _hash_clause(matched_hash_id),
        f"src {matched_source}" if matched_source else None,
        None if message_id is None else f"msg {message_id}",
    )


def confirmed_reason(detection_id: int, *, matched_hash_id: str | None = None) -> str:
    """Reason for an action a moderator confirmed on a review card or by command.

    Unlike the automated path this runs after the detection row exists, so the
    detection id is real and worth recording -- it is the join key back to the
    review card, the appeal, and the audit trail.
    """
    return _join(
        f"{REASON_PREFIX} \u2014 confirmed by moderator",
        f"det #{detection_id}",
        _hash_clause(matched_hash_id),
    )


def false_positive_reason(detection_id: int) -> str:
    """Reason for reversing an action a moderator judged to be a false positive."""
    return _reversal("false positive, action reversed", detection_id)


def manual_unban_reason(detection_id: int) -> str:
    """Reason for a moderator lifting a ban from the review card."""
    return _reversal("unbanned by moderator", detection_id)


def appeal_approved_reason(detection_id: int) -> str:
    """Reason for a ban lifted because the member's appeal was approved."""
    return _reversal("appeal approved", detection_id)


def _reversal(what: str, detection_id: int) -> str:
    """Shared shape for the three ways an action gets undone.

    Reversals keep the same prefix as the enforcement they undo, so one audit
    log filter shows a member's whole optimus history -- the removal and its
    reversal -- instead of only half of it.
    """
    return _join(f"{REASON_PREFIX} \u2014 {what}", f"det #{detection_id}")
