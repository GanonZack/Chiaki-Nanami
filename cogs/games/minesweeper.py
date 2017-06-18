import asyncio
import contextlib
import discord
import enum
import itertools
import random
import textwrap
import time

from datetime import datetime
from discord.ext import commands

from .manager import SessionManager

from ..utils.converter import in_, ranged
from ..utils.database import Database
from ..utils.paginator import BaseReactionPaginator, page


class MinesweeperException(Exception):
    pass


class HitMine(MinesweeperException):
    def __init__(self, x, y):
        super().__init__(f'hit a mine on {x + 1} {y + 1}')


class Level(enum.Enum):
    beginner = enum.auto()
    intermediate = enum.auto()
    expert = enum.auto()
    custom = enum.auto()

    def __str__(self):
        return self.name.title()

    @classmethod
    async def convert(cls, ctx, arg):
        lowered = arg.lower()
        try:
            return cls[lowered]
        except KeyError:
            raise commands.BadArgument(f'No level called {arg}.') from None

class FlagType(enum.Enum):
    default = None
    f = 'flag'
    flag = 'flag'
    u = 'unsure'
    unsure = 'unsure'


class Tile(enum.Enum):
    blank = '\N{WHITE LARGE SQUARE}'
    flag = '\N{TRIANGULAR FLAG ON POST}'
    mine = '\N{EIGHT POINTED BLACK STAR}'
    shown = '\N{BLACK LARGE SQUARE}'
    unsure = '\N{BLACK QUESTION MARK ORNAMENT}'
    boom = '\N{COLLISION SYMBOL}'

    def __str__(self):
        return self.value

    @staticmethod
    def numbered(number):
        return f'{number}\U000020e3'


SURROUNDING = ((-1, -1), (-1,  0), (-1,  1),
               (0 , -1),           (0 ,  1),
               (1 , -1), (1 ,  0), (1 ,  1))


class Board:
    def __init__(self, width, height, mines):
        if mines > width * height:
            raise ValueError(f'Too many mines (expected max {width * height}, got {mines})')
        if mines <= 0:
            raise ValueError("A least one mine is required")
        
        self._board = [[Tile.blank] * width for _ in range(height)]
        self.visible = set()
        self.flags = set()
        self.unsures = set()

        coords = list(itertools.product(range(self.width), range(self.height)))
        random.shuffle(coords)
        self.mines = set(itertools.islice(coords, mines))

    def __contains__(self, xy):
        return 0 <= xy[0] < self.width and 0 <= xy[1] < self.height

    def __repr__(self):
        return f'{type(self).__name__}({self.width}, {self.height}, {len(self.mines)})'

    def __str__(self):
        padding = len(str(self.width - 1))
        numbers = ''.join(map(str, range(self.height)))
        board_string = ''# f"Mines: {len(self.mines)}\n"#  {numbers :>{padding + 1}}\n"
        board_string += '\n'.join([f"`{i :<{padding + 1}}\u200b`{' '.join(map(str, cells))}"
                                   for i, cells in enumerate(self._board, start=1)])
        print(len(board_string))
        #board_string += f"\n  {numbers}"
        return board_string

    def is_mine(self, x, y):
        return (x, y) in self.mines

    def is_flag(self, x, y):
        return (x, y) in self.flags

    def is_visible(self, x, y):
        return (x, y) in self.visible

    def is_unsure(self, x, y):
        return (x, y) in self.unsures

    def _get_neighbours(self, x, y):
        pairs = ((x + surr_x, y + surr_y) for (surr_x, surr_y) in SURROUNDING)
        return (p for p in pairs if p in self)

    def show(self, x, y):
        if self.is_visible(x, y):
            return

        self.visible.add((x, y))
        if self.is_mine(x, y) and not self.is_flag(x, y):
            raise HitMine(x, y)

        surrounding = sum(self.is_mine(nx, ny) for nx, ny in self._get_neighbours(x, y))
        if not surrounding:
            self._board[y][x] = Tile.shown
            for nx, ny in self._get_neighbours(x, y):
                self.show(nx, ny)
        else:
            self._board[y][x] = Tile.numbered(surrounding)

    def _modify_board(self, x, y, attr):
        if self.is_visible(x, y):
            return

        tup = x, y
        was_thing = getattr(self, f'is_{attr}')(x, y)
        for thing in ('flags', 'unsures'):
            getattr(self, thing).discard(tup)

        if was_thing:
            self._board[y][x] = Tile.blank
        else:
            getattr(self, f'{attr}s').add(tup)
            self._board[y][x] = getattr(Tile, attr)

    def flag(self, x, y):
        self._modify_board(x, y, 'flag')

    def unsure(self, x, y):
        self._modify_board(x, y, 'unsure')

    def reveal_mines(self):
        for mx, my in self.mines:
            self._board[my][mx] = Tile.boom

    def hide_mines(self):
        for mx, my in self.mines:
            self._board[my][mx] = Tile.blank

    def explode(self, x, y):
        if not self.is_visible(x, y):
            return
        self._board[y][x] = Tile.boom

    def is_solved(self):
        return len(self.visible) + len(self.mines) == self.width * self.height

    @property
    def width(self):
        return len(self._board[0])

    @property
    def height(self):
        return len(self._board)

    @property
    def mines_marked(self):
        return len(self.flags)

    @property
    def remaining_flags(self):
        return len(self.mines) - self.mines_marked

    @property
    def remaining_mines(self):
        return len(self.mines - self.flags)

    @classmethod
    def beginner(cls):
        """Returns a beginner minesweeper board"""
        return cls(9, 9, 10)

    @classmethod
    def intermediate(cls):
        """Returns a intermediate minesweeper board"""
        return cls(12, 12, 20)

    @classmethod
    def expert(cls):
        """Returns an expert minesweeper board"""
        return cls(14, 13, 40)


class MinesweeperDisplay(BaseReactionPaginator):
    class State(enum.Enum):
        GAME = enum.auto()
        HELP = enum.auto()

    def __init__(self, context, game):
        super().__init__(context)
        context.game_stopped = False
        self.state = None
        self.game = game

    def _board_repr(self):
        small_squares = itertools.cycle(('\N{WHITE SMALL SQUARE}', '\N{BLACK SMALL SQUARE}'))
        top_row = ' '.join(itertools.islice(small_squares, self.board.width))
        # Discord strips any leading and trailing spaces.
        # By putting a zero-width space we bypass that
        return f'\u200b     {top_row}\n{self.board}' 

    @property
    def board(self):
        return self.game.board

    @page('\N{INPUT SYMBOL FOR NUMBERS}')
    def default(self):
        """Shows the default game screen"""
        self.state = self.State.GAME
        board = self.board
        return (discord.Embed(colour=self.context.bot.colour, description=self._board_repr())
               .set_author(name=f'Minesweeper - {board.width} x {board.height}')
               .add_field(name='Player', value=self.context.author)
               .add_field(name='Mines Found', value=f'{board.mines_marked} / {len(board.mines)}')
               .add_field(name='Flags Remaining', value=board.remaining_flags)
               )

    @page('\N{INFORMATION SOURCE}')
    def help_page(self):
        """Shows this page"""
        self.state = self.State.HELP
        text = textwrap.dedent('''
        Basically the goal is to reveal all of the board and NOT get hit with a mine!

        To make a move, send a message in this format:
        ```
        <x> <y> [f|flag|u|unsure]
        ```
        Inputting `f` or `flag` will mark the tile with a flag.
        Inputting `u` or `unsure` will mark the tile as unsure.
        Inputting nothing, well you know what it will do.

        Note that you can only input it if you're in this actual game.
        (ie inputting anything in this screen won't do anything.)
        ''')
        return (discord.Embed(colour=self.context.bot.colour, description=text)
               .set_author(name='Welcome to Minesweeper!')
               )

    @page('\N{BLACK SQUARE FOR STOP}')
    def stop(self):
        self.game.stop()
        return super().stop()


class MinesweeperSession:
    def __init__(self, ctx, board):
        self.board = board
        self.ctx = ctx
        self._interaction = None
        self._runner = None
        self._game_screen = MinesweeperDisplay(ctx, self)

    def check_message(self, message):
        if self._game_screen.state != MinesweeperDisplay.State.GAME:
            return False

        return (message.channel == self.ctx.channel and
                message.author == self.ctx.author)

    def parse_message(self, content):
        splitted = content.split()
        chars = len(splitted)
        # print(chars, self.board.width, self.board.height)
        if chars == 2:
            flag = FlagType.default
        elif chars == 3:
            flag = getattr(FlagType, splitted[2].lower(), FlagType.default)
        else:
            return None

        try:
            # offset for the fact that the board starts at 1
            x, y = int(splitted[0]) - 1, int(splitted[1]) - 1

        except ValueError:
            return None
        else: 
            if (x, y) not in self.board:
                return None
            return x, y, flag

    async def edit_board(self, new_colour=None):
        embed = self._game_screen.default()
        if new_colour is not None:
            embed.colour = new_colour

            if not new_colour:
                embed.set_author(name='Minesweeper stopped.')
        
        await self._game_screen.message.edit(embed=embed)

    async def _loop(self):
        start = time.perf_counter() 
        while True:
            colour = None
            try:
                message = await self.ctx.bot.wait_for('message', timeout=120, check=self.check_message)
            except asyncio.TimeoutError:
                await self.ctx.send('You took too long!')
                break

            parsed = self.parse_message(message.content)
            if parsed is None:      # garbage input, ignore.
                continue
            x, y, thing = parsed
            await message.delete()

            try:
                if thing.value:
                    getattr(self.board, thing.value)(x, y)
                else:
                    self.board.show(x, y)
            except HitMine:
                self.board.explode(x, y)
                await self.edit_board(0xFFFF00)
                await asyncio.sleep(random.uniform(0.5, 1))
                self.board.reveal_mines()
                colour = 0xFF0000
                raise
            except Exception as e:
                await self.ctx.send(f'An error happened.\n```\y\n{type(e).__name__}: {e}```')
                raise
            else:
                if self.board.is_solved():
                    colour = 0x00FF00
                    return time.perf_counter() - start
            finally:
                await self.edit_board(colour)

    async def run_loop(self): 
        try:
           return await self._loop()
        except asyncio.CancelledError:
            await self.edit_board(0)
            raise
        return None

    async def run(self):
        self._interaction = asyncio.ensure_future(self._game_screen.interact(timeout=None, delete_after=False))
        self._runner = asyncio.ensure_future(self.run_loop())
        await self._game_screen.wait_until_ready()
        try:
            return await self._runner
        finally:
            self._interaction.cancel()
       
    def stop(self):
        for task in (self._runner, self._interaction):
            with contextlib.suppress(BaseException):
                task.cancel()


class Minesweeper:
    def __init__(self, bot):
        self.bot = bot
        self.manager_bucket = {level: SessionManager() for level in Level}
        # self.leaderboard = Database('minesweeperlb.json')

    async def _do_minesweeper(self, ctx, level, board, *, record_time=True):
        manager = self.manager_bucket[level]
        session = manager.get_session(ctx.author.id)
        if session is not None:
            return await ctx.send(f'You already have a {level} Minesweeper game '
                                  f'in {session.ctx.channel} from {session.ctx.guild}')

        await ctx.send(f'Starting a {level} minesweeper game...')
        with manager.temp_session(ctx.author.id, MinesweeperSession(ctx, board)) as inst:
            time = await inst.run()
            text = f'You beat game in {time: .2f} seconds.'
            win_embed = (discord.Embed(title='A winner is you!', colour=0x00FF00, timestamp=datetime.utcnow(), description=text)
                        .set_thumbnail(url=ctx.author.avatar_url)
                        )
            await ctx.send(embed=win_embed)
            if not record_time or time is None:
                return

    @commands.group(aliases=['msw'], invoke_without_command=True)
    async def minesweeper(self, ctx, level: Level=Level.beginner):
        board = getattr(Board, str(level).lower())()
        """Starts a game of Minesweeper."""
        await self._do_minesweeper(ctx, level, board)

    @minesweeper.command(name='custom')
    async def minesweeper_custom(self, ctx, width: ranged(7, 15), height: ranged(7, 15), mines: int):
        """Starts a custom minesweeper game."""
        board = Board(width, height, mines)
        await self._do_minesweeper(ctx, Level.custom, board, record_time=False)

    @minesweeper.error
    @minesweeper_custom.error
    async def minesweeper_error(self, ctx, error):
        cause = error.__cause__

        if isinstance(cause, ValueError):
            await ctx.send(cause)
        if isinstance(cause, HitMine):
            await ctx.send(f'You {cause}... ;-;')
        if isinstance(cause, asyncio.CancelledError):
            await ctx.send(f'Ok, cya later...')

    @minesweeper.command(name='stop')
    async def minesweeper_stop(self, ctx, level: Level):
        """Stops a currently running minesweeper game.

        Ideally, you should not have to call this, because the game already 
        has a stop button in place.
        """
        manager = self.manager_bucket[level]
        session = manager.get_session(ctx.author.id)
        if session is None:
            return await ctx.send("You don't have a {level} minesweeper running...")

        session.stop()

    @minesweeper.command(name='leaderboard', aliases=['lb'])
    async def minesweeper_leaderboard(self, ctx, level: Level):
        pass

    @minesweeper.command(name='rank')
    async def minesweeper_rank(self, ctx, level: Level):
        pass

def setup(bot):
    bot.add_cog(Minesweeper(bot))
