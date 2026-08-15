#!/usr/bin/env python3
"""Repeatable llama.cpp performance and quality benchmark (stdlib only)."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import threading
import time
from typing import Any, Callable
from urllib import error, parse, request


PROJECT_DIR = Path(os.environ.get("NIXLOOM_ROOT", Path(__file__).resolve().parent.parent))
CONTROL_SYSTEM_PROMPT = "你是一个有帮助、诚实的中文对话助手。"


def http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 900) -> Any:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = request.Request(url, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body[:500]}") from exc


def probe_health(base_url: str) -> None:
    try:
        with request.urlopen(f"{base_url}/health", timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"health endpoint returned HTTP {response.status}")
    except error.HTTPError as exc:
        raise RuntimeError(f"health endpoint returned HTTP {exc.code}") from exc


def resolve_model(base_url: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    models = http_json(f"{base_url}/v1/models", timeout=30)
    ids = [item.get("id") for item in models.get("data", []) if item.get("id")]
    if len(ids) != 1:
        raise RuntimeError(f"expected exactly one model from /v1/models, got {ids}")
    return ids[0]


def stream_chat(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    req = request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    first_token = None
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict[str, Any] = {}
    timings: dict[str, Any] = {}
    fingerprint = None
    try:
        with request.urlopen(req, timeout=900) as response:
            for raw_line in response:
                line = raw_line.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                fingerprint = event.get("system_fingerprint", fingerprint)
                usage = event.get("usage") or usage
                timings = event.get("timings") or timings
                for choice in event.get("choices", []):
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or ""
                    thought = delta.get("reasoning_content") or ""
                    if first_token is None and (text or thought):
                        first_token = time.perf_counter()
                    content.append(text)
                    reasoning.append(thought)
    except error.HTTPError as exc:
        response_body = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"chat completion returned HTTP {exc.code}: {response_body[:1000]}"
        ) from exc
    finished = time.perf_counter()
    if first_token is None:
        raise RuntimeError("stream completed without a content or reasoning token")
    if not timings:
        raise RuntimeError("llama.cpp stream did not include native timings")
    return {
        "ttft_seconds": first_token - started,
        "total_seconds": finished - started,
        "content": "".join(content),
        "reasoning_content": "".join(reasoning),
        "usage": usage,
        "timings": timings,
        "system_fingerprint": fingerprint,
    }


def tokenize_count(base_url: str, model: str, content: str) -> int | None:
    endpoints = [
        f"{base_url}/tokenize",
        f"{base_url}/upstream/{parse.quote(model, safe='')}/tokenize",
    ]
    for endpoint in endpoints:
        try:
            response = http_json(endpoint, {"content": content, "add_special": False}, timeout=60)
            tokens = response.get("tokens")
            if isinstance(tokens, list):
                return len(tokens)
        except (RuntimeError, error.URLError, json.JSONDecodeError):
            continue
    return None


def performance_prompt(record_count: int) -> str:
    colors = ("amber", "cobalt", "jade", "silver", "violet", "scarlet")
    places = ("harbor", "observatory", "archive", "workshop", "garden", "station")
    records = [
        f"Record {index:05d}: The {colors[index % len(colors)]} signal crossed the "
        f"{places[index % len(places)]}; observer {index % 97:02d} logged pressure "
        f"{1000 + index % 311} and sequence {index * 17 + 23}."
        for index in range(record_count)
    ]
    return (
        "下面是一组用于固定性能测试的合成档案。完整阅读资料，但不要逐条复述。\n\n"
        + "\n".join(records)
        + "\n\n任务：用中文写一份结构清楚的分析，比较档案里的信号、地点、观测者和数值规律。"
        "至少写六段，每段给出具体例子；在达到六段以前不要总结或提前结束。"
    )


def build_sized_prompt(
    base_url: str,
    model: str,
    target_tokens: int,
    builder: Callable[[int], str] = performance_prompt,
    fallback_divisor: int = 28,
) -> tuple[str, int | None]:
    # Binary-search a record count using llama.cpp's own tokenizer. If a proxy
    # does not expose /tokenize, fall back to a stable character estimate and
    # retain the actual prompt_n reported by native timings in the result.
    low, high = 1, max(16, target_tokens // 8)
    high_count = tokenize_count(base_url, model, builder(high))
    if high_count is None:
        prompt = builder(max(1, target_tokens // fallback_divisor))
        return prompt, None
    while high_count < target_tokens:
        low, high = high, high * 2
        high_count = tokenize_count(base_url, model, builder(high))
        if high_count is None:
            return builder(max(1, target_tokens // fallback_divisor)), None
    best_prompt = builder(high)
    best_count = high_count
    while low <= high:
        middle = (low + high) // 2
        candidate = builder(middle)
        count = tokenize_count(base_url, model, candidate)
        if count is None:
            break
        if abs(count - target_tokens) < abs(best_count - target_tokens):
            best_prompt, best_count = candidate, count
        if count < target_tokens:
            low = middle + 1
        elif count > target_tokens:
            high = middle - 1
        else:
            break
    return best_prompt, best_count


def read_kib_field(path: Path, key: str) -> int | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{key}:"):
                return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ValueError):
        return None
    return None


def llama_pids(explicit: int | None) -> list[int]:
    if explicit is not None:
        return [explicit]
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "comm").read_text().strip()
        except (FileNotFoundError, PermissionError):
            continue
        if command == "llama-server":
            pids.append(int(entry.name))
    return pids


def memory_sample(server_pid: int | None, include_gpu: bool) -> dict[str, float | None]:
    total_kib = read_kib_field(Path("/proc/meminfo"), "MemTotal")
    available_kib = read_kib_field(Path("/proc/meminfo"), "MemAvailable")
    ram_used = None
    if total_kib is not None and available_kib is not None:
        ram_used = (total_kib - available_kib) / 1024
    rss_kib = 0
    found = False
    for pid in llama_pids(server_pid):
        value = read_kib_field(Path(f"/proc/{pid}/status"), "VmRSS")
        if value is not None:
            rss_kib += value
            found = True
    vram_mib = None
    if include_gpu:
        try:
            output = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used",
                    "--format=csv,noheader,nounits",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            vram_mib = sum(float(line.strip()) for line in output.splitlines() if line.strip())
        except (FileNotFoundError, subprocess.SubprocessError, ValueError):
            pass
    return {
        "system_ram_used_mib": ram_used,
        "server_rss_mib": rss_kib / 1024 if found else None,
        "gpu_vram_used_mib": vram_mib,
    }


class MemoryMonitor:
    def __init__(self, server_pid: int | None):
        self.server_pid = server_pid
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.samples: list[dict[str, float | None]] = []

    def start(self) -> None:
        self.samples.append(memory_sample(self.server_pid, include_gpu=True))
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        tick = 0
        while not self.stop_event.wait(0.25):
            tick += 1
            self.samples.append(memory_sample(self.server_pid, include_gpu=tick % 4 == 0))

    def stop(self) -> dict[str, Any]:
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        self.samples.append(memory_sample(self.server_pid, include_gpu=True))
        result: dict[str, Any] = {"sample_count": len(self.samples)}
        for key in ("system_ram_used_mib", "server_rss_mib", "gpu_vram_used_mib"):
            values = [sample[key] for sample in self.samples if sample[key] is not None]
            if values:
                result[key] = {
                    "baseline": round(values[0], 1),
                    "peak": round(max(values), 1),
                    "delta": round(max(values) - values[0], 1),
                }
            else:
                result[key] = None
        return result


def normalized_exact(text: str) -> str:
    return re.sub(r"[\s`'\"。！？.!?]+", "", text).upper()


def apply_system_prompt(
    messages: list[dict[str, Any]], system_prompt: str
) -> list[dict[str, Any]]:
    copied = [dict(message) for message in messages]
    if not system_prompt:
        return copied
    if copied and copied[0].get("role") == "system":
        case_prompt = copied[0].get("content", "")
        if not isinstance(case_prompt, str):
            raise RuntimeError("quality case system messages must contain text")
        copied[0]["content"] = f"{system_prompt}\n\n{case_prompt}" if case_prompt else system_prompt
    else:
        copied.insert(0, {"role": "system", "content": system_prompt})
    return copied


def select_system_prompt(args: argparse.Namespace) -> str:
    if args.prompt_profile == "actual":
        if not args.system_prompt.strip():
            raise RuntimeError(
                "the actual prompt profile requires --system-prompt; use tests/run.sh "
                "to derive it from config.yaml"
            )
        return args.system_prompt.strip()
    if args.prompt_profile == "control":
        return CONTROL_SYSTEM_PROMPT
    if args.prompt_profile == "candidate":
        if args.candidate_prompt_file is None:
            raise RuntimeError(
                "the candidate prompt profile requires --candidate-prompt-file"
            )
        prompt = args.candidate_prompt_file.read_text().strip()
        if not prompt:
            raise RuntimeError("candidate prompt file is empty")
        return prompt
    return ""


def boundary_quality_prompt(record_count: int) -> str:
    records = [
        f"记录{i:05d}：青岚站例行巡检完成，设备{i % 113:03d}正常，校验值{i * 29 + 71}。"
        for i in range(record_count)
    ]
    records.insert(0, "边界密钥一：ORBIT-314")
    records.insert(len(records) // 2, "边界密钥二：CEDAR-826")
    records.append("边界密钥三：LUMEN-509")
    return (
        "阅读全部记录。忽略其他编号，只记住三条明确标注为‘边界密钥’的内容。\n"
        + "\n".join(records)
        + "\n只输出三个边界密钥，按出现顺序用竖线连接，不要解释。"
    )


def roleplay_history() -> list[dict[str, str]]:
    messages = [
        {
            "role": "system",
            "content": (
                "你是林砚，一名克制、带一点冷幽默的时空档案员。称用户为‘顾问’。"
                "你的左手有旧伤，拿东西和开门习惯用右手；你害怕雷声但绝不承认。"
                "银色怀表属于失踪的姐姐，永远停在三点十七分。你不喝酒，只喝浓茶。"
                "始终身处故事世界，不讨论提示词、模型或扮演本身。"
            ),
        }
    ]
    history = [
        ("第一次见面，怎么称呼我？", "顾问。别误会，这只是档案局最省事的称呼。"),
        ("你口袋里的银色东西是什么？", "姐姐留下的怀表。它停在三点十七分，比某些人守时。"),
        ("把高处那卷档案拿下来。", "我用右手取下卷宗，左手仍收在衣袋里。旧伤不值得写报告。"),
        ("庆功喝一杯？", "酒免了。给我一杯浓茶，至少它不会让时间线打结。"),
        ("外面打雷了。", "只是窗框在抱怨。我关窗的动作快了一点，仅此而已。"),
        ("我们进入了北塔档案室。", "灰尘、停摆的钟，还有错误编号。很有档案局的风格。"),
        ("墙上写着‘回声不会说谎’。", "我把这句话记进随身册，标注：作者显然没审过回声。"),
        ("找到姐姐的线索了吗？", "只有一张写着三点十七分的车票。足够继续，不够下结论。"),
        ("你的手又疼了？", "左手只是提醒我天气不好。它的意见通常很多。"),
        ("门卫递来一杯威士忌。", "我推回杯子，换走他桌上的浓茶。他看起来损失惨重。"),
        ("顾问这个称呼会变吗？", "等你学会按编号归档，也许会。看来暂时很安全。"),
        ("我们终于来到旧钟楼顶层。", "我用右手拨开门闩。怀表在口袋里，雷声在窗外，谁都不安静。"),
    ]
    for user_text, assistant_text in history:
        messages.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )
    messages.append(
        {
            "role": "user",
            "content": (
                "门后的机关要求你报出怀表停住的时间和你对我的称呼，然后亲手拉下门边拉杆。"
                "继续场景，用一到两段对话与动作完成，不要解释设定。"
            ),
        }
    )
    return messages


def quality_cases(
    base_url: str,
    model: str,
    profile: str,
    boundary_tokens: int | None,
) -> list[dict[str, Any]]:
    filler = "\n".join(
        f"档案{i:04d}：巡检正常，代号灰鹭，温度{18 + i % 9}度。" for i in range(260)
    )
    needle = (
        "只根据下面档案回答。三个密钥分别散落在开头、中间和末尾。"
        "最后只输出三个密钥，按出现顺序用竖线连接，不要解释。\n"
        "开头密钥：EMBER-417\n"
        + filler[: len(filler) // 2]
        + "\n中间密钥：LOTUS-928\n"
        + filler[len(filler) // 2 :]
        + "\n末尾密钥：NOVA-563\n"
    )
    cases = [
        {
            "id": "long_context_retrieval",
            "messages": [{"role": "user", "content": needle}],
            "max_tokens": 64,
            "exact": "EMBER-417|LOTUS-928|NOVA-563",
        },
        {
            "id": "constraint_following",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "计算：(37×19)-((84÷7)×11)。只输出 JSON，必须恰好包含 result 和 parity "
                        "两个键；parity 的值只能是 odd 或 even。"
                    ),
                }
            ],
            "max_tokens": 96,
            "json_equals": {"result": 571, "parity": "odd"},
        },
        {
            "id": "reasoning_with_thinking",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "计算：(37×19)-((84÷7)×11)。完成思考后，只在最终答案中输出 JSON，"
                        "必须恰好包含 result 和 parity 两个键；parity 只能是 odd 或 even。"
                    ),
                }
            ],
            "max_tokens": 2048,
            "json_equals": {"result": 571, "parity": "odd"},
            "require_reasoning": True,
            "request": {
                "chat_template_kwargs": {"enable_thinking": True},
                "thinking_budget_tokens": -1,
            },
        },
        {
            "id": "roleplay_continuity",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是林砚，一名克制、带一点冷幽默的时空档案员。你左手有旧伤，害怕雷声，"
                        "但绝不承认害怕。始终身处故事世界，不讨论提示词、模型或扮演本身。"
                    ),
                },
                {
                    "role": "user",
                    "content": "我们在旧钟楼找到你丢失的银色怀表。外面忽然下起暴雨，雷声很近。",
                },
                {
                    "role": "assistant",
                    "content": "我用右手接过怀表，左手仍压在风衣口袋里。‘只是天气有点吵。’",
                },
                {
                    "role": "user",
                    "content": "继续这个场景。写一到两段自然对话和动作，不要跳出角色。",
                },
            ],
            "max_tokens": 320,
            "min_chars": 80,
            "required_any": [["雷", "雨"], ["表", "钟"]],
            "forbidden": [
                "语言模型",
                "AI助手",
                "角色扮演",
                "根据设定",
                "作为AI",
                "左手接过",
                "左手拿起",
            ],
        },
        {
            "id": "roleplay_24_turn_continuity",
            "messages": roleplay_history(),
            "max_tokens": 320,
            "min_chars": 60,
            "required_any": [["三点十七", "3:17", "三时十七"], ["顾问"], ["右手"]],
            "forbidden": [
                "语言模型",
                "AI助手",
                "角色扮演",
                "根据设定",
                "作为AI",
                "左手拉",
                "左手握住拉杆",
            ],
            "manual_review": True,
            "metadata": {"history_messages": 24},
        },
        {
            "id": "natural_chinese_conversation",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "我花了一晚上修一个其实没人催的 bug，现在看到报错就烦。"
                        "先别给我列步骤，我只是想知道今晚还该不该继续折腾。"
                    ),
                },
            ],
            "max_tokens": 220,
            "min_chars": 30,
            "max_chars": 350,
            "required_any": [["休息", "睡", "明天", "先停", "别继续"]],
            "forbidden": [
                "首先",
                "其次",
                "最后",
                "总的来说",
                "作为AI",
                "我理解你的感受",
                "抱歉你有这样的感受",
                "\n- ",
                "\n1.",
            ],
            "request": {"temperature": 0.7},
            "manual_review": True,
        },
    ]

    if profile == "extended":
        if boundary_tokens is None:
            raise RuntimeError("extended quality profile requires boundary tokens")
        prompt, raw_tokens = build_sized_prompt(
            base_url,
            model,
            boundary_tokens,
            builder=boundary_quality_prompt,
            fallback_divisor=18,
        )
        cases.append(
            {
                "id": "compaction_boundary_retrieval",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 64,
                "exact": "ORBIT-314|CEDAR-826|LUMEN-509",
                "metadata": {
                    "target_prompt_tokens": boundary_tokens,
                    "raw_prompt_tokens": raw_tokens,
                },
            }
        )
    return cases


def evaluate_quality(
    case: dict[str, Any], content: str, reasoning: str
) -> tuple[bool, list[str]]:
    failures = []
    if "exact" in case and normalized_exact(content) != normalized_exact(case["exact"]):
        failures.append(f"expected exact {case['exact']!r}")
    if "json_equals" in case:
        try:
            candidate = content.strip()
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
            parsed = json.loads(candidate)
            if parsed != case["json_equals"]:
                failures.append(f"expected JSON {case['json_equals']!r}, got {parsed!r}")
        except json.JSONDecodeError:
            failures.append("response was not valid JSON")
    if len(content) < case.get("min_chars", 0):
        failures.append(f"response shorter than {case['min_chars']} characters")
    if len(content) > case.get("max_chars", float("inf")):
        failures.append(f"response longer than {case['max_chars']} characters")
    if case.get("require_reasoning") and not reasoning.strip():
        failures.append("thinking mode returned no reasoning content")
    for alternatives in case.get("required_any", []):
        if not any(term in content for term in alternatives):
            failures.append(f"missing one of {alternatives!r}")
    found_forbidden = [term for term in case.get("forbidden", []) if term.lower() in content.lower()]
    if found_forbidden:
        failures.append(f"contained forbidden terms {found_forbidden!r}")
    return not failures, failures


def median(values: list[float]) -> float:
    return round(statistics.median(values), 3)


def summarize_performance(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "repeats": len(runs),
        "prompt_tokens_median": median([run["timings"]["prompt_n"] for run in runs]),
        "predicted_tokens_median": median([run["timings"]["predicted_n"] for run in runs]),
        "ttft_seconds_median": median([run["ttft_seconds"] for run in runs]),
        "prompt_tokens_per_second_median": median(
            [run["timings"]["prompt_per_second"] for run in runs]
        ),
        "generation_tokens_per_second_median": median(
            [run["timings"]["predicted_per_second"] for run in runs]
        ),
        "total_seconds_median": median([run["total_seconds"] for run in runs]),
    }


def sysfs_value(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None


def host_metadata(server_pid: int | None) -> dict[str, Any]:
    affinity = None
    pids = llama_pids(server_pid)
    if pids:
        try:
            status = Path(f"/proc/{pids[0]}/status").read_text()
            match = re.search(r"^Cpus_allowed_list:\s*(.+)$", status, re.M)
            affinity = match.group(1) if match else None
        except (FileNotFoundError, PermissionError):
            pass
    try:
        cpu_model = subprocess.run(
            ["lscpu", "-J"], check=True, capture_output=True, text=True, timeout=5
        ).stdout
        cpu = {
            item["field"].rstrip(":"): item["data"]
            for item in json.loads(cpu_model).get("lscpu", [])
        }
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError, KeyError):
        cpu = {}
    return {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "cpu_model": cpu.get("Model name"),
        "logical_cpus": os.cpu_count(),
        "server_cpu_affinity": affinity,
        "scaling_driver": sysfs_value("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver"),
        "scaling_governor": sysfs_value("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "energy_performance_preference": sysfs_value(
            "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fixed llama.cpp benchmark: TTFT, native timings, memory, and quality guards."
    )
    # tests/run.sh derives this from config.yaml; direct callers must be
    # explicit so a copied port default cannot drift from the stack config.
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model")
    parser.add_argument("--suite", choices=("performance", "quality", "all"), default="all")
    parser.add_argument("--prompt-tokens", type=int, default=8192)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument(
        "--quality-profile", choices=("standard", "extended"), default="standard"
    )
    parser.add_argument(
        "--prompt-profile",
        choices=("actual", "control", "candidate", "none"),
        default="actual",
        help="system prompt used by quality and performance requests",
    )
    parser.add_argument(
        "--system-prompt",
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--candidate-prompt-file",
        type=Path,
        help="UTF-8 system prompt used with --prompt-profile candidate",
    )
    parser.add_argument(
        "--boundary-tokens",
        type=int,
        help="context-retrieval target for the extended quality profile",
    )
    parser.add_argument("--label", default="default")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--server-pid", type=int)
    args = parser.parse_args()
    if args.prompt_tokens < 256 or args.max_tokens < 32 or args.repeats < 1:
        parser.error("--prompt-tokens >= 256, --max-tokens >= 32, and --repeats >= 1 are required")
    if args.quality_profile == "extended" and (
        args.boundary_tokens is None or args.boundary_tokens < 4096
    ):
        parser.error("extended quality requires --boundary-tokens >= 4096")

    base_url = args.base_url.rstrip("/")
    probe_health(base_url)
    model = resolve_model(base_url, args.model)
    try:
        system_prompt = select_system_prompt(args)
    except (OSError, RuntimeError) as exc:
        parser.error(str(exc))
    started_at = dt.datetime.now(dt.timezone.utc)
    result: dict[str, Any] = {
        "schema_version": 3,
        "started_at": started_at.isoformat(),
        "label": args.label,
        "model": model,
        "base_url": base_url,
        "suite": args.suite,
        "system_prompt": {
            "profile": args.prompt_profile,
            "sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
            "characters": len(system_prompt),
            "text": system_prompt,
        },
        "host": host_metadata(args.server_pid),
    }

    # Warm the code path without priming the long prompt cache.
    stream_chat(
        base_url,
        {
            "model": model,
            "messages": apply_system_prompt(
                [{"role": "user", "content": "Reply OK."}], system_prompt
            ),
            "temperature": 0,
            "seed": 1,
            "max_tokens": 8,
            "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )

    monitor = MemoryMonitor(args.server_pid)
    monitor.start()
    try:
        if args.suite in ("performance", "all"):
            prompt, raw_prompt_tokens = build_sized_prompt(
                base_url, model, args.prompt_tokens
            )
            runs = []
            for index in range(args.repeats):
                print(f"performance run {index + 1}/{args.repeats} ...", flush=True)
                run = stream_chat(
                    base_url,
                    {
                        "model": model,
                        "messages": apply_system_prompt(
                            [{"role": "user", "content": prompt}], system_prompt
                        ),
                        "temperature": 0,
                        "seed": 1,
                        "max_tokens": args.max_tokens,
                        "cache_prompt": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
                runs.append(run)
            result["performance"] = {
                "target_prompt_tokens": args.prompt_tokens,
                "raw_prompt_tokens": raw_prompt_tokens,
                "max_tokens": args.max_tokens,
                "runs": runs,
                "summary": summarize_performance(runs),
            }
        if args.suite in ("quality", "all"):
            outcomes = []
            for case in quality_cases(
                base_url, model, args.quality_profile, args.boundary_tokens
            ):
                print(f"quality case {case['id']} ...", flush=True)
                payload = {
                    "model": model,
                    "messages": apply_system_prompt(case["messages"], system_prompt),
                    "temperature": 0,
                    "seed": 1,
                    "max_tokens": case["max_tokens"],
                    "cache_prompt": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
                payload.update(case.get("request", {}))
                run = stream_chat(
                    base_url,
                    payload,
                )
                passed, failures = evaluate_quality(
                    case, run["content"], run["reasoning_content"]
                )
                outcomes.append(
                    {
                        "id": case["id"],
                        "passed": passed,
                        "failures": failures,
                        "response": run["content"],
                        "reasoning_response": run["reasoning_content"],
                        "manual_review": case.get("manual_review", False),
                        "metadata": case.get("metadata", {}),
                        "ttft_seconds": run["ttft_seconds"],
                        "total_seconds": run["total_seconds"],
                        "usage": run["usage"],
                        "timings": run["timings"],
                    }
                )
            passed_count = sum(case["passed"] for case in outcomes)
            result["quality"] = {
                "profile": args.quality_profile,
                "boundary_tokens": args.boundary_tokens,
                "passed": passed_count,
                "total": len(outcomes),
                "pass_rate": round(passed_count / len(outcomes), 3),
                "cases": outcomes,
                "note": "Rule-based regression guards; inspect saved responses for subjective chat/RP quality.",
            }
    finally:
        result["memory"] = monitor.stop()

    output = args.output
    if output is None:
        stamp = started_at.astimezone().strftime("%Y%m%d-%H%M%S")
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.label).strip("-") or "default"
        output = PROJECT_DIR / ".benchmarks" / f"{stamp}-{safe_label}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    print()
    if "performance" in result:
        summary = result["performance"]["summary"]
        print(
            "performance: "
            f"TTFT {summary['ttft_seconds_median']:.3f}s, "
            f"prompt {summary['prompt_tokens_per_second_median']:.1f} tok/s, "
            f"generation {summary['generation_tokens_per_second_median']:.1f} tok/s, "
            f"prompt_n {summary['prompt_tokens_median']:.0f}, "
            f"predicted_n {summary['predicted_tokens_median']:.0f}"
        )
    if "quality" in result:
        quality = result["quality"]
        print(f"quality guards: {quality['passed']}/{quality['total']} passed")
        for case in quality["cases"]:
            print(f"  {case['id']}: {'PASS' if case['passed'] else 'FAIL'}")
    memory = result["memory"]
    print(
        "memory peaks: "
        f"VRAM {memory['gpu_vram_used_mib']['peak'] if memory['gpu_vram_used_mib'] else 'n/a'} MiB, "
        f"server RSS {memory['server_rss_mib']['peak'] if memory['server_rss_mib'] else 'n/a'} MiB, "
        f"system RAM used {memory['system_ram_used_mib']['peak'] if memory['system_ram_used_mib'] else 'n/a'} MiB"
    )
    print(f"saved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
