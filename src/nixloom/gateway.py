"""One GPU scheduler for chat, browser workflows, and DSH image tools."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import signal
import socket
import subprocess
import sys
import urllib.parse
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

from . import comfy, runtime
from .config import Config, ConfigError, RuntimePaths

HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }
)


class Scheduler:
    def __init__(self, session: ClientSession, chat: str, images: str):
        self.session, self.chat, self.images = session, chat, images
        self.lock = asyncio.Lock()
        self.jobs: set[asyncio.Task] = set()
        self.failure: str | None = None

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        async with self.session.request(method, url, **kwargs) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def begin_image(self) -> None:
        await self.lock.acquire()
        try:
            if self.failure:
                raise ConfigError(self.failure)
            async with self.session.get(self.chat + "/unload") as response:
                response.raise_for_status()
                await response.read()
        except BaseException:
            self.lock.release()
            raise

    async def wait_images(self, prompt_id: str) -> None:
        try:
            # A prompt can finish before the first queue poll, so wait for the
            # accepted ID to appear in history as well as an empty queue.
            while True:
                history = await self.json("GET", self.images + "/history/" + prompt_id)
                queue = await self.json("GET", self.images + "/queue")
                if (
                    prompt_id in history
                    and not queue["queue_running"]
                    and not queue["queue_pending"]
                ):
                    return
                if not queue["queue_running"] and not queue["queue_pending"]:
                    # A cancelled/deleted queued task may have no history.
                    return
                await asyncio.sleep(0.25)
        except (ClientError, OSError, ValueError, KeyError, TypeError) as error:
            self.failure = f"image queue could not be checked: {error}; restart NixLoom"
            print(self.failure, file=sys.stderr, flush=True)
        finally:
            self.lock.release()

    def track(self, prompt_id: str) -> None:
        task = asyncio.create_task(self.wait_images(prompt_id))
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

    async def begin_chat(self) -> None:
        await self.lock.acquire()
        try:
            if self.failure:
                raise ConfigError(self.failure)
            # This synchronous endpoint runs only while the queue is idle. The
            # stock /free route schedules unloading later and cannot acknowledge
            # that the GPU is actually free before llama.cpp starts loading.
            await self.json("POST", self.images + "/nixloom/unload")
        except BaseException:
            self.lock.release()
            raise


async def websocket(
    request: web.Request, session: ClientSession, url: str
) -> web.WebSocketResponse:
    downstream = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024)
    async with session.ws_connect(url, max_msg_size=64 * 1024 * 1024) as upstream:
        await downstream.prepare(request)

        async def copy(source: Any, target: Any) -> None:
            async for message in source:
                if message.type == WSMsgType.TEXT:
                    await target.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await target.send_bytes(message.data)
                else:
                    break
            await target.close()

        await asyncio.gather(copy(downstream, upstream), copy(upstream, downstream))
    return downstream


async def relay(
    request: web.Request, session: ClientSession, base: str
) -> web.StreamResponse:
    url = base + request.rel_url.raw_path_qs
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await websocket(request, session, url)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    body = await request.read()
    async with session.request(
        request.method, url, data=body or None, headers=headers, allow_redirects=False
    ) as upstream:
        response = web.StreamResponse(
            status=upstream.status,
            headers={
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in HOP_HEADERS
            },
        )
        await response.prepare(request)
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
        await response.write_eof()
        return response


def applications(
    session: ClientSession, scheduler: Scheduler, *, enabled: bool = True
) -> tuple[web.Application, web.Application]:
    @web.middleware
    async def private_access(request: web.Request, handler: Any) -> web.StreamResponse:
        peer = request.remote
        hostname = urllib.parse.urlsplit("//" + request.host).hostname
        local_hosts = {
            "localhost",
            socket.gethostname().lower(),
            socket.getfqdn().lower(),
        }
        with contextlib.suppress(ValueError):
            if hostname and ipaddress.ip_address(hostname).is_loopback:
                local_hosts.add(hostname)
        if peer and ipaddress.ip_address(peer).is_loopback and hostname in local_hosts:
            return await handler(request)
        if os_tailnet_enabled():
            from .dsh import tailnet_identity

            try:
                identity = await asyncio.to_thread(tailnet_identity)
                hosts = {
                    identity["hostname"],
                    identity["hostname"].split(".", 1)[0],
                    *identity["addresses"],
                }
                if hostname in hosts and (
                    peer in identity["peers"]
                    or (peer and ipaddress.ip_address(peer).is_loopback)
                ):
                    return await handler(request)
            except (OSError, ValueError, ConfigError, subprocess.SubprocessError):
                pass
        raise web.HTTPForbidden(text="Use localhost or an owner's Tailscale device.")

    @web.middleware
    async def errors(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            return await handler(request)
        except (TimeoutError, ConfigError, ClientError, OSError) as error:
            return web.json_response({"error": str(error)}, status=503)

    async def chat(request: web.Request) -> web.StreamResponse:
        gpu = request.method == "POST" and request.path not in {"/unload", "/health"}
        if gpu and enabled:
            await scheduler.begin_chat()
        try:
            return await relay(request, session, scheduler.chat)
        finally:
            if gpu and enabled:
                scheduler.lock.release()

    async def image(request: web.Request) -> web.StreamResponse:
        path = request.path.removeprefix("/api")
        if path.startswith("/nixloom/"):
            raise web.HTTPNotFound()
        if request.method != "POST" or path != "/prompt":
            return await relay(request, session, scheduler.images)
        body = await request.read()
        await scheduler.begin_image()
        transferred = False
        try:
            async with session.post(
                scheduler.images + "/prompt",
                data=body,
                headers={
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() not in HOP_HEADERS
                },
            ) as response:
                payload = await response.read()
                if response.status == 200:
                    prompt_id = json.loads(payload).get("prompt_id")
                    if prompt_id:
                        scheduler.track(prompt_id)
                        transferred = True
                return web.Response(
                    body=payload,
                    status=response.status,
                    content_type="application/json",
                )
        finally:
            if not transferred:
                scheduler.lock.release()

    chat_app = web.Application(middlewares=[errors], client_max_size=100 * 1024 * 1024)
    image_app = web.Application(
        middlewares=[private_access, errors], client_max_size=100 * 1024 * 1024
    )
    chat_app.router.add_route("*", "/{path:.*}", chat)
    image_app.router.add_route("*", "/{path:.*}", image)
    return chat_app, image_app


async def serve(config: Config, paths: RuntimePaths) -> None:
    swap_command, document = runtime.swap_command(config, paths)
    paths.cache.mkdir(parents=True, exist_ok=True)
    (paths.cache / "llama-swap.yaml").write_text(document)
    children = [await asyncio.create_subprocess_exec(*swap_command)]
    runners: list[web.AppRunner] = []
    enabled = config.boolean("images.enabled")
    try:
        if enabled:
            comfy.prepare(config, paths)
            children.append(
                await asyncio.create_subprocess_exec(*comfy.command(config, paths))
            )
        chat_base = f"http://127.0.0.1:{config.integer('ports.swap', 8187, minimum=1)}"
        image_base = (
            f"http://127.0.0.1:{config.integer('ports.comfy_backend', 8189, minimum=1)}"
        )
        async with ClientSession(
            timeout=ClientTimeout(total=None, sock_connect=30), auto_decompress=True
        ) as session:
            scheduler = Scheduler(session, chat_base, image_base)
            if enabled:
                for attempt in range(120):
                    try:
                        schema = await scheduler.json(
                            "GET", image_base + "/object_info"
                        )
                        comfy.prepare_browser(config, paths, schema)
                        break
                    except ClientError:
                        if children[-1].returncode is not None or attempt == 119:
                            raise ConfigError(
                                "ComfyUI failed to start; see nixloom logs runtime"
                            )
                        await asyncio.sleep(1)
            apps = applications(session, scheduler, enabled=enabled)
            for app, port, host in (
                (apps[0], config.integer("ports.llama", minimum=1), "127.0.0.1"),
                (
                    apps[1],
                    config.integer("ports.comfyui", 8188, minimum=1),
                    "0.0.0.0" if os_tailnet_enabled() else "127.0.0.1",
                ),
            ):
                if app is apps[1] and not enabled:
                    continue
                runner = web.AppRunner(app)
                await runner.setup()
                runners.append(runner)
                await web.TCPSite(runner, host, port).start()
            stopped = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stopped.set)
            monitors = [asyncio.create_task(child.wait()) for child in children]
            stop = asyncio.create_task(stopped.wait())
            done, _ = await asyncio.wait(
                [stop, *monitors], return_when=asyncio.FIRST_COMPLETED
            )
            if stop not in done:
                raise ConfigError("a model backend exited; see nixloom logs runtime")
            for job in scheduler.jobs:
                job.cancel()
            await asyncio.gather(*scheduler.jobs, return_exceptions=True)
            for task in [stop, *monitors]:
                task.cancel()
    finally:
        for runner in runners:
            await runner.cleanup()
        for child in children:
            if child.returncode is None:
                child.terminate()
        for child in children:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(child.wait(), 10)
            if child.returncode is None:
                child.kill()
                await child.wait()


def os_tailnet_enabled() -> bool:
    import os

    return os.environ.get("NIXLOOM_DSH_TAILNET") == "1"


def run(config: Config, paths: RuntimePaths) -> None:
    asyncio.run(serve(config, paths))
