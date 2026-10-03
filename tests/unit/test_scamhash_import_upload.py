"""``/scamhash import`` as Discord actually delivers it.

Discord sends an ATTACHMENT option's value as the attachment's snowflake id;
the file itself rides on ``interaction.resolved.attachments`` and has to be
downloaded. Until this was wired, the handler was given the id, which
``json.loads`` happily parses as a number, so every real upload was reported
as an invalid import. The handler tests feed JSON straight into the option and
never saw it; these go through the resolver the live bot uses.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from optimus.db.models import GuildHash
from optimus.ingest.fetcher import FetchError
from optimus.ingest.ssrf import SSRFError
from optimus.services.interactions.handlers import InteractionContext, handle_command
from optimus.services.interactions.logic import (
    MAX_IMPORT_BYTES,
    CommandError,
    InteractionRejected,
)
from optimus.services.interactions.service import _resolve_import_options, error_message
from tests.unit.test_interactions_handlers import FakeDeps, _ctx

ATTACHMENT_ID = 1423456789012345678


def _import_ctx(file_value: Any) -> InteractionContext:
    return _ctx("scamhash", subcommand="import", file=file_value)


def _upload(body: bytes, *, size: int | None = None) -> SimpleNamespace:
    """An interaction carrying one resolved attachment, shaped like hikari's."""
    attachment = SimpleNamespace(
        id=ATTACHMENT_ID,
        url=f"https://cdn.discordapp.com/attachments/1/{ATTACHMENT_ID}/scamhash-export.json",
        size=len(body) if size is None else size,
        media_type="application/json; charset=utf-8",
    )
    return SimpleNamespace(resolved=SimpleNamespace(attachments={ATTACHMENT_ID: attachment}))


class _Fetch:
    def __init__(self, body: bytes = b"", error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.urls: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.body


async def _export_from(*rows: tuple[int, int, int]) -> bytes:
    source = FakeDeps()
    for n, (p, d, w) in enumerate(rows):
        source.hashes[f"h{n}"] = GuildHash(
            hash_id=f"h{n}", phash=p, dhash=d, whash=w, ahash=0, source="local"
        )
    resp = await handle_command(_ctx("scamhash", subcommand="export"), source)
    assert resp.i18n_key == "command.export_ok"
    assert isinstance(resp.attachment, str)
    return resp.attachment.encode("utf-8")


async def test_export_from_one_server_imports_on_another() -> None:
    """The reported case, end to end: export, upload, import elsewhere."""
    body = await _export_from((10, 11, 12), (20, 21, 22), (30, 31, 32))
    fetch = _Fetch(body)

    ctx = await _resolve_import_options(_import_ctx(ATTACHMENT_ID), _upload(body), fetch=fetch)
    target = FakeDeps()
    resp = await handle_command(ctx, target)

    assert resp.i18n_key == "command.import_ok"
    assert resp.params == {"added": 3, "skipped": 0}
    assert sorted(h.phash for h in target.hashes.values()) == [10, 20, 30]
    assert fetch.urls == [_upload(body).resolved.attachments[ATTACHMENT_ID].url]


async def test_resolver_hands_the_handler_file_bytes_not_the_attachment_id() -> None:
    body = await _export_from((1, 2, 3))
    ctx = await _resolve_import_options(
        _import_ctx(ATTACHMENT_ID), _upload(body), fetch=_Fetch(body)
    )
    assert ctx.options == {"file": body}
    assert ctx.subcommand == "import"


async def test_handler_never_parses_an_unresolved_attachment_id() -> None:
    """A bare id is valid JSON; it must not be reported as a malformed export."""
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(_import_ctx(ATTACHMENT_ID), FakeDeps())
    assert exc.value.reason is CommandError.IMPORT_DOWNLOAD_FAILED


async def test_import_accepts_a_file_saved_with_a_byte_order_mark() -> None:
    body = b"\xef\xbb\xbf" + await _export_from((5, 6, 7))
    ctx = await _resolve_import_options(
        _import_ctx(ATTACHMENT_ID), _upload(body), fetch=_Fetch(body)
    )
    resp = await handle_command(ctx, FakeDeps())
    assert resp.params == {"added": 1, "skipped": 0}


async def test_oversized_upload_is_refused_before_downloading() -> None:
    fetch = _Fetch(b"{}")
    with pytest.raises(InteractionRejected) as exc:
        await _resolve_import_options(
            _import_ctx(ATTACHMENT_ID), _upload(b"", size=MAX_IMPORT_BYTES + 1), fetch=fetch
        )
    assert exc.value.reason is CommandError.IMPORT_FILE_TOO_BIG
    assert exc.value.params == {"limit_kb": MAX_IMPORT_BYTES // 1024}
    assert fetch.urls == []


@pytest.mark.parametrize("error", [FetchError("unexpected status 404"), SSRFError("blocked")])
async def test_download_failure_is_reported_and_logged(error: Exception) -> None:
    with capture_logs() as logs, pytest.raises(InteractionRejected) as exc:
        await _resolve_import_options(
            _import_ctx(ATTACHMENT_ID), _upload(b"{}"), fetch=_Fetch(error=error)
        )
    assert exc.value.reason is CommandError.IMPORT_DOWNLOAD_FAILED
    event = next(e for e in logs if e["event"] == "scamhash_import_download_failed")
    assert event["error_type"] == type(error).__name__


@pytest.mark.parametrize(
    "interaction",
    [
        SimpleNamespace(resolved=None),
        SimpleNamespace(resolved=SimpleNamespace(attachments={})),
        SimpleNamespace(resolved=SimpleNamespace(attachments={42: SimpleNamespace(url="x")})),
    ],
)
async def test_missing_attachment_is_a_download_failure(interaction: Any) -> None:
    with pytest.raises(InteractionRejected) as exc:
        await _resolve_import_options(_import_ctx(ATTACHMENT_ID), interaction, fetch=_Fetch())
    assert exc.value.reason is CommandError.IMPORT_DOWNLOAD_FAILED


# --- what the moderator is told ------------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason", "params"),
    [
        (b"not json at all", CommandError.IMPORT_NOT_JSON, {}),
        (b"[1, 2, 3]", CommandError.IMPORT_INVALID, {}),
        (b'{"version": 1, "hashes": []}', CommandError.IMPORT_EMPTY, {}),
        (
            json.dumps(
                {"version": 1, "hashes": [{"phash": 1, "dhash": 2, "whash": 3}, {"phash": 1}]}
            ).encode(),
            CommandError.IMPORT_BAD_ENTRY,
            {"entry": 2},
        ),
    ],
)
async def test_bad_files_get_a_specific_reason(
    body: bytes, reason: CommandError, params: dict[str, Any]
) -> None:
    ctx = await _resolve_import_options(
        _import_ctx(ATTACHMENT_ID), _upload(body), fetch=_Fetch(body)
    )
    with pytest.raises(InteractionRejected) as exc:
        await handle_command(ctx, FakeDeps())
    assert exc.value.reason is reason
    assert exc.value.params == params


@pytest.mark.parametrize("locale", ["en", "sr"])
@pytest.mark.parametrize(
    ("reason", "params", "expected"),
    [
        (CommandError.IMPORT_INVALID, {}, "/scamhash export"),
        (CommandError.IMPORT_NOT_JSON, {}, "JSON"),
        (CommandError.IMPORT_EMPTY, {}, ""),
        (CommandError.IMPORT_BAD_ENTRY, {"entry": 7}, "7"),
        (CommandError.IMPORT_TOO_LARGE, {"limit": 1000}, "1000"),
        (CommandError.IMPORT_FILE_TOO_BIG, {"limit_kb": 1024}, "1024"),
        (CommandError.IMPORT_DOWNLOAD_FAILED, {}, "Discord"),
    ],
)
def test_import_errors_read_as_sentences_not_codes(
    locale: str, reason: CommandError, params: dict[str, Any], expected: str
) -> None:
    text = error_message(reason, locale, params)
    assert reason.value not in text  # never the bare code, as in the report
    assert "{" not in text  # every placeholder filled
    assert expected in text


def test_error_message_without_params_still_fills_placeholders() -> None:
    for reason in (
        CommandError.IMPORT_BAD_ENTRY,
        CommandError.IMPORT_TOO_LARGE,
        CommandError.IMPORT_FILE_TOO_BIG,
    ):
        assert "{" not in error_message(reason, "en")
