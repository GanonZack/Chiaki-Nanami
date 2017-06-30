import aiohttp
import collections
import contextlib
import discord
import functools
import inspect
import json
import psutil
import random
import sys

from contextlib import redirect_stdout
from discord.ext import commands
from io import StringIO
from itertools import chain, filterfalse, islice, starmap, tee
from math import log10
from operator import attrgetter, itemgetter

from .utils import converter
from .utils.compat import url_color, user_color
from .utils.context_managers import redirect_exception, temp_message
from .utils.converter import BotCommand, union
from .utils.errors import InvalidUserArgument, ResultsNotFound
from .utils.misc import (
    escape_markdown, group_strings, role_name, str_join, nice_time, ordinal, truncate
)
from .utils.paginator import BaseReactionPaginator, ListPaginator, page


def join_and(items, *, conjunction='and'):
    if not items:
        return ''
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}" if len(items) != 1 else items[0]

async def _mee6_stats(session, member):
    async with session.get(f"https://mee6.xyz/levels/{member.guild.id}?json=1&limit=-1") as r:
        levels = await r.json(content_type=None)
    for idx, user_stats in enumerate(levels['players'], start=1):
        if user_stats.get("id") == str(member.id):
            user_stats["rank"] = idx
            return user_stats
    raise ResultsNotFound(f"{member} does not have a mee6 level. :frowning:")


_status_colors = {
    discord.Status.online    : discord.Colour.green(),
    discord.Status.idle      : discord.Colour.orange(),
    discord.Status.dnd       : discord.Colour.red(),
    discord.Status.offline   : discord.Colour.default(),
    discord.Status.invisible : discord.Colour.default(),
}


def default_last_n(n=50):
    return lambda: collections.deque(maxlen=n)

class ServerPages(BaseReactionPaginator):
    async def server_color(self):
        try:
            result = self._colour
        except AttributeError:
            result = 0
            url = self.guild.icon_url
            if url:
                result = self._colour = await url_color(url)
        return result

    @property
    def guild(self):
        return self.context.guild


    @page('\N{INFORMATION SOURCE}')
    def default(self):
        """|coro|
        Shows some information about this server
        """
        return Meta.server_embed(self.guild)

    @page('\N{CAMERA}')
    def icon(self):
        """Shows the server's icon"""
        return Meta.server_icon(self.guild)

    @page('\N{THINKING FACE}')
    async def emojis(self):
        """Shows the server's emojis"""
        guild = self.guild
        emojis = guild.emojis
        description = '\n'.join(group_strings(map(str, guild.emojis), 10)) if emojis else 'There are no emojis :('

        return (discord.Embed(colour=await self.server_color(), description=description)
               .set_author(name=f"{guild}'s custom emojis")
               .set_footer(text=f'{len(emojis)} emojis')
               )


class Meta:
    """Info related commands"""

    def __init__(self, bot):
        self.bot = bot
        self.cmd_history = collections.defaultdict(default_last_n())
        self.last_members = collections.defaultdict(default_last_n())
        self.session = aiohttp.ClientSession()
        self.process = psutil.Process()

    def __unload(self):
        # Pray it closes
        self.bot.loop.create_task(self.session.close())

    @commands.command()
    @commands.guild_only()
    async def uinfo(self, ctx, *, user: discord.Member=None):
        """Gets some basic userful info because why not"""
        if user is None:
            user = ctx.author
        fmt = ("    Name: {0.name}\n"
               "      ID: {0.id}\n"
               " Hashtag: {0.discriminator}\n"
               "Nickname: {0.display_name}\n"
               " Created: {0.created_at}\n"
               "  Joined: {0.joined_at}\n"
               "   Roles: {1}\n"
               "  Status: {0.status}\n"
               )
        roles = str_join(', ', reversed(user.roles[1:]))
        await ctx.send("```\n{}\n```".format(fmt.format(user, roles)))

    @staticmethod
    async def _user_embed(member):
        avatar_url = member.avatar_url_as(format=None)
        playing = f"Playing **{member.game}**" if member.game else "Not playing anything..."
        roles = sorted(member.roles, reverse=True)[:-1]  # last role is @everyone

        return  (discord.Embed(colour=_status_colors[member.status], description=playing)
                .set_thumbnail(url=avatar_url)
                .set_author(name=str(member))
                .add_field(name="Nickname", value=member.display_name)
                .add_field(name="Created at", value=nice_time(member.created_at))
                .add_field(name=f"Joined server at", value=nice_time(member.joined_at))
                .add_field(name=f"Avatar link", value=f'[Click Here!](avatar_url)')
                .add_field(name=f"Roles - {len(roles)}", value=', '.join([role.mention for role in roles]) or "-no roles-", inline=False)
                .set_footer(text=f"ID: {member.id}")
                )

    @commands.command(aliases=['you'])
    async def about(self, ctx):
        """Shows some info about me"""
        bot = self.bot
        command_stats = '\n'.join(starmap('{1} {0}'.format, bot.command_counter.most_common())) or 'No stats yet.'
        extension_stats = '\n'.join(f'{len(set(getattr(bot, attr).values()))} {attr}'
                                    for attr in ('cogs', 'extensions'))
        python_version = str_join('.', sys.version_info[:3])

        with self.process.oneshot():
            memory_usage_in_mb = self.process.memory_full_info().uss / 1024**2
            cpu_usage = self.process.cpu_percent() / psutil.cpu_count()

        try:
            creator = self._creator
        except AttributeError:
            creator = self._creator = await self.bot.get_user_info(239110748180054017)

        chiaki_embed = (discord.Embed(description=bot.appinfo.description, colour=self.bot.colour)
                       .set_author(name=str(ctx.bot.user), icon_url=bot.user.avatar_url_as(format=None))
                       .add_field(name='Created by', value=str(creator))
                       .add_field(name='Servers', value=len(self.bot.guilds))
                       .add_field(name='Modules', value=extension_stats)
                       .add_field(name='CPU Usage', value=f'{cpu_usage}%\n{memory_usage_in_mb: .2f}MB')
                       .add_field(name='Commands', value=command_stats)
                       .add_field(name='Uptime', value=self.bot.str_uptime.replace(', ', '\n'))
                       .set_footer(text=f'Made with discord.py {discord.__version__} | Python {python_version}')
                       )
        await ctx.send(embed=chiaki_embed)

    @commands.group()
    async def info(self, ctx):
        """Super-command for all info-related commands"""
        if ctx.invoked_subcommand is None:
            subcommands = '\n'.join(ctx.command.commands.keys())
            await ctx.send(f"```\nAvailable info commands:\n{subcommands}```")

    @info.command(name='user')
    @commands.guild_only()
    async def info_user(self, ctx, *, member: converter.ApproximateUser=None):
        """Gets some userful info because why not"""
        if member is None:
            member = ctx.author
        await ctx.send(embed=await self._user_embed(member))

    @info.command(name='mee6')
    @commands.guild_only()
    async def info_mee6(self, ctx, *, member: converter.ApproximateUser=None):
        """Equivalent to `{prefix}rank`"""
        await ctx.invoke(self.rank, member=member)

    @commands.command()
    @commands.guild_only()
    async def userinfo(self, ctx, *, member: discord.Member=None):
        """Gets some userful info because why not"""
        await ctx.invoke(self.info_user, member=member)

    @commands.command()
    @commands.guild_only()
    async def rank(self, ctx, *, member: converter.ApproximateUser=None):
        """Gets mee6 info... if it exists"""
        if member is None:
            member = ctx.author
        avatar_url = member.avatar_url_as(format=None)

        no_mee6_in_server = "No stats found. You don't have mee6 in this server... I think."
        with redirect_exception((json.JSONDecodeError, no_mee6_in_server)):
            async with ctx.typing(), temp_message(ctx, "Fetching data, please wait...") as message:
                stats = await _mee6_stats(self.session, member)

        description = f"Currently sitting at {stats['rank']}!"
        xp_progress = "{xp}/{lvl_xp} ({xp_percent}%)".format(**stats)
        xp_remaining = stats['lvl_xp'] - stats['xp']
        colour = await user_color(member)

        mee6_embed = (discord.Embed(colour=colour, description=description)
                     .set_author(name=member.display_name, icon_url=avatar_url)
                     .set_thumbnail(url=avatar_url)
                     .add_field(name="Level", value=stats['lvl'])
                     .add_field(name="Total XP", value=stats['total_xp'])
                     .add_field(name="Level XP",  value=xp_progress)
                     .add_field(name="XP Remaining to next level",  value=xp_remaining)
                     .set_footer(text=f"ID: {member.id}")
                     )

        await ctx.send(embed=mee6_embed)

    @info.command(name='role')
    async def info_role(self, ctx, *, role: converter.ApproximateRole):
        """Shows information about a particular role.

        The role is case-insensitive.
        """
        server = ctx.guild

        def bool_as_answer(b):
            return "YNeos"[not b::2]

        member_amount = len(role.members)
        if member_amount > 20:
            members_name = "Members"
            members_value = f"{member_amount} (use {ctx.prefix}inrole '{role}' to figure out who's in that role)"
        else:
            members_name = f"Members ({member_amount})"
            members_value = str_join(", ", role.members) or '-no one is in this role :(-'

        hex_role_color = str(role.colour).upper()
        permissions = role.permissions.value
        permission_binary = "{0:32b}".format(permissions)
        str_position = ordinal(role.position + 1)
        nice_created_at = nice_time(role.created_at)
        description = f"Just chilling as {server}'s {str_position} role"
        footer = f"Created at: {nice_created_at} | ID: {role.id}"

        # I think there's a way to make a solid color thumbnail, idk though
        role_embed = (discord.Embed(title=role.name, colour=role.colour, description=description)
                     .add_field(name="Colour", value=hex_role_color)
                     .add_field(name="Permissions", value=permissions)
                     .add_field(name="Permissions (as binary)", value=permission_binary)
                     .add_field(name="Mentionable?", value=bool_as_answer(role.mentionable))
                     .add_field(name="Displayed separately?", value=bool_as_answer(role.hoist))
                     .add_field(name="Integration role?", value=bool_as_answer(role.managed))
                     .add_field(name=members_name, value=members_value, inline=False)
                     .set_footer(text=footer)
                     )

        await ctx.send(embed=role_embed)

    @staticmethod
    def text_channel_embed(channel):
        topic = '\n'.join(group_strings(channel.topic, 70)) if channel.topic else discord.Embed.Empty
        member_count = len(channel.members)
        empty_overwrites = sum(ow.is_empty() for _, ow in channel.overwrites)
        overwrite_message = f'{len(channel.overwrites)} ({empty_overwrites} empty)'

        return (discord.Embed(description=topic, timestamp=channel.created_at)
               .set_author(name=f'#{channel.name}')
               .add_field(name='ID', value=channel.id)
               .add_field(name='Position', value=channel.position)
               .add_field(name='Members', value=len(channel.members))
               .add_field(name='Permission Overwrites', value=overwrite_message)
               .set_footer(text='Created')
               )

    @staticmethod
    def voice_channel_embed(channel):
        empty_overwrites = sum(ow.is_empty() for _, ow in channel.overwrites)
        overwrite_message = f'{len(channel.overwrites)} ({empty_overwrites} empty)'

        return (discord.Embed(timestamp=channel.created_at)
               .set_author(name=channel.name)
               .add_field(name='ID', value=channel.id)
               .add_field(name='Position', value=channel.position)
               .add_field(name='Bitrate', value=channel.bitrate)
               .add_field(name='Max Members', value=channel.user_limit or '\N{INFINITY}')
               .add_field(name='Permission Overwrites', value=overwrite_message)
               .set_footer(text='Created')
               )

    @staticmethod
    async def server_colour(server):
        icon = server.icon_url
        return await url_color(icon) if icon else discord.Colour.default()

    @info.command(name='channel')
    async def info_channel(self, ctx, channel: union(discord.TextChannel, discord.VoiceChannel)=None):
        """Shows info about a voice or text channel."""
        if channel is None:
            channel = ctx.channel
        embed_type = 'text_channel_embed' if isinstance(channel, discord.TextChannel) else 'voice_channel_embed'
        channel_embed = getattr(self, embed_type)(channel)
        channel_embed.colour = self.bot.colour

        await ctx.send(embed=channel_embed)

    @staticmethod
    async def server_embed(server):
        highest_role = server.role_hierarchy[0]
        description = f"Owned by {server.owner}"
        features = '\n'.join(server.features) or 'None'
        counts = (f'{len(getattr(server, thing))} {thing}' for thing in ('channels', 'roles', 'emojis'))

        statuses = collections.OrderedDict.fromkeys(['online', 'idle', 'dnd', 'offline'], 0)
        statuses.update(collections.Counter(m.status.name for m in server.members))
        statuses['bots'] = sum(m.bot for m in server.members)
        member_stats = '\n'.join(starmap('{1} {0}'.format, statuses.items()))

        server_embed = (discord.Embed(title=server.name, description=description, timestamp=server.created_at)
                       .add_field(name="Default Channel", value=server.default_channel.mention)
                       .add_field(name="Highest Role", value=highest_role)
                       .add_field(name="Region", value=server.region.value.title())
                       .add_field(name="Verification Level", value=server.verification_level)
                       .add_field(name="Explicit Content Filter", value=server.explicit_content_filter)
                       .add_field(name="Special Features", value=features)
                       .add_field(name='Counts', value='\n'.join(counts))
                       .add_field(name=f'{len(server.members)} Members', value=member_stats)
                       .set_footer(text=f'ID: {server.id} | Created')
                       )

        icon = server.icon_url
        if icon:
            server_embed.set_thumbnail(url=icon)
            server_embed.colour = await Meta.server_colour(server)
        return server_embed

    @info.group(name='server', aliases=['guild'])
    @commands.guild_only()
    async def info_server(self, ctx):
        """Shows info about a server"""
        if ctx.subcommand_passed in ['server', 'guild']:
            server_pages = ServerPages(ctx)
            await server_pages.interact()
        elif ctx.invoked_subcommand is None:
            subcommands = '\n'.join(ctx.command.all_commands)
            await ctx.send(f"```\nAvailable server commands:\n{subcommands}```")

    @commands.command(aliases=['chnls'])
    async def channels(self, ctx):
        """Shows all the channels in the server. Channels you can access are **bolded**

        If you're in a voice channel, that channel is ***italicized and bolded***
        """
        permissions_in = ctx.author.permissions_in

        def get_channels(channels, prefix, permission):
            return [f'**{prefix}{escape_markdown(str(c))}**' if getattr(permissions_in(c), permission) 
                    else f'{prefix}{escape_markdown(str(c))}' for c in channels]

        text_channels  = get_channels(ctx.guild.text_channels,  prefix='#', permission='read_messages')
        voice_channels = get_channels(ctx.guild.voice_channels, prefix='', permission='connect')

        voice = ctx.author.voice
        if voice is not None:
            index = voice.channel.position
            name = voice_channels[index]
            # Name was already bolded
            if not name.startswith('**'):
                name = f'**{name}**'
            voice_channels[index] = f'*{name}*'

        channels = chain(
            ('', f'**List of Text Channels ({len(text_channels)})**', '-' * 20, ), text_channels,
            ('', f'**List of Voice Channels ({len(voice_channels)})**', '-' * 20, ), voice_channels
        )

        pages = ListPaginator(ctx, channels, title=f'Channels in {ctx.guild}', colour=self.bot.colour)
        await pages.interact()

    @commands.command()
    async def members(self, ctx):
        """Shows all the members of the server, sorted by their top role, then by join date"""
        # TODO: Status
        members = [str(m) for m in sorted(ctx.guild.members, key=attrgetter("top_role", "joined_at"), reverse=True)]
        pages = ListPaginator(ctx, members, title=f'Members in {ctx.guild} ({len(members)})',
                           colour=self.bot.colour)
        await pages.interact()

    @staticmethod
    async def server_icon(server):
        icon = (discord.Embed(title=f"{server}'s icon")
               .set_footer(text=f"ID: {server.id}"))

        icon_url = server.icon_url
        if icon_url:
            icon.set_image(url=icon_url)
            icon.colour = await url_color(icon_url)
        else:
            icon.description = "This server has no icon :("
        return icon

    @commands.command()
    async def roles(self, ctx):
        """Shows all the roles in the server. Roles in bold are the ones you have"""
        roles = ctx.guild.role_hierarchy[:-1]
        padding = int(log10(max(map(len, (role.members for role in roles))))) + 1

        get_name = functools.partial(role_name, ctx.author)
        hierarchy = [f"`{len(role.members) :<{padding}}\u200b` {get_name(role)}" for role in roles]
        pages = ListPaginator(ctx, hierarchy, title=f'Roles in {ctx.guild} ({len(hierarchy)})',
                           colour=self.bot.colour)
        await pages.interact()

    @commands.command()
    async def emojis(self, ctx):
        """Shows all the emojis in the server."""

        if not ctx.guild.emojis:
            return await ctx.send("This server doesn't have any custom emojis. :'(")

        emojis = map('{0} = {0.name} ({0.id})'.format, ctx.guild.emojis)
        pages = ListPaginator(ctx, emojis, title=f'Emojis in {ctx.guild}', colour=self.bot.colour)
        await pages.interact()

    async def _source(self, ctx, thing):
        lines = inspect.getsourcelines(thing)[0]
        await iterable_limit_say(lines, '', ctx=ctx, prefix='```py\n', escape_code=True)

    # @commands.command(disabled=True)
    async def source(self, ctx, *, cmd: BotCommand):
        """Displays the source code for a particular command"""
        # TODO: use GitHub
        await self._source(ctx, cmd.callback)

    @staticmethod
    async def _inrole(ctx, *roles, members, conjunction='and'):
        # because join_and takes a sequence... -_-
        joined_roles = join_and([str(r) for r in roles], conjunction=conjunction)
        truncated_title = truncate(f'Members in role{"s" * (len(roles) != 1)} {joined_roles}', 256, '...')

        total_color = map(sum, zip(*(role.colour.to_rgb() for role in roles)))
        average_color = discord.Colour.from_rgb(*map(round, (c / len(roles) for c in total_color)))

        if members:
            entries = sorted(map(str, members))
            # Make the author's name bold (assuming they have that role).
            # We have to do it after the list was built, otherwise the author's name
            # would be at the top.
            with contextlib.suppress(ValueError):
                index = entries.index(str(ctx.author))
                entries[index] = f'**{entries[index]}**'
        else:
            entries = ('There are no members :(', )

        pages = ListPaginator(ctx, entries, colour=average_color, title=truncated_title)
        await pages.interact()

    @commands.command()
    @commands.guild_only()
    async def inrole(self, ctx, *, role: discord.Role):
        """Checks which members have a given role. The role is case sensitive.

        If you have the role, your name will be in **bold**.
        Only one role can be specified. For multiple roles, use `{prefix}inanyrole` or `{prefix}inallrole`.
        """
        await self._inrole(ctx, role, members=role.members)

    @commands.command()
    @commands.guild_only()
    async def inanyrole(self, ctx, *roles: discord.Role):
        """Checks which members have any of the given role(s). The role(s) are case sensitive.
        If you have the role, your name will be in **bold**.

        If you don't want to mention a role and there's a space in the role name, 
        you must put the role in quotes
        """
        await self._inrole(ctx, *roles, members=set(chain.from_iterable(map(attrgetter('members'), roles))),
                           conjunction='or')

    @commands.command()
    @commands.guild_only()
    async def inallrole(self, ctx, *roles: discord.Role):
        """Checks which members have all of the given role(s). The role(s) are case sensitive.
        If you have the role, your name will be in **bold**.

        If you don't want to mention a role and there's a space in the role name, 
        you must put that role in quotes
        """
        role_members = (role.members for role in roles)
        await self._inrole(ctx, *roles, members=set(next(role_members)).intersection(*role_members))

    @commands.command()
    @commands.guild_only()
    async def permroles(self, ctx, *, perm: str):
        """
        Checks which roles have a particular permission

        The permission is case insensitive.
        """
        perm_attr = perm.replace(' ', '_').lower()
        roles = filter(attrgetter(f'permissions.{perm_attr}'), ctx.guild.role_hierarchy)
        title = f"Roles in {ctx.guild} that have {perm.replace('_', ' ').title()}"
        entries = map(functools.partial(role_name, ctx.author), roles)

        pages = ListPaginator(ctx, entries, title=title, colour=ctx.bot.colour)
        await pages.interact()    

    @staticmethod
    async def _display_permissions(ctx, thing, permissions, extra=''):
        diffs = '\n'.join([f"{'-+'[value]} {attr.title().replace('_', ' ')}" for attr, value in permissions])
        str_perms = f'```diff\n{diffs}```'

        value = permissions.value
        perm_embed = (discord.Embed(colour=thing.colour, description=str_perms)
                     .set_author(name=f'Permissions for {thing}')
                     .set_footer(text=f'Value: {value} | Binary: {bin(value)[2:]}')
                     )
        await ctx.send(embed=perm_embed)

    @commands.command(aliases=['perms'])
    @commands.guild_only()
    async def permissions(self, ctx, *, member_or_role: union(discord.Member, discord.Role)=None):
        """Shows either a member's Permissions, or a role's Permissions

        ```diff
        + Permissions you have will be shown like this.
        - Permissions you don't have will be shown like this.
        ```
        """
        if member_or_role is None:
            member_or_role = ctx.author
        permissions = getattr(member_or_role, 'permissions', None) or member_or_role.guild_permissions
        await self._display_permissions(ctx, member_or_role, permissions)

    @commands.command(aliases=['permsin'])
    @commands.guild_only()
    async def permissionsin(self, ctx, *, member: discord.Member=None):
        """Shows either a member's Permissions *in the channel*

        ```diff
        + Permissions you have will be shown like this.
        - Permissions you don't have will be shown like this.
        ```
        """
        if member is None:
            member = ctx.author
        await self._display_permissions(ctx, member, ctx.channel.permissions_for(member), extra=f'in {ctx.channel.mention}')

    @commands.command(aliases=['av'])
    async def avatar(self, ctx, *, user: converter.ApproximateUser=None):
        if user is None:
            user = ctx.author
        avatar_url = user.avatar_url_as(format=None)
        colour = await user_color(user)
        nick = getattr(user, 'nick', None)
        description = f"*(Also known as \"{nick}\")*" * bool(nick)

        av_embed = (discord.Embed(colour=colour, description=description)
                   .set_author(name=f"{user}'s Avatar", icon_url=avatar_url, url=avatar_url)
                   #.add_field(name="Link", value=f"[Click me for avatar!]({avatar_url})")
                   .set_image(url=avatar_url)
                   .set_footer(text=f"ID: {user.id}")
                   )
        await ctx.send(embed=av_embed)

    @commands.command()
    async def cmdhistory(self, ctx):
        """Displays up to the last 50 commands you've input"""
        history = self.cmd_history[ctx.author]
        msg = (f"Your last {len(history)} commands:\n```\n{', '.join(history)}```"
               if history else "You have not input any commands...")
        await ctx.send(msg)

    @commands.command(name='cmdranks')
    async def command_ranks(self, ctx, n=10):
        """Shows the most common commands"""
        if not 3 <= n <= 50:
            raise InvalidUserArgument("I can only show the top 3 to the top 50 commands... sorry...")

        fmt = f'`{ctx.prefix}' + '{0}` = {1}'
        format_map = starmap(fmt.format, self.bot.command_leaderboard.most_common(n))
        embed = (discord.Embed(description='\n'.join(format_map), colour=self.bot.colour)
                .set_author(name=f'Top {n} used commands')
                )
        await ctx.send(embed=embed)

    async def on_command(self, ctx):
        self.cmd_history[ctx.author].append(ctx.message.content)
        self.bot.command_leaderboard[str(ctx.command)] += 1

    # @commands.command(disabled=True, usage=['pow', 'os.system'], aliases=['pyh'])
    async def pyhelp(self, ctx, thing):
        """Gives you the help string for a builtin python function.
        (or any sort of function, for that matter)
        """
        # Someone told me a "lib" already does this. Is that true? If so, what lib is it?
        # TODO: Only get the docstring
        with StringIO() as output, redirect_stdout(output):
            help(thing)
            help_lines = output.getvalue().splitlines()
            await iterable_limit_say(help_lines, ctx=ctx)

    async def lastjoin(self, ctx, n: int=10):
        """Display the last n members that have joined the server. Default is 10. Maximum is 50."""
        if not 0 < n < 50:
            raise InvalidUserArgument("I can only show between 1 and 50 members. Sorry. :(")

        members = str_join(', ', islice(self.last_members[ctx.guild], n))
        message = (f'These are the last {n} members that have joined the server:```\ncss{members}```'
                   if members else "I don't think any members joined this server yet :(")
        await ctx.send(message)

    async def on_member_join(self, member):
        self.last_members[member.guild].append(member)

def setup(bot):
    if not hasattr(bot, 'command_leaderboard'):
        bot.command_leaderboard = collections.Counter()
    bot.add_cog(Meta(bot))


