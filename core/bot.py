import asyncio
import asyncqlio
import collections
import contextlib
import discord
import functools
import inspect
import json
import logging
import random
import sys
import traceback

from datetime import datetime
from discord.ext import commands
from more_itertools import always_iterable

from . import context
from .formatter import ChiakiFormatter

from cogs.utils import errors
from cogs.utils.jsonf import JSONFile
from cogs.utils.misc import file_handler
from cogs.utils.scheduler import DatabaseScheduler
from cogs.utils.time import duration_units

# The bot's config file
import config

log = logging.getLogger(__name__)
log.addHandler(file_handler('chiakinanami'))

command_log = logging.getLogger('commands')
command_log.addHandler(file_handler('commands'))


_MINIMAL_PERMISSIONS = [
    'send_messages',
    'embed_links',
    'add_reactions',
    'attach_files'
    "use_external_emojis",
]

_FULL_PERMISSIONS = [
    *_MINIMAL_PERMISSIONS,
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "kick_members",
    "ban_members",
    "create_instant_invite",

    "manage_messages",
    "read_message_history",

    "mute_members",
    "deafen_members",
]

def _make_permissions(*permissions):
    perms = discord.Permissions.none()
    perms.update(**dict.fromkeys(permissions, True))
    return perms

_MINIMAL_PERMISSIONS = _make_permissions(*_MINIMAL_PERMISSIONS)
_FULL_PERMISSIONS = _make_permissions(*_FULL_PERMISSIONS)
del _make_permissions


MAX_FORMATTER_WIDTH = 90

def _callable_prefix(bot, message):
    prefixes = bot.custom_prefixes.get(message.guild.id, bot.default_prefix)
    return commands.when_mentioned_or(*prefixes)(bot, message)

_chiaki_formatter = ChiakiFormatter(width=MAX_FORMATTER_WIDTH, show_check_failure=True)


class Chiaki(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=_callable_prefix,
                         formatter=_chiaki_formatter,
                         description=config.description,
                         pm_help=None)

        try:
            with open('data/command_image_urls.json') as f:
                self.command_image_urls = __import__('json').load(f)
        except FileNotFoundError:
            self.command_image_urls = {}

        self.message_counter = 0
        self.command_counter = collections.Counter()
        self.custom_prefixes = JSONFile('customprefixes.json')
        self.cog_aliases = {}

        self.reset_requested = False

        self.db = asyncqlio.DatabaseInterface(config.postgresql)
        self.loop.run_until_complete(self._connect_to_db())
        self.db_scheduler = DatabaseScheduler(self.db, timefunc=datetime.utcnow)
        self.db_scheduler.add_callback(self._dispatch_from_scheduler)

        for ext in config.extensions:
            # Errors should never pass silently, if there's a bug in an extension,
            # better to know now before the bot logs in, because a restart
            # can become extremely expensive later on, especially with the
            # 1000 IDENTIFYs a day limit.
            self.load_extension(ext)

    def _dispatch_from_scheduler(self, entry):
        self.dispatch(entry.event, entry)

    async def _connect_to_db(self):
        # Unfortunately, while DatabaseInterface.connect takes in **kwargs, and
        # passes them to the underlying connector, the AsyncpgConnector doesn't
        # take them AT ALL. This is a big problem for my case, because I use JSONB
        # types, which requires the type_codec to be set first (they need to be str).
        #
        # As a result I have to explicitly use json.dumps when storing them,
        # which is rather annoying, but doable, since I only use JSONs in two
        # places (reminders and welcome/leave messages).
        await self.db.connect()

    async def close(self):
        await self.db.close()
        await super().close()

    def add_cog(self, cog):
        members = inspect.getmembers(cog)

        # cog aliases
        for alias in getattr(cog, '__aliases__', ()):
            if alias in self.cog_aliases:
                raise discord.ClientException(f'"{alias}" already has a cog registered')
            self.cog_aliases[alias] = cog

        # add to namespace
        cog.__hidden__ = getattr(cog, '__hidden__', False)
        super().add_cog(cog)

    def remove_cog(self, cog_name):
        cog = self.cogs.get(cog_name)
        if cog is None:
            return
        super().remove_cog(cog_name)

        # remove cog aliases
        self.cog_aliases = {alias: real for alias, real in self.cog_aliases.items() if real is not cog}

    @contextlib.contextmanager
    def temp_listener(self, func, name=None):
        """Context manager for temporary listeners"""
        self.add_listener(func, name)
        try:
            yield
        finally:
            self.remove_listener(func)

    async def change_game(self):
        await self.wait_until_ready()
        while True:
            name = random.choice(config.games)
            await self.change_presence(game=discord.Game(name=name, type=0))
            await asyncio.sleep(random.uniform(0.5, 10) * 60)

    def run(self):
        super().run(config.token, reconnect=True)

    def get_guild_prefixes(self, guild):
        proxy_msg = discord.Object(id=None)
        proxy_msg.guild = guild
        return _callable_prefix(self, proxy_msg)

    def get_raw_guild_prefixes(self, guild):
        return self.custom_prefixes.get(guild.id, self.default_prefix)

    async def set_guild_prefixes(self, guild, prefixes):
        prefixes = prefixes or []
        if len(prefixes) > 10:
            raise RuntimeError("You have too many prefixes you indecisive goof!")

        await self.custom_prefixes.put(guild.id, sorted(set(prefixes), reverse=True))

    async def process_commands(self, message):
        ctx = await self.get_context(message, cls=context.Context)

        if ctx.command is None:
            return

        async with ctx.acquire():
            await self.invoke(ctx)

    # --------- Events ----------

    async def on_ready(self):
        print('Logged in as')
        print(self.user.name)
        print(self.user.id)
        print('------')
        self.db_scheduler.run()

        if not hasattr(self, 'appinfo'):
            self.appinfo = (await self.application_info())

        if self.owner_id is None:
            self.owner = self.appinfo.owner
            self.owner_id = self.owner.id
        else:
            self.owner = self.get_user(self.owner_id)

        if not hasattr(self, 'creator'):
            self.creator = await self.get_user_info(239110748180054017)

        if not hasattr(self, 'start_time'):
            self.start_time = datetime.utcnow()

        self.loop.create_task(self.change_game())

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure) and await self.is_owner(ctx.author):
            # Let the old session continue whatever it was doing. We'll just make
            # a new session for this.
            ctx._old_session = ctx.session
            try:
                async with ctx.db.get_session() as ctx.session:
                    await ctx.reinvoke()
            except Exception as exc:
                await ctx.command.dispatch_error(ctx, exc)
            return

        # command_counter['failed'] += 0 sets the 'failed' key. We don't want that.
        if not isinstance(error, commands.CommandNotFound):
            self.command_counter['failed'] += 1

        cause = error.__cause__
        if isinstance(error, errors.ChiakiException):
            await ctx.send(str(error))
        elif type(error) is commands.BadArgument:
            await ctx.send(str(cause or error))
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send('This command cannot be used in private messages.')
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f'This command ({ctx.command}) needs another parameter ({error.param})')
        elif isinstance(error, commands.CommandInvokeError):
            print(f'In {ctx.command.qualified_name}:', file=sys.stderr)
            traceback.print_tb(error.original.__traceback__)
            print(f'{error.__class__.__name__}: {error}'.format(error), file=sys.stderr)

    async def on_message(self, message):
        self.message_counter += 1

        # prevent other selfs from triggering commands
        if not message.author.bot:
            await self.process_commands(message)

    async def on_command(self, ctx):
        self.command_counter['commands'] += 1
        self.command_counter['executed in DMs'] += isinstance(ctx.channel, discord.abc.PrivateChannel)
        fmt = ('Command executed in {0.channel} ({0.channel.id}) from {0.guild} ({0.guild.id}) '
               'by {0.author} ({0.author.id}) Message: "{0.message.content}"')
        command_log.info(fmt.format(ctx))

    async def on_command_completion(self, ctx):
        self.command_counter['succeeded'] += 1

    # ------ Config-related properties ------

    @discord.utils.cached_property
    def minimal_invite_url(self):
        return discord.utils.oauth_url(self.user.id, _MINIMAL_PERMISSIONS)

    @discord.utils.cached_property
    def invite_url(self):
        return discord.utils.oauth_url(self.user.id, _FULL_PERMISSIONS)

    @property
    def default_prefix(self):
        return always_iterable(config.command_prefix)

    @property
    def colour(self):
        return config.colour

    # ------ misc. properties ------

    @property
    def support_invite(self):
        # The following is the link to the bot's support server.
        # You are allowed to change this to be another server of your choice.
        # However, doing so will instantly void your warranty.
        # Change this at your own peril.
        return 'https://discord.gg/WtkPTmE'

    @property
    def uptime(self):
        return datetime.utcnow() - self.start_time

    @property
    def str_uptime(self):
        return duration_units(self.uptime.total_seconds())

    @property
    def all_cogs(self):
        return collections.ChainMap(self.cogs, self.cog_aliases)
