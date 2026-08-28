"""Tests for the slash-command tree -> hikari builder adapter."""

from __future__ import annotations

import hikari
import pytest
from pydantic import ValidationError

from optimus.services.interactions.commands import (
    COMMANDS,
    MEMBER_COMMANDS,
    build_command_builders,
    is_enabled,
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


def test_builder_carries_required_flag_on_options() -> None:
    # Top-level optional options stay optional (/setup mod_role/channel)...
    setup = next(b for b in build_command_builders() if b.name == "setup")
    assert setup.options and all(o.is_required is False for o in setup.options)
    # ...and required options nested under subcommands stay required.
    global_cmd = next(b for b in build_command_builders() if b.name == "global")
    approve = next(o for o in global_cmd.options if o.name == "approve_server")
    assert approve.options is not None
    server_opt = next(o for o in approve.options if o.name == "server_id")
    assert server_opt.is_required is True


def test_submit_global_removed_and_global_help_present() -> None:
    names = {c.name for c in COMMANDS}
    assert "submit_global" not in names
    assert {"global", "help"} <= names
    help_cmd = next(c for c in COMMANDS if c.name == "help")
    assert help_cmd.guild_only is False
    assert help_cmd.required_permission is None
    global_cmd = next(c for c in COMMANDS if c.name == "global")
    assert {s.name for s in global_cmd.subcommands} == {
        "approve_server",
        "revoke_server",
        "servers",
    }


def test_required_permission_lookup() -> None:
    assert required_permission("scamhash") is Permission.MANAGE_GUILD
    assert required_permission("delete_server_data") is Permission.ADMINISTRATOR
    assert required_permission("appeal") is None
    assert required_permission("does_not_exist") is None


def test_member_commands_set_is_exactly_the_permissionless_commands() -> None:
    assert {"report", "appeal", "forget_me", "help"} == MEMBER_COMMANDS


def test_is_enabled_defaults_to_exposing_everything() -> None:
    # ``None`` is the shipped default, so an operator who never sets
    # OPTIMUS_MEMBER_COMMANDS sees no behavioural change at all.
    for cmd in COMMANDS:
        assert is_enabled(cmd.name, None) is True


def test_member_commands_narrows_only_the_member_surface() -> None:
    narrowed = ("report",)
    assert is_enabled("report", narrowed) is True
    assert is_enabled("appeal", narrowed) is False
    assert is_enabled("forget_me", narrowed) is False
    assert is_enabled("help", narrowed) is False
    # Moderator and admin commands are out of this setting's reach entirely, so
    # no value here can ever take /scamhash or /config away from mods.
    assert is_enabled("scamhash", narrowed) is True
    assert is_enabled("config", narrowed) is True
    assert is_enabled("delete_server_data", narrowed) is True


def test_builders_omit_disabled_member_commands() -> None:
    names = {b.name for b in build_command_builders(("report",))}
    assert "report" in names
    assert names.isdisjoint({"appeal", "forget_me", "help"})
    assert {"scamhash", "config", "setup", "stats", "global"} <= names


def test_empty_member_commands_hides_every_member_command() -> None:
    names = {b.name for b in build_command_builders(())}
    assert names.isdisjoint(MEMBER_COMMANDS)
    assert {c.name for c in COMMANDS} - names == MEMBER_COMMANDS


def test_settings_parses_and_validates_member_commands() -> None:
    from optimus.core.config import Settings

    assert Settings(discord_token="t").member_commands is None
    assert Settings(discord_token="t", member_commands="").member_commands is None
    # Slashes, whitespace and duplicates are all tolerated in the env value.
    assert Settings(
        discord_token="t", member_commands=" /report , report,  help "
    ).member_commands == ("report", "help")
    # A moderator command cannot be smuggled in, and a typo is a hard error
    # rather than a silent no-op that leaves the command exposed.
    for bad in ("report,scamhash", "reprot"):
        with pytest.raises(ValidationError):
            Settings(discord_token="t", member_commands=bad)


def test_member_commands_error_explains_the_moderator_split() -> None:
    """The two rejection reasons read differently.

    An operator who names ``/scamhash`` has not made a typo -- they asked for
    something the setting structurally cannot do, so the error has to say that
    moderator commands sit outside its reach rather than only listing the
    member-facing names.
    """
    from optimus.core.config import Settings

    with pytest.raises(ValidationError) as privileged:
        Settings(discord_token="t", member_commands="report,scamhash")
    message = str(privileged.value)
    assert "moderator command" in message
    assert "MANAGE_GUILD" in message
    assert "narrows the member-facing surface only" in message

    with pytest.raises(ValidationError) as typo:
        Settings(discord_token="t", member_commands="reprot")
    typo_message = str(typo.value)
    assert "is not a member-facing command" in typo_message
    # A typo is not a permission problem, so it must not claim to be one.
    assert "moderator command" not in typo_message
