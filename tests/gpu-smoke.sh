#!/usr/bin/env bash
set -euo pipefail

NIXLOOM_LIBEXEC="${NIXLOOM_LIBEXEC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=config/lib.sh
source "${NIXLOOM_LIBEXEC}/config/lib.sh"
resolve_config_file ""

BASE_URL=""
SKIP_SD=0

usage() {
  cat <<'EOF'
Usage: ./tests/run.sh smoke [--base-url URL] [--skip-sd]

Checks the embedded chat template, normal chat, request-level thinking, vision,
and optional SD generation against a running stack. All chat capabilities must
use the same LLM ID.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      [[ $# -ge 2 ]] || { printf '%s requires a value\n' "$1" >&2; exit 2; }
      BASE_URL="${2%/}"
      shift 2
      ;;
    --skip-sd)
      SKIP_SD=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

BASE_URL="${BASE_URL:-http://127.0.0.1:$(cfg_required '.ports.llama' 'ports.llama')}"

MODEL_ID="$(llm_id)"
WORK_DIR="$(mktemp -d)"
IMAGE_DATA='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='

post_json() {
  local endpoint="$1" payload="$2" output="$3" status
  status="$(curl --max-time 900 -sS -o "${output}" -w '%{http_code}' \
    -H 'Content-Type: application/json' -d "${payload}" "${BASE_URL}${endpoint}" || true)"
  if [[ "${status}" != "200" ]]; then
    printf '%s returned HTTP %s\n' "${endpoint}" "${status:-000}" >&2
    sed -n '1,40p' "${output}" >&2
    return 1
  fi
}

restore_llm() {
  local payload
  payload="$(jq -cn --arg model "${MODEL_ID}" '{
    model: $model,
    messages: [{role: "user", content: "Reply OK."}],
    max_tokens: 8,
    chat_template_kwargs: {enable_thinking: false}
  }')"
  curl --max-time 900 -sS -o /dev/null \
    -H 'Content-Type: application/json' -d "${payload}" \
    "${BASE_URL}/v1/chat/completions" >/dev/null 2>&1 || true
}

on_exit() {
  local rc=$?
  restore_llm
  rm -r -- "${WORK_DIR}"
  exit "${rc}"
}
trap on_exit EXIT

curl --max-time 10 -fsS "${BASE_URL}/health" >/dev/null

# llama-swap exposes upstream properties below the model path, while a direct
# llama-server URL exposes /props. The template must come from the GGUF (or an
# explicit server override) and retain Qwen's tool, vision and thinking syntax.
if ! curl --max-time 30 -fsS \
  "${BASE_URL}/upstream/${MODEL_ID}/props" -o "${WORK_DIR}/props.json"; then
  curl --max-time 30 -fsS "${BASE_URL}/props" -o "${WORK_DIR}/props.json"
fi
jq -e '
  .chat_template as $template
  | ($template | type == "string")
    and ($template | contains("enable_thinking"))
    and ($template | contains("<|vision_start|>"))
    and ($template | contains("<tool_call>"))
' "${WORK_DIR}/props.json" >/dev/null
printf 'chat template: ok (GGUF/Jinja)\n'

chat_payload="$(jq -cn --arg model "${MODEL_ID}" '{
  model: $model,
  messages: [{role: "user", content: "Reply with CHAT_OK only."}],
  temperature: 0,
  max_tokens: 64,
  chat_template_kwargs: {enable_thinking: false}
}')"
post_json /v1/chat/completions "${chat_payload}" "${WORK_DIR}/chat.json"
jq -e '
  (.choices[0].message.content | length > 0)
  and ((.choices[0].message.reasoning_content // "") | length == 0)
' "${WORK_DIR}/chat.json" >/dev/null
jq -r '"chat: ok, \(.timings.predicted_per_second | floor) tok/s"' "${WORK_DIR}/chat.json"

think_payload="$(jq -cn --arg model "${MODEL_ID}" '{
  model: $model,
  messages: [{role: "user", content: "用一句话说明稀疏 MoE 的优势。"}],
  max_tokens: 256,
  chat_template_kwargs: {enable_thinking: true},
  thinking_budget_tokens: -1
}')"
post_json /v1/chat/completions "${think_payload}" "${WORK_DIR}/think.json"
jq -e '(.choices[0].message.reasoning_content // "" | length > 0)' \
  "${WORK_DIR}/think.json" >/dev/null
jq -r '"thinking: ok, \(.timings.predicted_per_second | floor) tok/s"' "${WORK_DIR}/think.json"

vision_payload="$(jq -cn --arg model "${MODEL_ID}" --arg image "${IMAGE_DATA}" '{
  model: $model,
  messages: [{role: "user", content: [
    {type: "text", text: "Briefly state the dominant image color."},
    {type: "image_url", image_url: {url: $image}}
  ]}],
  temperature: 0,
  max_tokens: 96,
  chat_template_kwargs: {enable_thinking: false}
}')"
post_json /v1/chat/completions "${vision_payload}" "${WORK_DIR}/vision.json"
jq -e '(.choices[0].message.content | length > 0)' "${WORK_DIR}/vision.json" >/dev/null
jq -r '"vision: ok, \(.timings.predicted_per_second | floor) tok/s"' "${WORK_DIR}/vision.json"

if [[ "${SKIP_SD}" != "1" ]]; then
  sd_payload="$(jq -cn '{
    prompt: "addmicrodetails, masterpiece, best quality, blue circle, simple background",
    width: 512,
    height: 512,
    steps: 2
  }')"
  post_json /upstream/sd/sdapi/v1/txt2img "${sd_payload}" "${WORK_DIR}/sd.json"
  jq -er '.images[0] | select(length > 1000)' "${WORK_DIR}/sd.json" >/dev/null
  jq -r '"sd: ok, image bytes=" + ((.images[0] | @base64d | length) | tostring)' \
    "${WORK_DIR}/sd.json"
fi
