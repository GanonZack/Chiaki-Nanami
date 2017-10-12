import asyncio
import contextlib
import collections
import itertools
import json
import os
import uuid


JSONS_PATH = 'jsonfiles'
os.makedirs(JSONS_PATH, exist_ok=True)


# Shamelessly copied from Danny because he's Danny and he's cool.
class JSONFile(collections.MutableMapping):
    """The "database" object. Internally based on ``json``.

    Basically a wrapper for persistent data, whenever I don't want to use a DB,
    usually because it will get queried a ton (which is always pleasant).
    """
    _transform_key = str

    def __init__(self, name, **options):
        self._name = name
        self._db = {}

        self._loop = options.pop('loop', asyncio.get_event_loop())
        self._lock = asyncio.Lock()
        if options.pop('load_later', False):
            self._loop.create_task(self.load())
        else:
            self.load_from_file()

    def __getitem__(self, key):
        return self._db[self._transform_key(key)]

    def __setitem__(self, key, value):
        self._db[self._transform_key(key)] = value

    def __delitem__(self, key):
        del self._db[self._transform_key(key)]

    def __iter__(self):
        return iter(self._db)

    def __len__(self):
        return len(self._db)

    def load_from_file(self):
        with contextlib.suppress(FileNotFoundError), open(self._name, 'r') as f:
            self._db.update(json.load(f))

    async def load(self):
        async with self._lock:
            await self._loop.run_in_executor(None, self.load_from_file)

    def _dump(self):
        name = f'{JSONS_PATH}/{self._name}'
        temp = f'{name}-{uuid.uuid4()}.tmp'
        with open(temp, 'w', encoding='utf-8') as tmp:
            json.dump(self._db.copy(), tmp, ensure_ascii=True, separators=(',', ':'))

        # atomically move the file
        os.replace(temp, name)

    async def save(self):
        async with self._lock:
            await self._loop.run_in_executor(None, self._dump)

    async def put(self, key, value, *args):
        """Edits a config entry."""
        self._db[self._transform_key(key)] = value
        await self.save()

    async def remove(self, key):
        """Removes a config entry."""
        del self._db[self._transform_key(key)]
        await self.save()

    async def update(self, mapping, **kwargs):
        super().update(mapping, **kwargs)
        await self.save()

