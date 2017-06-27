import asyncio
import contextlib
import discord
import functools
import itertools
import json
import os

from collections import defaultdict, deque, namedtuple
from datetime import datetime, timedelta
from discord.ext import commands
from operator import attrgetter, contains, itemgetter

from .utils import checks, errors
from .utils.context_managers import redirect_exception  
from .utils.converter import duration, in_, union
from .utils.database import Database
from .utils.json_serializers import (
    DatetimeEncoder, DequeEncoder, decode_datetime, decode_deque, union_decoder
    )
from .utils.misc import duration_units, emoji_url, ordinal, role_name
from .utils.paginator import ListPaginator
from .utils.timer import Scheduler, TimerEntry


def _mod_file(filename): 
    return os.path.join('mod', filename)

def _rreplace(s, old, new, count=1):
    li = s.rsplit(old, count)  
    return new.join(li)


def _make_entries(scheduler, data):
    print(dict(data))
    data.update(zip(data, map(TimerEntry._make, data.values())))
    for entry in data.values():
        scheduler.add_entry(entry)


class MemberID(union):
    def __init__(self, user_type=discord.Member):
        super().__init__(user_type, int)

    async def convert(self, ctx, arg):
        member = await super().convert(ctx, arg)
        if isinstance(member, int):
            obj = discord.Object(id=member)
            obj.__str__ = attrgetter('id')
            obj.guild = ctx.guild
            return obj
        return member


ModAction = namedtuple('ModAction', 'repr emoji colour')
mod_action_types = {
    'warn'         : ModAction('warned', '\N{WARNING SIGN}', 0xFFAA00),
    'mute'         : ModAction('muted', '\N{ZIPPER-MOUTH FACE}', 0),
    'kick'         : ModAction('kicked', '\N{WOMANS BOOTS}', 0xFF0000),
    'softban'      : ModAction('soft banned', '\N{BIOHAZARD SIGN}', 0xF08000),
    'tempban'      : ModAction('temporarily banned', '\N{ALARM CLOCK}', 0xA00000),
    'ban'          : ModAction('banned', '\N{HAMMER}', 0x800000),
    'unban'        : ModAction('unbanned', '\N{HAMMER}', 0x00FF00),
}
_restricted_warn_punishments = {'softban', 'unban', 'warn'}

ModCase = namedtuple('ModCase', 'type mod user reason')
WarnEntry = namedtuple('WarnEntry', 'time reason')

SlowmodeEntry = namedtuple('SlowmodeEntry', 'duration no_immune')
SlowmodeEntry.__new__.__defaults__ = (False, )


_member_key = 's{0.guild.id};m{0.id}'.format


class WarnEncoder(DequeEncoder, DatetimeEncoder):
    pass

warn_hook = union_decoder(decode_deque, decode_datetime)

_default_warn_config = {
    'timeout': 60 * 15,
    'punishments': {
        '2': {
            'punish': 'mute',
            'duration': 60 * 10,
        }
    }
}


# TODO:
# - implement anti-raid protocol
# - implement antispam
# - implement mention-spam
class Moderator:
    def __init__(self, bot):
        self.bot = bot

        # Current statuses
        self.current_slowmodes = defaultdict(dict)
        self.current_slowonlys = {}

        # Databases / Configs
        self.guild_warn_config = Database(_mod_file('warnconfig.json'), default_factory=_default_warn_config.copy)
        self.warn_log = Database(_mod_file('warnlog.json'), default_factory=deque, encoder=WarnEncoder, object_hook=warn_hook)
        self.raids = Database(_mod_file('raids.json'))
        self.cases = Database(_mod_file('cases.json'), default_factory=dict)
        self.mutes = Database(_mod_file('mutes.json'))
        self.tempbans = Database(_mod_file('tempbans.json'))
        self.muted_roles = Database(_mod_file('muted_roles.json'), default_factory=None)

        self.mute_scheduler = Scheduler(bot, 'mute_end')
        self.tempban_scheduler = Scheduler(bot, 'tempban_end')

        _make_entries(self.mute_scheduler, self.mutes)
        _make_entries(self.tempban_scheduler, self.tempbans)

        self.slowmodes = Database(_mod_file('slowmode.json'))
        self.slowusers = Database(_mod_file('slow-users.json'))
        # because namedtuples serialize a namedtuple as a list in JSON
        self.slowmodes.update(zip(self.slowmodes, map(SlowmodeEntry._make, self.slowmodes.values())))

        self.slow_immune = Database(_mod_file('slow-immune-roles.json'), default_factory=list)
        self.slowmode_bucket = {}
        self.slowuser_bucket = {}

    @staticmethod
    def _case_embed(num, ctx, case, duration=None):
        case_type, mod, user, reason = case
        avatar_url = user.avatar_url_as(format=None)
        bot_avatar = ctx.bot.user.avatar_url_as(format=None)

        auto_punished = getattr(ctx, 'auto_punished', False)
        if auto_punished:
            mod = ctx.bot.user

        duration_string = f' for {duration_units(duration)}' if duration is not None else ''
        action_field = f'{"Auto-" * auto_punished}{case_type.repr.title()}{duration_string} by {mod}'
        reason = reason or 'No reason. Please enter one.'

        return (discord.Embed(color=case_type.colour, timestamp=ctx.message.created_at)
               .set_author(name=f"Case #{num}", icon_url=emoji_url(case_type.emoji))
               .set_thumbnail(url=avatar_url)
               .add_field(name="User", value=str(user))
               .add_field(name="Action", value=action_field, inline=False)
               .add_field(name="Reason", value=reason, inline=False)
               .set_footer(text=f'ID: {user.id}', icon_url=bot_avatar)
               )

    async def _send_case(self, ctx, case, duration=None):
        server_cases = self.cases[ctx.guild]
        case_channel = self.bot.get_channel(server_cases.get('case_channel'))
        if case_channel is None:
            return

        cases = server_cases.setdefault('cases', [])
        case_embed = self._case_embed(len(cases) + 1, ctx, case, duration)

        msg = await case_channel.send(embed=case_embed)

        case = {
            'message_id': msg.id,
            'channel_id': case_channel.id,
            'type': case.type,
            'mod': case.mod.id,
            'user': case.user.id,
            'reason': case.reason,
        }
        cases.append(case)

    # ---------------- Slowmode ------------------

    def _is_slowmode_immune(self, member):
        immune_roles = self.slow_immune.get(member.guild, []) 
        return any(r.id in immune_roles for r in member.roles)

    @staticmethod
    async def _delete_if_rate_limited(bucket, key, duration, message):
        time = bucket.get(key)
        if time is None or (message.created_at - time).total_seconds() >= duration:
            bucket[key] = message.created_at
        else:
            await message.delete()

    async def check_slowmode(self, message):
        channel = message.channel
        config = self.slowmodes.get(channel)
        if config is None:
            return

        author = message.author
        if not config.no_immune and self._is_slowmode_immune(author):
            return

        bucket = self.slowmode_bucket.setdefault(channel, {})
        await self._delete_if_rate_limited(bucket, author, config.duration, message)

    async def check_slowuser(self, message):
        key = _member_key(message.author)
        duration = self.slowusers.get(key)
        if duration is None:
            return

        await self._delete_if_rate_limited(self.slowuser_bucket, key, duration, message)

    @commands.group(invoke_without_command=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def slowmode(self, ctx, duration: duration, *, member: discord.Member=None):
        """Puts a thing in slowmode.

        An optional member argument can be provided. If it's given, it puts only 
        that user in slowmode for the entire server. Otherwise it puts the channel in slowmode.

        Those with a slowmode-immune role will not be affected. 
        If you want to put them in slowmode too, use `{prefix}slowmode noimmune`
        """
        if member is not None:
            if self._is_slowmode_immune(member):
                message = (f"{member} is immune from slowmode due to having a "
                            "slowmode-immune role. Consider either removing the "
                           f"role from them, using `{ctx.prefix}slowmode no-immune`, "
                            "or giving them a harsher punishment.")
                return await ctx.send(message)

            self.slowusers[_member_key(member)] = duration
            await ctx.send('{member.mention} is now in slowmode! They must wait {duration} '
                           'between each message they send.')
        else:
            channel = ctx.channel
            current_slowmode = self.slowmodes.get(channel)
            if current_slowmode and current_slowmode.no_immune:
                return await ctx.send('{channel.mention} is already in **no-immune** slowmode. '
                                      'You need to turn it off first.')

            self.slowmodes[ctx.channel] = SlowmodeEntry(duration, False)
            await ctx.send(f'{channel.mention} is now in slowmode! '
                            'Everyone must wait {duration} between each message they send.')

    @slowmode.command(name='noimmune', aliases=['n-i'], invoke_without_command=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def slowmode_no_immune(self, ctx, duration: duration, *, member: discord.Member=None):
        """Puts the channel or member in "no-immune" slowmode.

        Unlike `{prefix}slowmode`, no one is immune to this slowmode,
        even those with a slowmode-immune role, which means everyone's messages
        will be deleted if they are within the duration given.
        """
        if member is None:
            member, pronoun = ctx.channel, 'Everyone'
            self.slowmodes[member] =  SlowmodeEntry(duration, True)
        else:
            pronoun = 'They'
            self.slowusers[_member_key(member)] = duration

        await ctx.send(f'{member.mention} is now in **no-immune** slowmode! '
                       f'**{pronoun}** must wait {duration} after each message they send.')

    @slowmode.command(name='off')
    async def slowmode_off(self, ctx, *, member: discord.Member=None):
        """Turns off slowmode for either a member or channel."""
        if member is None:
            member = ctx.channel
            del self.slowmodes[member]
            del self.slowmode_bucket[member]
        else:
            key = _member_key(member)
            del self.slowusers[key]
            del self.slowuser_bucket[key]
        await ctx.send(f'{member.mention} is no longer in slowmode... \N{SMILING FACE WITH OPEN MOUTH AND COLD SWEAT}')

    @commands.command()
    async def slowoff(self, ctx, *, member: discord.Member=None):
        """Alias for `{prefix}slowmode off`"""
        await ctx.invoke(self.slowmode_off, member=member)

    @slowmode_off.error
    @slowoff.error
    async def slowmode_off_error(self, ctx, error):
        cause = error.__cause__
        if isinstance(cause, KeyError):
            arg = ctx.kwargs['member'] or ctx.channel
            await ctx.send(f'{arg.mention} was never in slowmode... \N{NEUTRAL FACE}')

    @slowmode.group(name='immune')
    async def slowmode_immune(self, ctx):
        """Lists all the roles that are immune to slowmode.

        If a member has any of these roles, during a normal slowmode, 
        they won't have their messages deleted.
        """
        if ctx.invoked_subcommand is not self.slowmode_immune:
            return

        immune = self.slow_immune[ctx.guild]
        getter = functools.partial(discord.utils.get, ctx.guild.roles)
        roles = (getter(id=id) for id in immune)
        entries = (map(functools.partial(role_name, ctx.author), roles)
                   if immune else ('There are no roles...', ))

        pages = ListPaginator(ctx, entries, title=f'List of slowmode-immune roles in {ctx.guild}',
                              colour=ctx.bot.colour)
        await pages.interact()

    @slowmode_immune.command(name='add')
    @checks.admin_or_permissions(manage_guild=True)
    async def slowmode_add_immune(self, ctx, *, role: discord.Role):
        """Makes a role  immune from slowmode."""
        immune = self.slow_immune[ctx.guild]
        id = role.id
        if id in immune:
            await ctx.send(f'**{role}** is already immune from slowmode...')
        else:
            immune.append(id)
            await ctx.send(f'**{role}** is now immune from slowmode!')

    @slowmode_immune.command(name='remove')
    @checks.admin_or_permissions(manage_guild=True)
    async def slowmode_remove_immune(self, ctx, *, role: discord.Role):
        """Makes a role no longer immune from slowmode."""
        self.slow_immune[ctx.guild].remove(role.id)
        await ctx.send(f'{role} is now no longer immune from slowmode')

    @slowmode_remove_immune.error
    async def sm_remove_immune_error(self, ctx, error):
        if isinstance(error.__cause__, ValueError):
            await ctx.send(f'{ctx.kwargs["roles"]} was never immune from slowmode...')

    @slowmode_immune.command(name='reset')
    @checks.admin_or_permissions(manage_guild=True)
    async def slowmode_reset_immune(self, ctx):
        """Removes all slowmode-immune roles."""
        immune = self.slow_immune[ctx.guild]
        if not immune:
            return await ctx.send('What are you doing? There are no slowmode-immune roles to clear!')

        immune.clear()
        await ctx.send('Done, there are no more slowmode-immune roles.')

    @commands.command(aliases=['clr'])
    @checks.mod_or_permissions(manage_messages=True)
    async def clear(self, ctx, num_or_user: union(int, discord.Member)=None):
        """Clears some messages in a channels

        The argument can either be a user or a number.
        If it's a number it deletes *up to* that many messages.
        If it's a user, it deletes any message by that user up to the last 100 messages.
        If no argument was specified, it deletes my messages.
        """

        if isinstance(num_or_user, int):
            if num_or_user < 1:
                raise errors.InvalidUserArgument(f"How can I delete {number} messages...?")
            deleted = await ctx.channel.purge(limit=min(num_or_user, 1000) + 1)
        elif isinstance(num_or_user, discord.Member):
            deleted = await ctx.channel.purge(check=lambda m: m.author.id == num_or_user.id)
        else:
            deleted = await ctx.channel.purge(check=lambda m: m.author.id == bot.user.id)

        deleted_count = len(deleted) - 1
        is_plural = 's'*(deleted_count != 1)
        await ctx.send(f"Deleted {deleted_count} message{is_plural} successfully!", delete_after=1.5)

    @clear.error
    async def clear_error(self, ctx, error):
        # We need to use the __cause__ because any non-CommandErrors will be 
        # wrapped in CommandInvokeError
        cause = error.__cause__
        if isinstance(cause, discord.Forbidden):
            await ctx.send("I need the Manage Messages perm to clear messages.")
        elif isinstance(cause, discord.HTTPException):
            await ctx.send("Couldn't delete the messages for some reason...")

    @commands.command()
    @checks.is_mod()
    async def warn(self, ctx, member: discord.Member, *, reason: str):
        """Warns a user (obviously)"""
        author, current_time = ctx.author, ctx.message.created_at
        warn_queue = self.warn_log[_member_key(member)]
        warn_queue.append((current_time, author.id, reason))
        current_warn_num = len(warn_queue)

        def check_warn_num():
            if current_warn_num >= max(map(int, punishments)):
                warn_queue.popleft()

        async def default_warn():
            warn_embed = (discord.Embed(colour=0xffaa00, description=reason, timestamp=current_time)
                         .set_author(name=str(author), icon_url=author.avatar_url)
                         )
            await member.send(f"You have been warned by {author} for the followng reason:", embed=warn_embed)
            await member.send(f"This is your {ordinal(current_warn_num)} warning.")
            await ctx.send(f"\N{WARNING SIGN} Warned {member.mention} successfully!")
            check_warn_num()

        warn_config = self.guild_warn_config[ctx.guild]
        punishments = warn_config['punishments']
        punishment = punishments.get(str(current_warn_num))
        if punishment is None:
            return await default_warn()

        # warn is too old, ignore it.
        if (current_time - warn_queue[0][0]).total_seconds() > warn_config['timeout']:
            return await default_warn()

        # Auto-punish the user
        args = member,
        if punishment['duration'] is not None:
            args += punishment['duration'],
        ctx.auto_punished = True

        punish = punishment['punish']
        await ctx.invoke(getattr(self, punish), *args, reason=reason + f'\n({ordinal(current_warn_num)} warning)')
        check_warn_num()

    @commands.command(name='clearwarns')
    @checks.is_mod()
    async def clear_warns(self, ctx, member: discord.Member):
        self.warn_log[_member_key(member)].clear()
        await ctx.send(f"{member}'s warns have been reset!")

    @commands.command(name='warnpunish')
    async def warn_punish(self, ctx, num: int, punishment, duration: duration=None):
        """Sets the punishment a user receives upon exceeding a given warn limit"""
        punish_lower = punishment.lower()
        if punish_lower in _restricted_warn_punishments:
            raise errors.InvalidUserArgument("{punish_lower} is not a valid punishment")

        if punish_lower in {'tempban', 'mute'} and duration is None:
            raise errors.InvalidUserArgument(f'A duration is required for {punish_lower}')

        payload = {
            'punish': punish_lower,
            'duration': duration,
        }
        self.guild_warn_config[ctx.guild]['punishments'][str(num)] = payload
        await ctx.send(f'\N{OK HAND SIGN} if a user has been warned {num} times, I will **{punish_lower}** them.')

    @commands.command(name='warnpunishments', aliases=['warnpl'])
    async def warn_punishments(self, ctx):
        """Shows this list of warn punishments"""
        punishments = sorted(self.guild_warn_config[ctx.guild]['punishments'].items(), key=lambda p: int(p[0]))
        entries = (f'{num} warns => **{p["punish"].title()}**' for num, p in punishments)

        pages = ListPaginator(ctx, entries, title=f'Punishments for {ctx.guild}', colour=ctx.bot.colour)
        await pages.interact()

    @commands.command(name='warntimeout')
    async def warn_timeout(self, ctx, duration: duration):
        """Sets the maximum time between the oldest warn and the most recent warn.
        If a user hits a warn limit within this timeframe, they will be punished.
        """
        self.guild_warn_config[ctx.guild]['timeout'] = duration
        await ctx.send(f'Alright, if a user was warned within {duration_units(duration)} '
                        'after their oldest warn, bad things will happen.')

    @staticmethod
    def _check_user(ctx, member):
        if ctx.author.id == member.id:
            raise errors.InvalidUserArgument("Please don't hurt yourself. :(")
        if member.id == ctx.bot.user.id:
            raise errors.InvalidUserArgument("Hey, what did I do??")

    async def _create_muted_role(self, server):
        role = await server.create_role(name='Chiaki-Muted', colour=discord.Colour.red())
        await self._regen_muted_role_perms(role, *server.channels)

        self.muted_roles[str(server.id)] = role.id
        # Explicit dump to make sure the roles get updated
        await self.mutes.dump()
        return role

    def _get_muted_role(self, server):
        if server is None:
            return None

        role_id = self.muted_roles.get(str(server.id))
        return discord.utils.get(server.roles, id=role_id)

    async def _setdefault_muted_role(self, server):
        # Role could've been deleted, which means it will be None. 
        # So we have to account for that.
        return self._get_muted_role(server) or await self._create_muted_role(server)

    @staticmethod
    async def _regen_muted_role_perms(role, *channels):
        muted_permissions = dict.fromkeys(['send_messages', 'manage_messages', 'add_reactions',
                                           'speak', 'connect', 'use_voice_activation'], False)
        for channel in channels:
            await channel.set_permissions(role, **muted_permissions)

    def put_payload(db, member, duration):
        payload = {
            'time': str(datetime.utcnow()),
            'duration': duration,
        }

        db[_member_key(member)] = payload

    async def _do_mute(self, member, when):
        mute_role = await self._setdefault_muted_role(member.guild)
        await member.add_roles(mute_role)

        entry = TimerEntry(when, (member.guild.id, member.id, mute_role.id))
        self.mute_scheduler.add_entry(entry)
        self.mutes[_member_key(member)] = entry

    async def _default_mute_command(self, ctx, member, when, *, duration, reason):
        await self._do_mute(member, when)
        await ctx.send(f"Done. {member.mention} will now be muted for {duration_units(duration)}... \N{ZIPPER-MOUTH FACE}")

    @commands.command()
    @checks.mod_or_permissions(manage_roles=True)
    async def mute(self, ctx, member: discord.Member, duration: duration, *, reason: str=None):
        """Mutes a user (obviously)"""
        when = datetime.utcnow() + timedelta(seconds=duration)
        await self._default_mute_command(ctx, member, when.timestamp(), duration=duration, reason=reason)

    @commands.command()
    async def mutetime(self, ctx, member: discord.Member=None):
        """Shows the time left for a member's mute. Defaults to yourself."""
        if member is None:
            member = ctx.author

        # early out for the case of premature role removal, 
        # either by ->unmute or manually removing the role
        role = self._get_muted_role(ctx.guild)
        if role not in member.roles:
            return await ctx.send('{member} is not muted...')

        try:    
            entry = self.mutes[_member_key(member)]
        except KeyError:
            await ctx.send(f"{member} has been perm-muted, you must've "
                            "added the role manually or something...")
        else:
            when = datetime.utcfromtimestamp(entry.when)
            delta = entry.when - datetime.utcnow().timestamp()
            await ctx.send(f'{member} will be muted for {duration_units(delta)}. '
                           f'They will be unmuted on {when: %c}.')

    @commands.command()
    @checks.mod_or_permissions(manage_roles=True)
    async def unmute(self, ctx, member: discord.Member, *, reason: str=None):
        """Unmutes a user (obviously)"""
        role = self._get_muted_role(ctx.guild)
        if role not in member.roles:
            return await ctx.send(f"{member} hasn't been muted!")

        await member.remove_roles(role)
        await ctx.send(f'{member.mention} can now speak again... '
                        '\N{SMILING FACE WITH OPEN MOUTH AND COLD SWEAT}')
        # We don't need to do anything with the scheduler tbh.
        # It's just gonna execute normally.

    @commands.command(name='regenmutedperms', aliases=['rmp'])
    @checks.is_owner()
    @commands.guild_only()
    async def regen_muted_perms(self, ctx):
        mute_role = await self._setdefault_muted_role(ctx.guild)
        await self._regen_muted_role_perms(mute_role, *ctx.guild.channels)
        await ctx.send('\N{THUMBS UP SIGN}')
        
    @commands.command(name='setmuterole', aliases=['smur'])
    @checks.admin_or_permissions(manage_roles=True, manage_server=True)
    async def set_muted_role(self, ctx, *, role: discord.Role):
        """Sets the muted role for the server.
        
        Ideally you shouldn't have to do this, as I already create a 
        muted role when I attempt to mute someone.
        This is just in case you already have a muted role and would like to use that one instead.
        """
        await self._regen_muted_role_perms(role, *ctx.guild.channels)
        self.muted_roles[str(ctx.guild.id)] = role.id
        await ctx.send(f'Set the muted role to **{role}**!')
        
    @commands.command(name='muterole', aliases=['mur'])
    async def muted_role(self, ctx):
        """Gets the current muted role."""
        role = self._get_muted_role(ctx.guild)
        msg = ("There is no muted role, either set one now or let me create one for you."
               if role is None else f"The current muted role is **{role}**")
        await ctx.send(msg)

    @commands.command()
    @checks.mod_or_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str=None):
        """Kick a user (obviously)"""

        self._check_user(ctx, member)
        await member.kick(reason=reason)
        await ctx.send(f"Done. Please don't make me do that again...")

    @commands.command(aliases=['sb'])
    @checks.mod_or_permissions(kick_members=True, manage_messages=True)
    async def softban(self, ctx, member: discord.Member, *, reason: str=None):
        """Softbans a user (obviously)"""

        self._check_user(ctx, member)
        await member.ban(reason=reason)
        await member.unban(reason=f'softban (original reason: {reason})')
        await ctx.send("Done. At least he'll be ok...")

    @commands.command(aliases=['tb'])
    @checks.mod_or_permissions(ban_members=True)
    async def tempban(self, ctx, member: discord.Member, duration: duration, *, reason: str=None):
        """Temporarily bans a user (obviously)"""

        self._check_user(ctx, member)
        await ctx.guild.ban(member, reason=reason)
        await ctx.send(f"Done. Please don't make me do that again...")

        # gonna somehow refactor this out soon:tm:
        when = datetime.utcnow() + timedelta(seconds=duration)
        entry = TimerEntry(when.timestamp(), (ctx.guild.id, member.id))
        self.tempban_scheduler.add_entry(entry)
        self.tempbans[_member_key(member)] = entry

    @commands.command()
    @checks.mod_or_permissions(ban_members=True)
    async def ban(self, ctx, member: MemberID, *, reason: str=None):
        """Bans a user (obviously)

        You can also use this to ban someone even if they're not in the server, 
        just use the ID. (not so obviously)
        """

        with contextlib.suppress(AttributeError):
            self._check_user(ctx, member)

        await ctx.guild.ban(member, reason=reason)
        await ctx.send(f"Done. Please don't make me do that again...")

    @commands.command()
    @checks.mod_or_permissions(ban_members=True)
    async def unban(self, ctx, user: MemberID(user_type=discord.User), *, reason: str=None):
        """Unbans the user (obviously)"""

        # Will not remove the scheduler (this is ok)
        await ctx.guild.unban(user)
        await ctx.send("Done. What did they do to get banned in the first place...?")

    @commands.command()
    @checks.is_owner()
    async def massban(self, ctx, reason, *members: MemberID):
        """Bans a series a people (obviously)"""
        for m in members:
            await ctx.guild.ban(m, reason=reason)

        await ctx.send(f"Done. What happened...?")

    mute._required_perms    = 'Manage Roles'
    unmute._required_perms  = 'Manage Roles'
    kick._required_perms    = 'Kick Members'
    for cmd in (softban, tempban, ban, unban):
        cmd._required_perms = 'Ban Members'
    del cmd     # cmd still exists outside the for loop, (which is named as unban...)

    @mute.error
    @unmute.error
    @kick.error
    @softban.error
    @tempban.error
    @ban.error
    @unban.error
    @massban.error
    async def mod_action_error(self, ctx, error):
        # We need to use the __cause__ because any non-CommandErrors will be 
        # wrapped in CommandInvokeError
        cause = error.__cause__
        command = ctx.command

        if isinstance(cause, discord.Forbidden):
            await ctx.send(f'I need the {command._required_perms} permissions to {command}, I think... '
                            "Or maybe they're just too powerful for me.")
        elif isinstance(cause, discord.HTTPException):
            await ctx.send(f"Couldn't {command} the member for some reason")

    # ------------- Case Related Commands ------------------

    def _get_case(self, server, num=None):
        cases = self.cases[server].setdefault('cases', [])
        if num is None:
            return cases
        num -= num > 0
        with redirect_exception((IndexError, f"Couldn't find case {num}."),
                                cls=errors.ResultsNotFound):
            # support negative indexing
            return cases[num]

    @commands.group()
    @checks.admin_or_permissions(manage_server=True)
    async def caseset(self, ctx):
        """Super-command for all mod case-related commands

        Only cases where the bot was used will be logged.
        """
        # TODO: Make like Pollr and log *everything*
        pass

    @caseset.command(name='logchannel', aliases=['channel'])
    async def log_channel(self, ctx, channel: discord.TextChannel):
        """Sets the channel for logging mod cases"""
        if not channel.permissions_for(ctx.me).send_messages:
            raise errors.InvalidUserArgument(f"I can't speak in {channel.mention}. Please give me the Send Messages perm there.\n")

        self.cases[ctx.guild]['case_channel'] = channel.id
        await ctx.send(f"Cases will now be put on {channel.mention}")

    @caseset.command(name='stop')
    async def case_stop(self, ctx):
        """Stops logging the mod-cases."""
        with redirect_exception((KeyError, "There was never a place to log any cases...")):
            del self.cases[ctx.guild]['case_channel']
        await ctx.send(f"Cases will now be put on {channel.mention}")

    @caseset.command(name='reason')
    async def case_reason(self, ctx, num: int, *, reason):
        """Sets the reason for a given mod case"""
        case = self._get_case(ctx.guild, num)
        mod = case['mod']
        if case['type'].lower() == 'warn':
            return await ctx.send("Cannot edit a warn case (it doesn't make sense anyway...)")

        if mod not in (None, ctx.author.id):    
            return await ctx.send("That case is not yours...")

        channel = self.bot.get_channel(case['channel_id'])
        if channel is None:
            return await ctx.send("This channel no longer exists")

        message = await channel.get_message(case['message_id'])
        assert message.author.id == self.bot.user.id

        embed = message.embeds[0].set_field_at(-1, name="Reason", value=reason, inline=False)
        if mod is None:
            case['mod'] = ctx.author.id
            action_field = embed.fields[1]
            new_action = _rreplace(action_field.value, 'None', str(ctx.author), 1)
            embed.set_field_at(1, name=action_field.name, value=new_action, inline=False)

        await message.edit(embed=embed)
        case['reason'] = reason
        await ctx.send(f"Successfully changed case #{num}'s reason to {reason}!")

    @caseset.command(name='reset', aliases=['clear'])
    async def case_reset(self, ctx):
        """Resets all the mod cases. However, this doesn't clear the existing case messages."""
        cases = self._get_case(ctx.guild)
        if not cases:
            raise errors.ResultsNotFound("There are no cases in this server!")

        cases.clear()
        await ctx.send("Successfully cleared the cases for this server!")


    # --------- Events ---------

    async def on_message(self, message):
        await self.check_slowmode(message)
        # Might throw an exception if the message was already deleted.
        with contextlib.suppress(discord.NotFound):
            await self.check_slowuser(message)

    async def on_guild_channel_create(self, channel):
        server = channel.guild
        role = await self._setdefault_muted_role(server)
        if role is None:
            return
        await self._regen_muted_role_perms(role, channel)

    async def on_member_join(self, member):
        # Prevent mute-evasion
        entry = self.mutes.get(_member_key(member))
        if not entry:
            return

        # remove the old entry, we're gonna put a new one in its place anyway.
        with contextlib.suppress(ValueError):
            self.mute_scheduler.remove_entry(entry)

        # mute them for an extra 60 mins
        await self._do_mute(member, entry.when + 3600)

    async def on_command_completion(self, ctx):
        # For all mod-action related commands.
        name = ctx.command.name
        case_type = mod_action_types.get(name)
        if case_type is None:
            return

        _, _, user, duration = (ctx.args + [None])[:4]
        reason = ctx.kwargs['reason']
        case = ModCase(type=mod_action_types[name], mod=ctx.author, user=user, reason=reason)
        await self._send_case(ctx, case, duration=duration)

    # -------- Custom Events (used in schedulers) -----------

    async def on_mute_end(self, timer):
        # Bot.get_guild will return None if there are any pending mutes 
        # when this cog first gets loaded. Thus we have to wait until the bot has logged in.
        await self.bot.wait_until_ready()
        server_id, member_id, mute_role_id = timer.args
        server = self.bot.get_guild(server_id)

        # from here we'll just assume things go normally
        # it doesn't really matter if an exception is thrown at this point
        member = server.get_member(member_id)
        role = discord.utils.get(server.roles, id=mute_role_id)

        await member.remove_roles(role)
        del self.mutes[_member_key(member)]

    async def on_tempban_end(self, timer):
        await self.bot.wait_until_ready()
        server_id, user_id = timer.args
        obj = discord.Object(id=user_id)
        server = obj.guild = self.bot.get_guild(server_id)

        await server.unban(obj, reason='unban from tempban')
        del self.tempbans[_member_key(obj)]


def setup(bot):
    bot.add_cog(Moderator(bot))
