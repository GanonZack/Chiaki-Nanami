import itertools

from more_itertools import chunked, pairwise, sliced, spy

BLACK, WHITE = False, True
PIECES = BK_PIECE, WH_PIECE = 'bw'
KINGS = BK_KING, WH_KING = 'BW'

_is_king = str.isupper

def _get_checkers(start, end, direction):
    return [
        (x, y) for y, x in itertools.product(range(start, end + direction, direction), range(8))
        if (x + y) % 2 == 1
    ]

_STARTING_BOARD = [
    ' ', BK_PIECE, ' ', BK_PIECE, ' ', BK_PIECE, ' ', BK_PIECE,
    BK_PIECE, ' ', BK_PIECE, ' ', BK_PIECE, ' ', BK_PIECE, ' ',
    ' ', BK_PIECE, ' ', BK_PIECE, ' ', BK_PIECE, ' ', BK_PIECE,
    ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ',
    ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ',
    WH_PIECE, ' ', WH_PIECE, ' ', WH_PIECE, ' ', WH_PIECE, ' ',
    ' ', WH_PIECE, ' ', WH_PIECE, ' ', WH_PIECE, ' ', WH_PIECE,
    WH_PIECE, ' ', WH_PIECE, ' ', WH_PIECE, ' ', WH_PIECE, ' ',
]

X = 'abcdefgh'
Y = '87654321'

def _to_i(x, y):
    return y * 8 + x

def _i_to_xy(i):
    y, x = divmod(i, 8)
    return X[x] + Y[y]

def _xy_to_i(xy):
    x, y = xy
    return _to_i(X.index(x), Y.index(y))

def _in_range(x, y):
    return 0 <= x < 8 and 0 <= y < 8

def _moves(x, y, dy):
    return [_to_i(x + dx, y + dy) for dx in (-1, 1) if _in_range(x + dx, y + dy)]

def _captures(x, y, dy):
    return [
        (_to_i(x + dx, y + dy), _to_i(x + dx * 2, y + dy * 2))
        for dx in (-1, 1)
        if _in_range(x + dx, y + dy) and _in_range(x + dx * 2, y + dy * 2)
    ]

def _make_dict(f):
    moves = {}
    moves[BK_PIECE] = {_to_i(x, y): f(x, y, 1) for x, y in _get_checkers(0, 8, 1)}
    moves[WH_PIECE] = {_to_i(x, y): f(x, y, -1) for x, y in _get_checkers(8, 0, -1)}
    # Kings can move anywhere
    moves[BK_KING] = moves[WH_KING] = {
        k: moves[WH_PIECE].get(k, []) + moves[BK_PIECE].get(k, [])
        for k in moves[BK_PIECE].keys() | moves[WH_PIECE].keys()
    }
    return moves

# Generate lookup table for moves
_MOVES = _make_dict(_moves)
_CAPTURES = _make_dict(_captures)


class Board:
    TILES = {
        BLACK: '\N{BLACK LARGE SQUARE}',
        WHITE: '\N{WHITE LARGE SQUARE}',
        BK_PIECE: '\N{LARGE RED CIRCLE}',
        WH_PIECE: '\N{LARGE BLUE CIRCLE}',
        BK_KING: '\N{HEAVY BLACK HEART}',
        WH_KING: '\N{BLUE HEART}',
    }
    X = '\u200b'.join(map(chr, range(0x1f1e6, 0x1f1ee)))
    Y = [f'{i}\u20e3' for i in Y]

    def __init__(self):
        self._board = _STARTING_BOARD[:]
        self._half_moves = 0
        self.turn = WHITE

    def __str__(self):
        board = '\n'.join(f'{y}{"".join(chunk)}' for y, chunk in zip(self.Y, chunked(self._tiles(), 8)))
        return f'\N{BLACK LARGE SQUARE}{self.X}\n{board}'

    @property
    def half_moves(self):
        return self._half_moves

    def _tiles(self):
        tiles = self.TILES
        for i, char in enumerate(self._board):
            key = not sum(divmod(i, 8)) % 2 if char == ' ' else char
            yield tiles[key]

    def _find_all_pieces(self, colour):
        return [i for i, v in enumerate(self._board) if v.lower() == colour]

    def legal_moves(self):
        """Generate all legal moves in the current position.

        If there are any jumps one could make, those get generated instead,
        as jumps must be made according to the rules of Checkers.
        """
        jumps_exist, jumps = spy(self.jumps())
        if jumps_exist:
            yield from jumps
            return

        board = self._board
        for i in self._find_all_pieces(PIECES[self.turn]):
            for end in _MOVES[board[i]][i]:
                if board[end] == ' ':
                    yield _i_to_xy(i) + _i_to_xy(end)

    def jumps(self):
        """Generate all jumps one can make in the current position"""
        owner = PIECES[self.turn]
        return itertools.chain.from_iterable(map(self.jumps_from, self._find_all_pieces(owner)))

    def jumps_from(self, square):
        """Generate all jumps from a particular square in the current position"""
        board = self._board
        captures = _CAPTURES[board[square]]

        def jump_helper(square, captured):
            is_king = _is_king(board[square])
            for jump_over, jump_end in captures[square]:
                if board[jump_over].lower() != PIECES[not self.turn]:
                    continue

                if jump_over in captured:
                    # no loops
                    continue

                if board[jump_end] != ' ':
                    # The square must be empty (obviously)
                    continue

                if not is_king and square >> 3 == 7 * self.turn:
                    # When a piece reaches the back rank they're supposed to be
                    # kinged and can't jump anymore. Exception is if the piece
                    # was already a king.
                    yield square, jump_end
                else:
                    chain_exists, squares = spy(jump_helper(jump_end, captured | {jump_over}))
                    if chain_exists:
                        for sequence in squares:
                            yield (square, *sequence)
                    else:
                        yield (square, jump_end)

        return (''.join(map(_i_to_xy, s)) for s in jump_helper(square, set()))

    def is_game_over(self):
        """Return True if the game is over for the current player. False otherwise."""
        return next(self.legal_moves(), None) is None

    def move(self, move):
        """Take a move and apply it to the game"""
        if move not in self.legal_moves():
            raise ValueError(f'illegal move: {move!r}')

        board = self._board
        squares = [_xy_to_i(xy) for xy in sliced(move, 2)]
        end = squares[-1]

        piece = board[squares[0]]
        if end >> 3 == 7 * (not self.turn) and not _is_king(piece):
            # New king
            piece = piece.upper()

        for before, after in pairwise(squares):
            difference = abs(before - after)
            if difference not in {18, 14}:
                continue

            # A two step rather than a one step means a capture.
            square_between = min(before, after) + difference // 2
            board[square_between] = ' '

        board[squares[0]] = ' '
        board[end] = piece
        self._half_moves += 1
        self.turn = not self.turn


# Below is the game logic. If you just want to copy the board, Ignore this.
import asyncio
import contextlib
import random
import re

import discord

from .bases import Status, TwoPlayerGameCog
from ..utils.context_managers import temp_message
from ..utils.misc import emoji_url


_VALID_MOVE_REGEX = re.compile(r'^([a-h][1-8]\s?)+', re.IGNORECASE)
_MESSAGES = {
    Status.PLAYING: 'Your turn, {user}',
    Status.END: '{user} wins!',
    Status.QUIT: '{user} forefited...',
    Status.TIMEOUT: '{user} ran out of time...',
}


def _safe_sample(population, k):
    # random.sample complains if the number of items is less than k.
    # We don't care about that really.
    return random.sample(population, min(k, len(population)))


class CheckersSession:
    def __init__(self, ctx, opponent):
        self._ctx = ctx
        self._players = random.sample((ctx.author, opponent), 2)

        self._status = Status.PLAYING
        self._display = discord.Embed(colour=ctx.bot.colour)

        self._board = Board()
        if ctx.bot_has_permissions(external_emojis=True):
            config = ctx.bot.emoji_config
            self._board.TILES = {
                **self._board.TILES,
                BK_KING: str(config.checkers_black_king),
                WH_KING: str(config.checkers_white_king),
            }

    def _check(self, message):
        if not (message.channel == self._ctx.channel and message.author == self.current):
            return False

        if message.content.lower() in {'stop', 'quit'}:
            self._status = Status.QUIT
            return True

        lowered = message.content.lower()
        if not _VALID_MOVE_REGEX.match(lowered):
            return False

        try:
            self._board.move(''.join(lowered.split()))
        except ValueError:
            pass
        else:
            return True

    def _instructions(self):
        if self._board.half_moves >= 4:
            return ''

        sample = _safe_sample(list(self._board.legal_moves()), 5)
        joined = ', '.join(f'`{c}`' for c in sample)
        return (
            '**Instructions:**\n'
            'Type the position of the piece you want to move,\n'
            'and where you want to move it.\n'
            f'**Example:**\n{joined}\n\u200b\n'
        )

    def _update_display(self):
        board = self._board

        if self._status is Status.PLAYING:
            instructions = self._instructions()
            icon = emoji_url(board.TILES[PIECES[board.turn]])
        else:
            instructions = ''
            icon = discord.Embed.Empty

        if self._status is Status.END:
            user = self._players[not self._board.turn]
        else:
            user = self.current

        header = _MESSAGES[self._status].format(user=user)
        self._display.description = f'{instructions}{board}'
        self._display.set_author(name=header, icon_url=icon)

    async def _loop(self):
        wait_for = self._ctx.bot.wait_for
        # needed cuz we're looking this up a few times
        resigned = Status.QUIT

        while not self._board.is_game_over():
            self._update_display()
            async with temp_message(self._ctx, embed=self._display):
                try:
                    user_message = await wait_for('message', timeout=120, check=self._check)
                except asyncio.TimeoutError:
                    self._status = Status.TIMEOUT
                    return

                with contextlib.suppress(Exception):
                    await user_message.delete()

                if self._status is resigned:
                    return

        self._status = Status.END

    async def run(self):
        await self._loop()
        self._update_display()
        await self._ctx.send(embed=self._display)

    @property
    def current(self):
        return self._players[self._board.turn]

class Checkers(TwoPlayerGameCog, game_cls=CheckersSession):
    async def _end_game(self, ctx, inst, result):
        pass

def setup(bot):
    bot.add_cog(Checkers(bot))
