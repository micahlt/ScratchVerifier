import os
import time
from secrets import token_bytes, token_hex, randbits
from hashlib import sha256
import asyncio
import aiosqlite as sql

DATABASE_FILENAME = 'scratchverifier.db'
USERS_API = 'https://api.scratch.mit.edu/users/{}'
VERIFY_EXPIRY = 1800 # 30 minutes
SESSION_EXPIRY = 31540000 # 1 year in seconds

class Database:
    def __init__(self, session):
        loop = asyncio.get_event_loop()
        # lock to prevent race conditions when SELECT then fetchone
        self.lock = asyncio.Lock(loop=loop)
        self.dbw = loop.run_until_complete(sql.connect(DATABASE_FILENAME))
        self.dbw.row_factory = sql.Row
        self.db = loop.run_until_complete(self.dbw.cursor())
        with open(os.path.join(os.path.dirname(__file__), 'sql',
                               'startup.sql')) as startup:
            loop.run_until_complete(self.db.executescript(startup.read()))
        self.session = session

    async def close(self):
        await self.dbw.commit()
        await self.dbw.close()

    ### TABLE: clients ###

    async def client_matches(self, client_id, token):
        async with self.lock:
            await self.db.execute('SELECT client_id FROM scratchverifier_clients \
WHERE client_id=? AND token=?', (client_id, token))
            if (await self.db.fetchone()):
                return True
        return False

    ### TABLE: clients and sessions ###

    async def username_from_session(self, session_id):
        async with self.lock:
            await self.db.execute('SELECT username FROM scratchverifier_sessions \
WHERE session_id=?', (session_id,))
            row = await self.db.fetchone()
        if row is None:
            return None
        return row[0]

    async def new_client(self, session_id):
        if session_id == 0: # 0 means debug mode
            return {'client_id': 0, 'username': 'Kenny2scratch', 'token': ''}
        username = await self.username_from_session(session_id)
        if username is None:
            return None
        async with self.session.get(USERS_API.format(username)) as resp:
            assert resp.status == 200
            data = await resp.json()
        client_id = data['id']
        token = token_hex(32)
        await self.db.execute('INSERT INTO scratchverifier_clients (client_id, \
token, username) VALUES (?, ?, ?)', (client_id, token, username))
        return {'client_id': client_id, 'token': token, 'username': username}

    async def get_client(self, session_id):
        if session_id == 0: # 0 means debug mode
            return {'client_id': 0, 'username': 'Kenny2scratch', 'token': ''}
        username = await self.username_from_session(session_id)
        if username is None:
            return None
        async with self.lock:
            await self.db.execute('SELECT * FROM scratchverifier_clients \
WHERE username=?', (username,))
            row = await self.db.fetchone()
        if row is None:
            return None
        return dict(row)

    async def reset_token(self, session_id):
        if session_id == 0: # 0 means debug mode
            return {'client_id': 0, 'username': 'Kenny2scratch', 'token': ''}
        username = await self.username_from_session(session_id)
        if username is None:
            return
        await self.db.execute('UPDATE scratchverifier_clients SET token=? \
WHERE username=?', (token_hex(32), username))

    async def del_client(self, session_id):
        if session_id == 0: # 0 means debug mode
            return
        username = await self.username_from_session(session_id)
        if username is None:
            return
        await self.db.execute('DELETE FROM scratchverifier_clients \
WHERE username=?', (username,))

    ### TABLE: sessions ###

    async def new_session(self, username):
        while 1:
            session_id = randbits(32)
            async with self.lock:
                await self.db.execute('SELECT session_id FROM \
scratchverifier_sessions WHERE session_id=?', (session_id,))
                if (await self.db.fetchone()) is None:
                    break
        await self.db.execute('INSERT INTO scratchverifier_sessions \
(session_id, expiry, username) VALUES (?, ?, ?)', (
            session_id,
            int(time.time()) + SESSION_EXPIRY,
            username
        ))
        await self.db.execute('DELETE FROM scratchverifier_sessions WHERE \
expiry<=?', (int(time.time()),))
        return session_id

    async def get_expired(self, session_id):
        if session_id == 0: # 0 means debug mode
            return False
        async with self.lock:
            await self.db.execute('SELECT expiry FROM scratchverifier_sessions \
WHERE session_id=?', (session_id,))
            expiry = await self.db.fetchone()
        if expiry is None:
            return True
        expiry = expiry[0]
        if time.time() > expiry:
            await self.db.execute('DELETE FROM scratchverifier_sessions \
WHERE session_id=?', (session_id,))
            return True
        return False

    async def logout(self, session_id):
        await self.db.execute('DELETE FROM scratchverifier_sessions \
WHERE session_id=?', (session_id,))

    async def logout_user(self, username):
        await self.db.execute('DELETE FROM scratchverifier_sessions \
WHERE username=?', (username,))

    ### TABLE: usage ###

    async def start_verification(self, client_id, username):
        async with self.lock:
            await self.db.execute('SELECT code FROM scratchverifier_usage WHERE \
client_id=? AND username=?', (client_id, username))
            row = await self.db.fetchone()
        if row is not None:
            await self.db.execute('UPDATE scratchverifier_usage SET expiry=? \
WHERE client_id=? AND username=? AND code=?', (int(time.time()) + VERIFY_EXPIRY,
                                               client_id, username, row[0]))
            return row[0]
        code = sha256(
            str(client_id).encode()
            + str(time.time()).encode()
            + username.encode()
            + token_bytes()
        # 0->A, 1->B, etc, to avoid Scratch's phone number censor
        ).hexdigest().translate({ord('0') + i: ord('A') + i for i in range(10)})
        await self.db.execute('INSERT INTO scratchverifier_usage (client_id, \
code, username, expiry) VALUES (?, ?, ?, ?)', (client_id, code, username,
                               int(time.time() + VERIFY_EXPIRY)))
        await self.db.execute('INSERT INTO scratchverifier_logs (client_id, \
username, log_time, log_type) VALUES (?, ?, ?, ?)', (client_id, username,
                                                     int(time.time()), 1))
        await self.db.execute('DELETE FROM scratchverifier_usage WHERE \
expiry<=?', (int(time.time()),))
        return code

    async def get_code(self, client_id, username):
        async with self.lock:
            await self.db.execute('SELECT code, expiry FROM scratchverifier_usage \
WHERE client_id=? AND username=?', (client_id, username))
            row = await self.db.fetchone()
        if row is None:
            return None
        if time.time() > row['expiry']:
            await self.end_verification(client_id, username, False)
            return None
        return row['code']

    async def end_verification(self, client_id, username, succ=True):
        await self.db.execute('DELETE FROM scratchverifier_usage WHERE \
client_id=? AND username=?', (client_id, username))
        await self.db.execute('INSERT INTO scratchverifier_logs (client_id, \
username, log_time, log_type) \
VALUES (?, ?, ?, ?)', (client_id, username, int(time.time()), 3 - succ))

    ### TABLE: logs solely ###

    async def get_logs(self, limit=100, **params):
        query = 'SELECT * FROM scratchverifier_logs WHERE 1=1'
        if 'start' in params:
            query += ' AND log_id<:start'
        if 'before' in params:
            query += ' AND log_time<=:before'
        if 'end' in params:
            query += ' AND log_id>:end'
        if 'after' in params:
            query += ' AND log_time>=:after'
        if 'client_id' in params:
            query += ' AND client_id=:client_id'
        if 'username' in params:
            query += ' AND username=:username'
        if 'type' in params:
            query += ' AND log_type=:type'
        query += ' ORDER BY log_id DESC LIMIT :limit'
        for k, v in params.items():
            if k in {'start', 'before', 'end', 'after', 'client_id', 'type'}:
                params[k] = int(v)
        limit = int(limit)
        async with self.lock:
            await self.db.execute(query, {'limit': limit, **params})
            rows = await self.db.fetchall()
        return [dict(i) for i in rows]

    async def get_log(self, log_id):
        async with self.lock:
            await self.db.execute('SELECT * FROM scratchverifier_logs \
WHERE log_id=?', (log_id,))
            row = await self.db.fetchone()
        if row is None:
            return None
        return dict(row)
