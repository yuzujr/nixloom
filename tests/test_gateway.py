import asyncio
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from nixloom.gateway import Scheduler, applications


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.running = False
        self.completed = False
        self.events = []
        self.session = ClientSession()
        self.servers = []

        async def unload_chat(request):
            self.events.append("chat-unloaded")
            return web.json_response({})

        async def chat(request):
            self.events.append("chat-started")
            return web.json_response({"choices": []})

        async def submit(request):
            value = await request.json()
            if value.get("bad"):
                return web.json_response({"error": "bad graph"}, status=400)
            self.events.append("image-started")
            self.running = True
            self.completed = False
            return web.json_response({"prompt_id": "job"})

        async def queue(request):
            return web.json_response(
                {"queue_running": ["job"] if self.running else [], "queue_pending": []}
            )

        async def history(request):
            return web.json_response(
                {"job": {"status": {"status_str": "success"}}} if self.completed else {}
            )

        async def unload_image(request):
            if self.running:
                return web.json_response({"error": "busy"}, status=409)
            self.events.append("image-unloaded")
            return web.json_response({})

        async def echo(request):
            return web.json_response(
                {"host": request.host, "origin": request.headers.get("Origin")}
            )

        chat_app = web.Application()
        chat_app.router.add_get("/unload", unload_chat)
        chat_app.router.add_post("/v1/chat/completions", chat)
        image_app = web.Application()
        image_app.router.add_post("/prompt", submit)
        image_app.router.add_get("/queue", queue)
        image_app.router.add_get("/history/job", history)
        image_app.router.add_post("/nixloom/unload", unload_image)
        image_app.router.add_get("/echo", echo)
        chat_url, image_url = await self.start(chat_app), await self.start(image_app)
        self.scheduler = Scheduler(self.session, chat_url, image_url)
        apps = applications(self.session, self.scheduler)
        self.chat_url, self.image_url = (
            await self.start(apps[0]),
            await self.start(apps[1]),
        )

    async def start(self, app):
        server = TestServer(app)
        await server.start_server()
        self.servers.append(server)
        return str(server.make_url("")).rstrip("/")

    async def asyncTearDown(self):
        for task in list(self.scheduler.jobs):
            task.cancel()
        await asyncio.gather(*self.scheduler.jobs, return_exceptions=True)
        for server in reversed(self.servers):
            await server.close()
        await self.session.close()

    async def test_chat_waits_for_execution_not_http_acceptance(self):
        async with self.session.post(self.image_url + "/prompt", json={}) as response:
            self.assertEqual(response.status, 200)
        chat = asyncio.create_task(
            self.session.post(self.chat_url + "/v1/chat/completions", json={})
        )
        await asyncio.sleep(0.1)
        self.assertEqual(self.events, ["chat-unloaded", "image-started"])
        self.running, self.completed = False, True
        response = await asyncio.wait_for(chat, 2)
        await response.read()
        response.release()
        self.assertEqual(self.events[-2:], ["image-unloaded", "chat-started"])

    async def test_api_prefixed_prompt_uses_the_same_scheduler(self):
        async with self.session.post(
            self.image_url + "/api/prompt", json={}
        ) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.events, ["chat-unloaded", "image-started"])
        async with self.session.post(
            self.image_url + "/api/nixloom/unload"
        ) as response:
            self.assertEqual(response.status, 404)
        self.running = False

    async def test_rejected_graph_releases_scheduler(self):
        async with self.session.post(
            self.image_url + "/prompt", json={"bad": True}
        ) as response:
            self.assertEqual(response.status, 400)
        async with self.session.post(
            self.chat_url + "/v1/chat/completions", json={}
        ) as response:
            self.assertEqual(response.status, 200)
        self.assertFalse(self.scheduler.lock.locked())

    async def test_cancelled_queue_does_not_block_chat_forever(self):
        async with self.session.post(self.image_url + "/prompt", json={}) as response:
            self.assertEqual(response.status, 200)
        self.running = False
        async with self.session.post(
            self.chat_url + "/v1/chat/completions", json={}
        ) as response:
            self.assertEqual(response.status, 200)

    async def test_private_unload_route_is_not_public(self):
        async with self.session.post(self.image_url + "/nixloom/unload") as response:
            self.assertEqual(response.status, 404)
        self.assertEqual(self.events, [])

    async def test_host_and_origin_remain_consistent_for_backend_csrf_checks(self):
        with (
            patch("nixloom.gateway.socket.gethostname", return_value="laptop-nixos"),
            patch("nixloom.gateway.socket.getfqdn", return_value="laptop-nixos"),
        ):
            async with self.session.get(
                self.image_url + "/echo",
                headers={
                    "Host": "laptop-nixos:8188",
                    "Origin": "http://laptop-nixos:8188",
                },
            ) as response:
                result = await response.json()
        self.assertEqual(
            result, {"host": "laptop-nixos:8188", "origin": "http://laptop-nixos:8188"}
        )

    async def test_foreign_host_cannot_rebind_a_loopback_request(self):
        async with self.session.post(
            self.image_url + "/prompt", json={}, headers={"Host": "evil.example:8188"}
        ) as response:
            self.assertEqual(response.status, 403)
        self.assertEqual(self.events, [])

    async def test_queue_inspection_failure_prevents_a_second_gpu_load(self):
        await self.scheduler.begin_image()
        with patch.object(
            self.scheduler, "json", side_effect=OSError("backend disconnected")
        ):
            await self.scheduler.wait_images("job")
        async with self.session.post(
            self.chat_url + "/v1/chat/completions", json={}
        ) as response:
            self.assertEqual(response.status, 503)
        self.assertEqual(self.events, ["chat-unloaded"])
