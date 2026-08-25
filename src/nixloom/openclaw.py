"""OpenClaw integration without owning OpenClaw's mutable user state."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, RuntimePaths

IMAGE_SETTINGS = (
    "models.providers.openai",
    "agents.defaults.imageGenerationModel",
    "browser.ssrfPolicy.dangerouslyAllowPrivateNetwork",
)
YUANBAO_SETTINGS = (
    "plugins.entries.openclaw-plugin-yuanbao.enabled",
    "channels.yuanbao.enabled",
)
YUANBAO_SECRET_SETTINGS = (
    "channels.yuanbao.appKey",
    "channels.yuanbao.appSecret",
)
TAVILY_SETTINGS = (
    "plugins.entries.tavily.enabled",
    "tools.web.search.provider",
)


def _setting(path: str, value: Any) -> dict[str, Any]:
    return {"path": path, "value": value}


def managed_settings(config: Config) -> list[dict[str, Any]]:
    model_id = config.string("llm.id")
    llama_port = config.integer("ports.llama", minimum=1)
    provider = {
        "baseUrl": f"http://127.0.0.1:{llama_port}/v1",
        "apiKey": "nixloom-local",
        "api": "openai-completions",
        "timeoutSeconds": 900,
        "models": [
            {
                "id": model_id,
                "name": model_id,
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": config.integer("llm.context", minimum=1),
                "maxTokens": config.integer("llm.max_tokens", minimum=1),
                "compat": {
                    "thinkingFormat": "qwen-chat-template",
                    "toolSchemaProfile": "llamacpp",
                },
            }
        ],
    }
    settings = [
        _setting("gateway.mode", "local"),
        _setting("gateway.port", config.integer("ports.openclaw", minimum=1)),
        _setting("gateway.bind", "loopback"),
        _setting("gateway.tailscale.mode", "serve"),
        _setting("gateway.controlUi.enabled", True),
        _setting("gateway.controlUi.allowedOrigins", ["*"]),
        _setting("gateway.controlUi.dangerouslyDisableDeviceAuth", True),
        _setting("gateway.auth.mode", "token"),
        _setting("models.mode", "merge"),
        _setting("models.providers.nixloom", provider),
        _setting("agents.defaults.model.primary", f"nixloom/{model_id}"),
        # Keep the Control UI picker and /model overrides limited to the
        # model actually served by nixloom.  OpenClaw's bundled provider
        # catalogs otherwise add unrelated models (notably OpenAI/Codex
        # entries) to the picker even when no such provider is configured.
        _setting("agents.defaults.models", {f"nixloom/{model_id}": {}}),
        _setting("tools.loopDetection.enabled", True),
        _setting("tools.web.fetch.ssrfPolicy.allowRfc2544BenchmarkRange", True),
    ]
    control_ui_root = os.environ.get("NIXLOOM_OPENCLAW_CONTROL_UI_ROOT", "").strip()
    if control_ui_root:
        settings.append(_setting("gateway.controlUi.root", control_ui_root))
    workspace = config.string("openclaw.workspace", "")
    if workspace:
        if not Path(workspace).is_absolute():
            raise ConfigError(
                f"openclaw.workspace must be an absolute path: {workspace}"
            )
        settings.append(_setting("agents.defaults.workspace", workspace))

    if config.boolean("images.enabled"):
        profile_name, profile = config.image_profile()
        image_model = Path(str(profile["model_file"])).stem or profile_name
        image_provider = {
            "baseUrl": f"http://127.0.0.1:{llama_port}/upstream/sd/v1",
            "apiKey": "nixloom-local",
            "api": "openai-completions",
            "models": [
                {
                    "id": image_model,
                    "name": image_model,
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 4096,
                    "maxTokens": 1024,
                }
            ],
        }
        settings.extend(
            [
                _setting("models.providers.openai", image_provider),
                _setting(
                    "agents.defaults.imageGenerationModel",
                    {"primary": f"openai/{image_model}", "timeoutMs": 600000},
                ),
                _setting("browser.ssrfPolicy.dangerouslyAllowPrivateNetwork", True),
            ]
        )
    return settings


def _set_batch(settings: list[dict[str, Any]]) -> None:
    config = _read_config()
    for setting in settings:
        _set_config_value(config, setting["path"], setting["value"])
    _write_config(config)


def _config_path() -> Path:
    value = os.environ.get("OPENCLAW_CONFIG_PATH", "")
    if not value:
        raise ConfigError("OPENCLAW_CONFIG_PATH is not set")
    return Path(value)


def _read_config() -> dict[str, Any]:
    path = _config_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read OpenClaw config {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError(f"OpenClaw config must contain a JSON object: {path}")
    return value


def _write_config(value: dict[str, Any]) -> None:
    path = _config_path()
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _set_config_value(config: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    target = config
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


def _get_config_value(config: dict[str, Any], path: str) -> Any:
    value: Any = config
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _unset_config_value(config: dict[str, Any], path: str) -> bool:
    parts = path.split(".")
    parents: list[tuple[dict[str, Any], str]] = []
    target = config
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            return False
        parents.append((target, part))
        target = child
    if parts[-1] not in target:
        return False
    del target[parts[-1]]
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break
    return True


def unmanaged_settings(config: Config) -> list[str]:
    """Return formerly managed paths that must converge to absence."""
    paths: list[str] = []
    if not config.string("openclaw.workspace", ""):
        paths.append("agents.defaults.workspace")
    if not config.boolean("images.enabled"):
        paths.extend(IMAGE_SETTINGS)
    return paths


def _unset_paths(paths: list[str] | tuple[str, ...]) -> None:
    config = _read_config()
    changed = False
    for path in paths:
        changed = _unset_config_value(config, path) or changed
    if changed:
        _write_config(config)


def _ensure_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists() or path.stat().st_size == 0:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".", dir=path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(secrets.token_hex(32))
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                pass
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    return path.read_text(encoding="utf-8").strip()


def _write_secret(path: Path, value: str) -> None:
    """Atomically materialize a runtime credential outside the Nix store."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _sync_control_ui(src: Path, root: Path, css: Path) -> None:
    """Mirror the packaged Control UI into a mutable state-dir copy.

    The gateway serves a custom ``controlUi.root`` with ``rejectHardlinks``
    enabled, and Nix store files are hard-linked (``nlink > 1``), which that
    check rejects.  A plain copy (``cp`` semantics) yields ``nlink == 1`` files
    that pass the check, and lets the CSS be edited in place for fast phone
    iteration without rebuilding the openclaw package.
    """
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(src, root)
    # Store files come out read-only (0444) and the store dirs read-execute
    # (0555); make the whole copy writable first so the CSS copy and the
    # index.html patch below can write.
    for dirpath, _dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o644)
    # The packaged dist carries no polish layer; inject the repo CSS and its
    # <link> into the served copy so the package never rebuilds for a style
    # change.
    shutil.copy2(css, root / "nixloom-control-ui.css")
    # copy2 preserves the store's 0444; the CSS is the file meant for editing.
    os.chmod(root / "nixloom-control-ui.css", 0o644)
    index = root / "index.html"
    html = index.read_text(encoding="utf-8")
    if "nixloom-control-ui.css" not in html:
        # Inject the polish stylesheet at the END of <head>, i.e. AFTER
        # OpenClaw's own assets/index-*.css link.  Same-specificity rules
        # cascade in document order, so the polish layer must load last or the
        # base mobile rules win and none of the overrides take effect.
        html = html.replace(
            "</head>",
            '<link rel="stylesheet" href="./nixloom-control-ui.css" />\n</head>',
        )
    # Mobile "+" entry point: a bottom sheet that re-triggers the existing
    # low-frequency composer actions (attach / voice / settings / context),
    # which are hidden from the compact two-row layout but never removed.
    # The sheet lives outside the Lit-rendered subtree and is wired via
    # document-level delegation, so re-renders cannot drop it.
    if "<!-- nixloom:composer-more -->" not in html:
        html = html.replace(
            "</body>",
            '<!-- nixloom:composer-more -->\n'
            '<script>\n'
            '  (function () {\n'
            '    if (window.innerWidth > 768) return;\n'
            '    var ITEMS = [\n'
            '      { label: "Attach file", icon: "\\uD83D\\uDCCE", sel: ".agent-chat__file-input" },\n'
            '      { label: "Voice", icon: "\\uD83C\\uDF99", sel: ".agent-chat__toolbar-left .agent-chat__input-btn:nth-of-type(2)" },\n'
            '      { label: "Session settings", icon: "\\u2699\\uFE0F", sel: ".chat-settings-chip" },\n'
            '      { label: "Context", icon: "\\u25CE", sel: ".chat-controls__quota" }\n'
            '    ];\n'
            '    var SHEET = null, BACKDROP = null;\n'
            '    function trigger(item) { var el = document.querySelector(item.sel); if (el) el.click(); }\n'
            '    function close() {\n'
            '      if (SHEET) SHEET.style.transform = "translateY(110%)";\n'
            '      if (BACKDROP) BACKDROP.style.opacity = "0";\n'
            '      setTimeout(function () { if (BACKDROP) BACKDROP.style.display = "none"; }, 200);\n'
            '    }\n'
            '    function ensure() {\n'
            '      if (SHEET) return;\n'
            '      BACKDROP = document.createElement("div");\n'
            '      BACKDROP.style.cssText = "position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,.45);opacity:0;transition:opacity .18s ease;display:none;";\n'
            '      BACKDROP.addEventListener("click", close);\n'
            '      document.body.appendChild(BACKDROP);\n'
            '      SHEET = document.createElement("div");\n'
            '      SHEET.id = "nixloom-composer-more";\n'
            '      SHEET.setAttribute("role", "menu");\n'
            '      SHEET.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:9999;box-sizing:border-box;background:var(--card,#161b22);border-top:1px solid var(--border,#2a3038);border-radius:16px 16px 0 0;padding:6px 0 max(10px,env(safe-area-inset-bottom));transform:translateY(110%);transition:transform .18s ease;box-shadow:0 -8px 40px rgba(0,0,0,.35);";\n'
            '      ITEMS.forEach(function (item) {\n'
            '        var b = document.createElement("button");\n'
            '        b.type = "button";\n'
            '        b.textContent = item.icon + "  " + item.label;\n'
            '        b.style.cssText = "display:flex;width:100%;align-items:center;gap:10px;padding:13px 20px;font:inherit;font-size:15px;color:var(--text);background:none;border:none;text-align:left;cursor:pointer;";\n'
            '        b.addEventListener("click", function () { close(); trigger(item); });\n'
            '        SHEET.appendChild(b);\n'
            '      });\n'
            '      document.body.appendChild(SHEET);\n'
            '    }\n'
            '    function open() {\n'
            '      ensure();\n'
            '      if (BACKDROP) { BACKDROP.style.display = "block"; requestAnimationFrame(function () { BACKDROP.style.opacity = "1"; }); }\n'
            '      if (SHEET) requestAnimationFrame(function () { SHEET.style.transform = "translateY(0)"; });\n'
            '    }\n'
            '    document.addEventListener("click", function (e) {\n'
            '      var t = e.target.closest ? e.target.closest(".agent-chat__toolbar-left .agent-chat__input-btn") : null;\n'
            '      if (t) { e.preventDefault(); e.stopPropagation(); open(); }\n'
            '    }, true);\n'
            '  })();\n'
            '</script>\n'
            '</body>',
        )
    # Mobile metadata: collapse message timestamps to a bare time for today's
    # messages (the full date stays in the <time> title).  Pure CSS cannot
    # re-format text content, so this tiny hook re-writes the label on load
    # and after streaming mutations.
    if "<!-- nixloom:timestamp-hook -->" not in html:
        html = html.replace(
            "</body>",
            '<!-- nixloom:timestamp-hook -->\n'
            '<script>\n'
            '  (function () {\n'
            '    var today = new Date().toDateString();\n'
            '    function pad(n) { return n < 10 ? "0" + n : "" + n; }\n'
            '    function fmt(ts) {\n'
            '      var d = new Date(ts);\n'
            '      if (isNaN(d.getTime())) return "";\n'
            '      var t = pad(d.getHours()) + ":" + pad(d.getMinutes());\n'
            '      return d.toDateString() === today\n'
            '        ? t\n'
            '        : (d.getMonth() + 1) + "/" + d.getDate() + " " + t;\n'
            '    }\n'
            '    function update() {\n'
            '      var nodes = document.querySelectorAll(".chat-group-timestamp");\n'
            '      for (var i = 0; i < nodes.length; i++) {\n'
            '        var ts = nodes[i].getAttribute("datetime");\n'
            '        if (ts) { var v = fmt(ts); if (v) nodes[i].textContent = v; }\n'
            '      }\n'
            '    }\n'
            '    update();\n'
            '    if (window.MutationObserver) {\n'
            '      var timer = null;\n'
            '      new MutationObserver(function () {\n'
            '        clearTimeout(timer); timer = setTimeout(update, 100);\n'
            '      }).observe(document.body, { childList: true, subtree: true });\n'
            '    }\n'
            '  })();\n'
            '</script>\n'
            '</body>',
        )
    index.write_text(html, encoding="utf-8")


def _sync_plugin_path(plugin_path: Path | None, suffix: str) -> None:
    paths = _get_config_value(_read_config(), "plugins.load.paths")
    if not isinstance(paths, list):
        paths = []
    paths = [
        item for item in paths if isinstance(item, str) and not item.endswith(suffix)
    ]
    if plugin_path:
        paths.append(str(plugin_path))
    paths = sorted(set(paths))
    if paths:
        _set_batch([_setting("plugins.load.paths", paths)])
    else:
        _unset_paths(["plugins.load.paths"])


def _configure_yuanbao(config: Config) -> None:
    plugin_path_value = os.environ.get("NIXLOOM_OPENCLAW_PLUGIN_PATH", "")
    plugin_path = Path(plugin_path_value) if plugin_path_value else None
    if not plugin_path or not (plugin_path / "openclaw.plugin.json").is_file():
        raise ConfigError(
            f"the packaged Yuanbao plugin is unavailable: {plugin_path_value or '<unset>'}"
        )
    _sync_plugin_path(plugin_path, "/share/nixloom/openclaw-plugin-yuanbao")
    _set_batch(
        [
            _setting("plugins.entries.openclaw-plugin-yuanbao.enabled", True),
            _setting("channels.yuanbao.enabled", True),
        ]
    )
    _unset_paths(YUANBAO_SECRET_SETTINGS)


def _disable_yuanbao() -> None:
    _sync_plugin_path(None, "/share/nixloom/openclaw-plugin-yuanbao")
    _unset_paths((*YUANBAO_SETTINGS, *YUANBAO_SECRET_SETTINGS))


def _configure_tavily(config: Config) -> None:
    plugin_path_value = os.environ.get("NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH", "")
    plugin_path = Path(plugin_path_value) if plugin_path_value else None
    api_key = config.string("credentials.tavily_api_key", "")
    if not api_key:
        _sync_plugin_path(None, "/share/nixloom/openclaw-plugin-tavily")
        _unset_paths(TAVILY_SETTINGS)
        return
    if not plugin_path or not (plugin_path / "openclaw.plugin.json").is_file():
        raise ConfigError(
            f"the packaged Tavily plugin is unavailable: {plugin_path_value or '<unset>'}"
        )
    _sync_plugin_path(plugin_path, "/share/nixloom/openclaw-plugin-tavily")
    _set_batch(
        [
            _setting("plugins.entries.tavily.enabled", True),
            _setting("tools.web.search.provider", "tavily"),
        ]
    )


def reconcile_settings(config: Config) -> None:
    """Apply the desired settings and remove managed values no longer desired."""
    _set_batch(managed_settings(config))
    _unset_paths(unmanaged_settings(config))
    if config.boolean("openclaw.yuanbao"):
        _configure_yuanbao(config)
    else:
        _disable_yuanbao()
    _configure_tavily(config)


def prepare(config: Config, paths: RuntimePaths) -> None:
    """Render OpenClaw config and credentials during Home Manager activation."""
    state = paths.state / ".openclaw"
    config_path = state / "openclaw.json"
    os.umask(0o077)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path.touch(mode=0o600, exist_ok=True)
    if config_path.stat().st_size == 0:
        config_path.write_text("{}\n", encoding="utf-8")
        config_path.chmod(0o600)
    os.environ["OPENCLAW_STATE_DIR"] = str(state)
    os.environ["OPENCLAW_CONFIG_PATH"] = str(config_path)
    runtime_dir = paths.state / ".run"
    _ensure_token(runtime_dir / "openclaw-gateway-token")
    _write_secret(
        runtime_dir / "openclaw-tavily-api-key",
        config.string("credentials.tavily_api_key", ""),
    )
    _write_secret(
        runtime_dir / "openclaw-yuanbao-app-key",
        config.string("credentials.yuanbao_app_key", ""),
    )
    _write_secret(
        runtime_dir / "openclaw-yuanbao-app-secret",
        config.string("credentials.yuanbao_app_secret", ""),
    )
    control_ui_src = os.environ.get("NIXLOOM_OPENCLAW_CONTROL_UI_SRC", "").strip()
    control_ui_root = os.environ.get("NIXLOOM_OPENCLAW_CONTROL_UI_ROOT", "").strip()
    control_ui_css = os.environ.get("NIXLOOM_OPENCLAW_CONTROL_UI_CSS", "").strip()
    if control_ui_src and control_ui_root and control_ui_css:
        _sync_control_ui(
            Path(control_ui_src), Path(control_ui_root), Path(control_ui_css)
        )
    reconcile_settings(config)


def run(config: Config, paths: RuntimePaths, *, dry_run: bool = False) -> None:
    state = paths.state / ".openclaw"
    config_path = state / "openclaw.json"
    settings = managed_settings(config)
    if dry_run:
        print(f"OPENCLAW_STATE_DIR={state}")
        print(f"OPENCLAW_CONFIG_PATH={config_path}")
        print("OPENCLAW_GATEWAY_TOKEN=<generated>")
        print("MANAGED_CONFIG=" + json.dumps(settings, ensure_ascii=False))
        print(
            "UNSET_CONFIG=" + json.dumps(unmanaged_settings(config), ensure_ascii=False)
        )
        if not config.boolean("openclaw.yuanbao"):
            print("DISABLE_YUANBAO=true")
        print("openclaw gateway")
        return

    prepare(config, paths)
    runtime_dir = paths.state / ".run"
    os.environ["OPENCLAW_GATEWAY_TOKEN"] = _ensure_token(
        runtime_dir / "openclaw-gateway-token"
    )
    os.environ["TAVILY_API_KEY"] = (runtime_dir / "openclaw-tavily-api-key").read_text(
        encoding="utf-8"
    )
    os.environ["YUANBAO_APP_KEY"] = (runtime_dir / "openclaw-yuanbao-app-key").read_text(
        encoding="utf-8"
    )
    os.environ["YUANBAO_APP_SECRET"] = (
        runtime_dir / "openclaw-yuanbao-app-secret"
    ).read_text(encoding="utf-8")
    # Packaged plugins live in the immutable Nix store and may contain
    # hard-linked files. OpenClaw only permits that layout in its Nix mode;
    # without this flag plugin discovery silently skips Yuanbao and Tavily.
    os.environ["OPENCLAW_NIX_MODE"] = "1"
    port = config.integer("ports.openclaw", minimum=1)
    print(f"Starting OpenClaw on http://127.0.0.1:{port}", file=sys.stderr)
    os.execvp("openclaw", ["openclaw", "gateway"])
