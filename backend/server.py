import os
import re
import time
from glob import glob
import asyncio
import mimetypes
from aiohttp import web, ClientSession, BasicAuth
from db import Database, USERS_API
from responses import *

DEFAULT_PORT = 8888
COMMENTS_API = 'https://scratch.mit.edu/site-api/comments/user/{}/\
?page=1&salt={}'
COMMENTS_REGEX = re.compile(r"""<div id="comments-\d+" class="comment +" data-comment-id="\d+">.*?<div class="actions-wrap">.*?<div class="name">\s+<a href="/users/([_a-zA-Z0-9-]+)">\1</a>\s+</div>\s+<div class="content">(.*?)</div>""", re.S)

class Server:
    def __init__(self):
        self.app = web.Application()
        self.app.add_routes([
            web.put('/verify/{username}', self.verify),
            web.post('/verify/{username}', self.verified),
            web.post('/users/{username}/login', self.login),
            web.post('/users/{username}/finish-login', self.finish_login),
            web.get('/session/{session}', self.get_user),
            web.put('/session/{session}', self.put_user),
            web.patch('/session/{session}', self.reset_token),
            web.delete('/session/{session}', self.del_user),
            web.get('/site/{path:.*}', self.file_handler),
            web.view('/{path:.*}', self.not_found)
        ])
        self.session = ClientSession()
        self.db = Database(self.session)

    async def run(self, port=DEFAULT_PORT):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', port)
        await site.start()

    async def stop(self):
        await self.runner.cleanup()
        await self.session.close()
        await self.db.close()

    async def _wakeup(self):
        files = glob(os.path.join(os.path.dirname(__file__), '*.py'))
        files = {f: os.path.getmtime(f) for f in files}
        while 1:
            try:
                for i in files:
                    if os.path.getmtime(i) > files[i]:
                        return
                await asyncio.sleep(1)
            except:
                return

    def run_sync(self, port=DEFAULT_PORT):
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(self.run(port))
            loop.run_until_complete(self._wakeup())
        finally:
            loop.run_until_complete(self.stop())

    async def check_token(self, request):
        if 'Authorization' not in request.headers:
            raise web.HTTPUnauthorized()
        auth = BasicAuth.decode(request.headers['Authorization'])
        client_id = int(auth.login)
        if not await self.db.client_matches(client_id, auth.password):
            raise web.HTTPUnauthorized()
        return client_id

    async def check_username(self, request):
        username = request.match_info.get('username', None)
        if not username or not 3 <= len(username) <= 20:
            raise web.HTTPBadRequest()
        async with self.session.get(USERS_API.format(username)) as resp:
            if resp.status != 200:
                raise web.HTTPNotFound()
        return username.casefold()

    async def verify(self, request):
        client_id = await self.check_token(request)
        username = await self.check_username(request)
        code = await self.db.start_verification(client_id, username)
        return web.json_response(Verification(
            code=code, username=username
        ).d())

    async def _verified(self, client_id, username):
        code = await self.db.get_code(client_id, username)
        if not code:
            raise web.HTTPNotFound()
        async with self.session.get(COMMENTS_API.format(
            username, int(time.time())
        )) as resp:
            if resp.status != 200:
                raise web.HTTPNotFound() #likely banned or something
            data = await resp.text()
        await self.db.end_verification(client_id, username)
        data = data.strip()
        if not data:
            return False #no comments at all, failed
        for m in re.finditer(COMMENTS_REGEX, data):
            if m.group(1).casefold() != username:
                continue
            if m.group(2).strip() == code:
                break
        else:
            return False #nothing was the code, failed
        return True

    async def verified(self, request):
        client_id = await self.check_token(request)
        username = await self.check_username(request)
        if await self._verified(client_id, username):
            raise web.HTTPNoContent()
        else:
            raise web.HTTPForbidden()

    async def login(self, request):
        username = await self.check_username(request)
        code = await self.db.start_verification(0, username)
        return web.json_response(Verification(
            code=code, username=username
        ).d())

    async def finish_login(self, request):
        username = await self.check_username(request)
        if await self._verified(client_id, username):
            return web.json_response(Session(
                session=await self.db.new_session(username)
            ).d())
        else:
            raise web.HTTPUnauthorized()

    async def check_session(self, request):
        session = request.match_info.get('session', request.query.get('session',
                                                                      None))
        if not (session or session.strip()):
            raise web.HTTPUnauthorized()
        session_id = int(session)
        if await self.db.get_expired(session_id):
            raise web.HTTPUnauthorized()
        return session_id

    async def get_user(self, request):
        session_id = await self.check_session(request)
        data = await self.db.get_client(session_id)
        if data is None:
            raise web.HTTPNotFound()
        return web.json_response(data)

    async def put_user(self, request):
        session_id = await self.check_session(request)
        data = await self.db.get_client(session_id)
        if data is not None:
            raise web.HTTPConflict()
        data = await self.db.new_client(session_id)
        return web.json_response(data)

    async def reset_token(self, request):
        session_id = await self.check_session(request)
        await self.db.reset_token(session_id)
        return self.get_user(request)

    async def del_user(self, request):
        session_id = await self.check_session(request)
        await self.db.del_client(session_id)
        raise web.HTTPNoContent()

    async def file_handler(self, request):
        WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'public')
        PATH = request.match_info.get('path', 'index.html') or 'index.html'
        print(repr(PATH))
        if '.' not in PATH.split('/')[-1]:
            PATH += '/index.html'
        FILE = os.path.join(WEB_ROOT, PATH)
        if request.if_modified_since:
            if os.path.getmtime(FILE) <= \
                    request.if_modified_since.total_seconds():
                raise web.HTTPNotModified()
        if request.if_unmodified_since:
            if os.path.getmtime(FILE) > \
                    request.if_unmodified_since.total_seconds():
                raise web.HTTPPreconditionFailed()
        try:
            with open(FILE, 'rb') as f:
                range = request.http_range
                f.seek(range.start or 0)
                data = f.read(((range.stop or 0) - (range.start or 0)) or -1)
            ct = mimetypes.guess_type(PATH)
            return web.Response(body=data, content_type=ct[0], charset=ct[1])
        except FileNotFoundError:
            raise web.HTTPNotFound()

    async def not_found(self, request):
        raise web.HTTPNotFound()