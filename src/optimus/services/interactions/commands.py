"""Slash command schema as pure data, plus a hikari builder adapter.

The command tree (names, descriptions, options, and the *server-side* permission
each command requires) is declared as plain dataclasses so it can be asserted on
in tests without importing hikari. :func:`build_command_builders` converts the
tree into hikari ``SlashCommandBuilder`` objects for registration.

``default_member_permissions`` is set on the builders purely as a client-side
convenience (it greys the command out for users who clearly lack access). It is
*never* the authorization boundary — see :data:`COMMAND_PERMISSIONS` and the
server-side re-check in :mod:`.service`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from optimus.services.interactions.logic import CONFIG_FIELDS, Permission

if TYPE_CHECKING:
    import hikari

# hikari OptionType numeric values (stable Discord API constants).
OPT_SUB_COMMAND = 1
OPT_STRING = 3
OPT_INTEGER = 4
OPT_BOOLEAN = 5
OPT_USER = 6
OPT_CHANNEL = 7
OPT_ROLE = 8
OPT_ATTACHMENT = 11


@dataclass(frozen=True, slots=True)
class Option:
    """One command (or subcommand) option.

    ``choices`` (STRING options only) turns free-text input into a Discord
    picker so users cannot typo a value the handler would reject.
    """

    name: str
    description: str
    type: int
    required: bool = False
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubCommand:
    """A subcommand under a top-level command."""

    name: str
    description: str
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True)
class Command:
    """A top-level slash command.

    ``required_permission`` is the permission the *service* enforces server-side
    for every (sub)command under it; ``None`` means any guild member may run it
    (e.g. ``/appeal``, ``/forget_me``). ``guild_only`` commands are refused in
    DMs even before the permission check.
    """

    name: str
    description: str
    required_permission: Permission | None = None
    guild_only: bool = True
    options: tuple[Option, ...] = ()
    subcommands: tuple[SubCommand, ...] = ()


COMMANDS: tuple[Command, ...] = (
    Command(
        name="scamhash",
        description="Manage this server's scam-image blocklist.",
        required_permission=Permission.MANAGE_GUILD,
        subcommands=(
            SubCommand(
                name="add",
                description="Block a scam image: attach it and future reposts get caught.",
                options=(
                    Option(
                        "image",
                        "The scam image to block (screenshot or saved copy).",
                        OPT_ATTACHMENT,
                        required=True,
                    ),
                ),
            ),
            SubCommand(
                name="remove",
                description="Unblock an image by its hash id (shown by /scamhash list).",
                options=(
                    Option(
                        "hash_id",
                        "The hash id to remove — copy it from /scamhash list.",
                        OPT_STRING,
                        required=True,
                    ),
                ),
            ),
            SubCommand(
                name="list",
                description="Show the scam images blocked on this server.",
            ),
            SubCommand(
                name="import",
                description="Load blocked hashes from a file made by /scamhash export.",
                options=(
                    Option(
                        "file",
                        "A JSON file created by /scamhash export on another server.",
                        OPT_ATTACHMENT,
                        required=True,
                    ),
                ),
            ),
            SubCommand(
                name="export",
                description="Download this server's blocked hashes as a JSON file.",
            ),
            SubCommand(
                name="review",
                description="Mark a posted message as scam: block its images, act on author.",
                options=(
                    Option(
                        "message",
                        "Message link or ID (right-click the message > Copy Message Link).",
                        OPT_STRING,
                        required=True,
                    ),
                ),
            ),
        ),
    ),
    Command(
        name="config",
        description="View or change this server's bot settings.",
        required_permission=Permission.MANAGE_GUILD,
        subcommands=(
            SubCommand(name="view", description="Show all current settings and their values."),
            SubCommand(
                name="permissions",
                description="List channels where the bot cannot enforce, and what it is missing.",
            ),
            SubCommand(
                name="set",
                description="Change one setting.",
                options=(
                    Option(
                        "field",
                        "The setting to change.",
                        OPT_STRING,
                        required=True,
                        choices=CONFIG_FIELDS,
                    ),
                    Option(
                        "value",
                        "The new value, e.g. true, delete_ban, 0.85, 14, #channel.",
                        OPT_STRING,
                        required=True,
                    ),
                ),
            ),
        ),
    ),
    Command(
        name="setup",
        description="Create the private mod-review channel where detections await approval.",
        required_permission=Permission.MANAGE_GUILD,
        options=(
            Option(
                "mod_role",
                "Moderator role that should see the review channel (admins always can).",
                OPT_ROLE,
            ),
            Option(
                "channel",
                "Use this existing channel for reviews instead of creating a new one.",
                OPT_CHANNEL,
            ),
        ),
    ),
    Command(
        name="stats",
        description="Show detection activity and database status for this server.",
        required_permission=Permission.MANAGE_GUILD,
    ),
    Command(
        name="global",
        # Owner-only at the handler (there is no Discord-side "app owner"
        # permission); MANAGE_GUILD here only hides it from ordinary members.
        description="Bot owner: manage which servers may contribute to the global scam list.",
        required_permission=Permission.MANAGE_GUILD,
        subcommands=(
            SubCommand(
                name="approve_server",
                description="Approve a server: its mods' scam confirmations count globally.",
                options=(
                    Option(
                        "server_id",
                        "The server (guild) ID to approve for global contribution.",
                        OPT_STRING,
                        required=True,
                    ),
                ),
            ),
            SubCommand(
                name="revoke_server",
                description="Remove a server from the approved contributors list.",
                options=(
                    Option(
                        "server_id",
                        "The server (guild) ID to remove.",
                        OPT_STRING,
                        required=True,
                    ),
                ),
            ),
            SubCommand(
                name="servers",
                description="List the servers approved to contribute global confirmations.",
            ),
        ),
    ),
    Command(
        name="help",
        description="What each command does and how the review workflow works.",
        required_permission=None,
        guild_only=False,
    ),
    Command(
        name="delete_server_data",
        description="Permanently delete ALL of this server's data (GDPR).",
        required_permission=Permission.ADMINISTRATOR,
    ),
    Command(
        name="forget_me",
        description="Erase your data and opt out of all processing.",
        required_permission=None,
        guild_only=False,
    ),
    Command(
        name="appeal",
        description="Appeal your most recent detection in this server.",
        required_permission=None,
    ),
    Command(
        name="report",
        description="Report a message with a scam image to this server's moderators.",
        required_permission=None,
        options=(
            Option(
                "message",
                "Link to the message (or its ID, if it is in this channel).",
                OPT_STRING,
                required=True,
            ),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ContextMenuCommand:
    """A Discord message-context-menu (\"Apps\" right-click) command.

    Unlike a slash :class:`Command`, this has no description or options --
    Discord shows only its ``name`` in the right-click menu, and the target
    message is supplied by ``interaction.resolved`` rather than typed options.
    """

    name: str
    required_permission: Permission | None = None


#: The command name used for dispatch (``ctx.command``) -- distinct from the
#: user-visible menu label, which can be changed without touching handler code.
REVIEW_MESSAGE_COMMAND = "review_message"
#: Member-facing report: anyone can flag an image message to the mods. Unlike
#: ``review_message`` it never blocks, deletes, or bans anything by itself --
#: it only files the message into the mod-review queue.
REPORT_MESSAGE_COMMAND = "report_message"

MESSAGE_COMMANDS: tuple[ContextMenuCommand, ...] = (
    ContextMenuCommand(
        name=REVIEW_MESSAGE_COMMAND,
        required_permission=Permission.MANAGE_GUILD,
    ),
    ContextMenuCommand(
        name=REPORT_MESSAGE_COMMAND,
        required_permission=None,
    ),
)

#: The label shown in Discord's right-click "Apps" menu for each context-menu
#: command, keyed by dispatch name.
MESSAGE_COMMAND_LABELS: dict[str, str] = {
    REVIEW_MESSAGE_COMMAND: "Review as scam",
    REPORT_MESSAGE_COMMAND: "Report scam to mods",
}

#: Reverse map: Discord sends the menu *label* as ``command_name`` on the
#: interaction; dispatch runs on the stable internal name.
MESSAGE_COMMAND_DISPATCH: dict[str, str] = {
    label: name for name, label in MESSAGE_COMMAND_LABELS.items()
}


#: Map of every command/subcommand path to the permission the service enforces.
COMMAND_PERMISSIONS: dict[str, Permission | None] = {}
for _cmd in COMMANDS:
    COMMAND_PERMISSIONS[_cmd.name] = _cmd.required_permission
for _menu_cmd in MESSAGE_COMMANDS:
    COMMAND_PERMISSIONS[_menu_cmd.name] = _menu_cmd.required_permission


#: Slash commands any member can run -- those requiring no permission. This is
#: the only set :attr:`~optimus.core.config.Settings.member_commands` can
#: narrow, which is what keeps a typo in that setting from ever disabling a
#: moderator command.
MEMBER_COMMANDS: frozenset[str] = frozenset(
    cmd.name for cmd in COMMANDS if cmd.required_permission is None
)


def required_permission(command_name: str) -> Permission | None:
    """Return the server-side permission required for ``command_name``."""
    return COMMAND_PERMISSIONS.get(command_name)


def is_enabled(command_name: str, member_commands: tuple[str, ...] | None) -> bool:
    """Return whether ``command_name`` is exposed under this configuration.

    Moderator and admin commands are always enabled: ``member_commands`` only
    ever narrows the member-facing surface. ``None`` means "expose everything",
    so the default configuration answers ``True`` for every command.
    """
    if member_commands is None:
        return True
    if command_name not in MEMBER_COMMANDS:
        return True
    return command_name in member_commands


def build_command_builders(
    member_commands: tuple[str, ...] | None = None,
) -> list[hikari.api.SlashCommandBuilder]:
    """Build hikari ``SlashCommandBuilder`` objects for global registration.

    Commands excluded by ``member_commands`` are not registered at all, so they
    never appear in a member's command picker. Registration is only the
    cosmetic half: :func:`is_enabled` is re-checked server-side, because an
    unregistered command can still be invoked by a stale client.
    """
    import hikari

    builders: list[hikari.api.SlashCommandBuilder] = []
    for cmd in COMMANDS:
        if not is_enabled(cmd.name, member_commands):
            continue
        builder = hikari.impl.SlashCommandBuilder(cmd.name, cmd.description)
        if cmd.required_permission is not None:
            builder.set_default_member_permissions(int(cmd.required_permission))
        if cmd.guild_only:
            builder.set_context_types([hikari.ApplicationContextType.GUILD])
        else:
            builder.set_context_types(
                [
                    hikari.ApplicationContextType.GUILD,
                    hikari.ApplicationContextType.BOT_DM,
                ]
            )
        for sub in cmd.subcommands:
            builder.add_option(
                hikari.CommandOption(
                    type=hikari.OptionType.SUB_COMMAND,
                    name=sub.name,
                    description=sub.description,
                    options=[_to_option(o) for o in sub.options],
                )
            )
        for opt in cmd.options:
            builder.add_option(_to_option(opt))
        builders.append(builder)
    return builders


def _to_option(option: Option) -> hikari.CommandOption:
    import hikari

    return hikari.CommandOption(
        type=hikari.OptionType(option.type),
        name=option.name,
        description=option.description,
        is_required=option.required,
        choices=[hikari.CommandChoice(name=c, value=c) for c in option.choices],
    )


def build_context_menu_command_builders() -> list[hikari.api.ContextMenuCommandBuilder]:
    """Build hikari ``ContextMenuCommandBuilder`` objects for global registration.

    Message context-menu commands only ever run inside a guild here (they all
    act on a guild message), so they are always scoped to guild context,
    unlike the DM-permitting slash commands. A command with no
    ``required_permission`` (the member report) is visible to every member;
    permission-gated ones are hidden from members without that permission.
    """
    import hikari

    builders: list[hikari.api.ContextMenuCommandBuilder] = []
    for cmd in MESSAGE_COMMANDS:
        label = MESSAGE_COMMAND_LABELS[cmd.name]
        builder = hikari.impl.ContextMenuCommandBuilder(type=hikari.CommandType.MESSAGE, name=label)
        if cmd.required_permission is not None:
            builder.set_default_member_permissions(int(cmd.required_permission))
        builder.set_context_types([hikari.ApplicationContextType.GUILD])
        builders.append(builder)
    return builders
