"""A synchronous GPU release acknowledgement for NixLoom's scheduler."""

import gc

import comfy.model_management
from aiohttp import web
from server import PromptServer


@PromptServer.instance.routes.post("/nixloom/unload")
async def unload(request):
    queue = PromptServer.instance.prompt_queue
    running, pending = queue.get_current_queue()
    if running or pending:
        return web.json_response({"error": "image tasks are still queued"}, status=409)
    gc.collect()
    comfy.model_management.unload_all_models()
    gc.collect()
    comfy.model_management.soft_empty_cache()
    return web.json_response({"unloaded": True})


NODE_CLASS_MAPPINGS = {}
