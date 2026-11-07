"""A synchronous GPU release acknowledgement for NixLoom's scheduler."""

import gc
import logging
from aiohttp import web
from server import PromptServer
import comfy.model_management

logger = logging.getLogger("nixloom.comfy")


def _thorough_gpu_cleanup() -> None:
    # 1. Unload all loaded models in ComfyUI
    try:
        comfy.model_management.unload_all_models()
    except Exception as e:
        logger.warning(f"[nixloom] unload_all_models error: {e}")

    # Ensure any remaining LoadedModel instances are unpatched and detached
    if hasattr(comfy.model_management, "current_loaded_models"):
        for m in list(comfy.model_management.current_loaded_models):
            try:
                m.model_unload()
            except Exception:
                pass
        comfy.model_management.current_loaded_models.clear()

    # 2. Reset cast buffers (clears STREAM_AIMDO_CAST_BUFFERS and STREAM_CAST_BUFFERS)
    if hasattr(comfy.model_management, "reset_cast_buffers"):
        try:
            comfy.model_management.reset_cast_buffers()
        except Exception as e:
            logger.warning(f"[nixloom] reset_cast_buffers error: {e}")

    # 3. Clean up prefetch queues and malloc graphs
    try:
        import comfy.model_prefetch as mp

        if hasattr(mp, "cleanup_prefetch_queues"):
            mp.cleanup_prefetch_queues()
        if hasattr(mp, "cleanup_prefetched_modules"):
            mp.cleanup_prefetched_modules()
        if hasattr(mp, "cleanup_malloc_graph"):
            mp.cleanup_malloc_graph()
    except Exception:
        pass

    # 4. Reset PromptExecutor caches (which hold node output tensors)
    try:
        import execution

        for obj in gc.get_objects():
            if isinstance(obj, execution.PromptExecutor):
                obj.reset()
    except Exception as e:
        logger.warning(f"[nixloom] PromptExecutor reset error: {e}")

    # 5. Garbage collect Python cycles
    gc.collect()
    gc.collect()

    # 6. Synchronize and empty PyTorch CUDA cache / IPC
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception as e:
        logger.warning(f"[nixloom] torch cuda empty_cache error: {e}")

    # 7. ComfyUI soft empty cache
    try:
        comfy.model_management.soft_empty_cache(force=True)
    except Exception:
        pass


@PromptServer.instance.routes.post("/nixloom/unload")
async def unload(request):
    queue = PromptServer.instance.prompt_queue
    running, pending = queue.get_current_queue()
    if running or pending:
        return web.json_response({"error": "image tasks are still queued"}, status=409)

    _thorough_gpu_cleanup()
    print("[nixloom-comfy] GPU memory thoroughly cleaned up", flush=True)
    return web.json_response({"unloaded": True})


NODE_CLASS_MAPPINGS = {}
