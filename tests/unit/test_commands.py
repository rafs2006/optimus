"""Tests for the slash-command tree -> hikari builder adapter."""

from __future__ import annotations

import hikari
import pytest

from optimus.services.interactions.commands import (
    COMMANDS,
    build_command_builders,
    required_permission,
)
from optimus.services.interactions.logic import (
    CONFIG_FIELDS,
    InteractionRejected,
    Permission,
    validate_config_set,
)


def test_build_command_builders_covers_every_declared_command() -> None:
    builders = build_command_builders()
    assert {b.name for b in builders} == {c.name for c in COMMANDS}


def test_builder_sets_permissions_and_guild_only_context() -> None:
    builders = {b.name: b for b in build_command_builders()}

    scamhash = builders["scamhash"]
    assert scamhash.default_member_permissions == int(Permission.MANAGE_GUILD)
    assert hikari.ApplicationContextType.GUILD in scamhash.context_types
    assert hikari.ApplicationContextType.BOT_DM not in scamhash.context_types

    # /forget_me is permission-free and usable in DMs.
    forget = builders["forget_me"]
    assert hikari.ApplicationContextType.BOT_DM in forget.context_types


def test_builder_expands_subcommands_and_their_options() -> None:
    scamhash = next(b for b in build_command_builders() if b.name == "scamhash")
    subs = {opt.name: opt for opt in scamhash.options}
    assert "add" in subs
    assert subs["add"].type == hikari.OptionType.SUB_COMMAND
    # Adding is image-only: the confusing typed-hex options (phash/dhash/whash)
    # were removed -- bulk/hex exchange goes through import/export instead.
    add_opts = {o.name: o for o in (subs["add"].options or [])}
    assert set(add_opts) == {"image"}
    assert add_opts["image"].is_required is True


def test_review_subcommand_replaces_reviewmsg() -> None:
    scamhash = next(c for c in COMMANDS if c.name == "scamhash")
    subs = {s.name for s in scamhash.subcommands}
    assert "review" in subs
    assert "reviewmsg" not in subs


def test_config_set_field_offers_every_settable_field_as_a_choice() -> None:
    """``/config set field:`` is a picker of exactly the fields the validator accepts."""
    config = next(b for b in build_command_builders() if b.name == "config")
    set_sub = next(o for o in config.options if o.name == "set")
    field_opt = next(o for o in (set_sub.options or []) if o.name == "field")
    assert [c.value for c in (field_opt.choices or [])] == list(CONFIG_FIELDS)


def test_config_field_choices_match_the_validator() -> None:
    """Every advertised choice validates; anything else is UNKNOWN_FIELD."""
    samples = {
        "sensitivity": "strict",
        "action_policy": "delete_ban",
        "mod_queue_threshold": "0.5",
        "retention_days": "14",
        "ban_purge_hours": "24",
        "locale": "en",
        "review_channel": "<#123>",
        "optin_global_db": "true",
        "optin_scan_bots": "false",
        "optin_evidence_storage": "yes",
        "safe_mode": "off",
    }
    assert set(samples) == set(CONFIG_FIELDS)
    for field, value in samples.items():
        assert validate_config_set(field, value).field == field
    with pytest.raises(InteractionRejected):
        validate_config_set("not_a_field", "x")


def test_builder_carries_required_flag_on_top_level_options() -> None:
    submit = next(b for b in build_command_builders() if b.name == "submit_global")
    hash_opt = next(o for o in submit.options if o.name == "hash_id")
    assert hash_opt.is_required is True


def test_required_permission_lookup() -> None:
    assert required_permission("scamhash") is Permission.MANAGE_GUILD
    assert required_permission("delete_server_data") is Permission.ADMINISTRATOR
    assert required_permission("appeal") is None
    assert required_permission("does_not_exist") is None
