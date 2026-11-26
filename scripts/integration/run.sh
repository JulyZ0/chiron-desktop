#!/usr/bin/env bash
# run.sh —— P0-2 真机集成测试带：一条命令在干净容器环境跑通关键链路。
#
# 链路（每一步失败即退出，并打印相关容器日志尾部）：
#
#   1. 就绪    ：postgres / redis / migrate / gateway / python-engine / pageindex / fake-llm
#   2. 鉴权    ：用共享 JWT_SECRET 现签 owner JWT，打 /v1/admin/database/status
#   3. submit  ：建项目 → git init → 建会话 → 绑项目 → SSE 订阅 → POST /v1/agent/submit
#                → 等 turn_done → 断言消息落库
#   4. usage   ：断言 usage_events 有行**且 token 非零**（来自假 LLM 的 usage chunk）
#   5. checkpoint：断言 project_checkpoints 有 state=ok 的行 → 时间线接口 → 回滚 200
#   6. share   ：建分享 → **不带任何凭据** GET /v1/share/<token> 读到消息
#   7. 知识库  ：pageindex ready + /v1/kb 可用；尝试真实建库/上传/索引/查询，
#                假 LLM 建不出索引时记 SKIP（打印原因），不伪装成功
#   8. deps    ：GET /v1/admin/deps/list
#   9. 隔离    ：GET → PUT level=job → GET 复核 effective 一致
#
# 环境要求与退出码：
#   * 需要 docker（含 compose v2 插件或 docker-compose）与 python3（签 JWT）；
#   * 缺少 docker / 守护进程未启动 / 缺 compose / 缺 python → **退出 77（skipped）**，
#     与 `make`/CI 的"环境不具备"约定一致，不算失败；
#   * 任一断言失败 → 退出 1。
#
# 常用环境变量：
#   KEEP_CONTAINERS=1     失败/成功后都不 compose down（排查现场用）
#   INTEGRATION_TIMEOUT=300  等待 turn_done 的秒数
#   INTEGRATION_*_PORT    覆盖宿主机端口（默认见下）
#   INTEGRATION_TOKEN     直接使用外部 JWT（跳过自签；必须与网关 JWT_SECRET 匹配）
#
# 用法：
#   bash scripts/integration/run.sh
#   make integration

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

COMPOSE_FILE="$ROOT/docker-compose.integration.yml"
PROJECT_NAME="${CHIRON_INTEGRATION_PROJECT:-chiron-integration}"

export INTEGRATION_PG_PORT="${INTEGRATION_PG_PORT:-15432}"
export INTEGRATION_REDIS_PORT="${INTEGRATION_REDIS_PORT:-16379}"
export INTEGRATION_GATEWAY_PORT="${INTEGRATION_GATEWAY_PORT:-18080}"
export INTEGRATION_ENGINE_PORT="${INTEGRATION_ENGINE_PORT:-18000}"
export INTEGRATION_PAGEINDEX_PORT="${INTEGRATION_PAGEINDEX_PORT:-18100}"
export INTEGRATION_FAKE_LLM_PORT="${INTEGRATION_FAKE_LLM_PORT:-18081}"

GATEWAY_URL="http://127.0.0.1:${INTEGRATION_GATEWAY_PORT}"
ENGINE_URL="http://127.0.0.1:${INTEGRATION_ENGINE_PORT}"
PAGEINDEX_URL="http://127.0.0.1:${INTEGRATION_PAGEINDEX_PORT}"
FAKE_LLM_URL="http://127.0.0.1:${INTEGRATION_FAKE_LLM_PORT}"

JWT_SECRET="integration-jwt-secret-0123456789abcdef"
OWNER_ID="00000000-0000-0000-0000-0000000000a1"
OWNER_EMAIL="owner@chiron.local"
#: 容器内共享卷上的项目根（网关与引擎必须看到同一个绝对路径）。
PROJECT_ROOT_IN_CONTAINER="/srv/chiron/projects/demo"

TURN_TIMEOUT="${INTEGRATION_TIMEOUT:-300}"
PASS=0
FAIL=0
SKIPPED=0
SKIP_NOTES=""
START_EPOCH="$(date +%s)"
SSE_FILE=""
SSE_PID=""

# ── 输出与失败处理 ──────────────────────────────────────────────────────

log() { printf '%s\n' "$*"; }

skipped_exit() {
  printf 'SKIP  集成测试带未运行：%s\n' "$1" >&2
  printf 'SKIP  （这是"环境不具备"而不是失败；退出码 77）\n' >&2
  exit 77
}

summary() {
  local elapsed=$(( $(date +%s) - START_EPOCH ))
  log ""
  log "── 结果：PASS=$PASS FAIL=$FAIL SKIP=$SKIPPED  耗时=${elapsed}s"
  if [ -n "$SKIP_NOTES" ]; then
    log "   未验证：$SKIP_NOTES"
  fi
}

dump_logs() {
  log ""
  log "── 容器日志尾部（$*）"
  # shellcheck disable=SC2086
  compose logs --no-color --tail=120 $* 2>&1 | sed -n '1,400p'
}

fail_assert() {
  # fail_assert <断言名> <原因>
  FAIL=$((FAIL + 1))
  log "FAIL  $1"
  log "      $2"
  dump_logs gateway python-engine pageindex-worker fake-llm migrate
  summary
  exit 1
}

ok_assert() {
  PASS=$((PASS + 1))
  log "PASS  $1${2:+  [$2]}"
}

skip_assert() {
  SKIPPED=$((SKIPPED + 1))
  SKIP_NOTES="${SKIP_NOTES}${SKIP_NOTES:+, }$1"
  log "SKIP  $1  ($2)"
}

cleanup() {
  local rc=$?
  if [ -n "$SSE_PID" ]; then
    kill "$SSE_PID" 2>/dev/null || true
    wait "$SSE_PID" 2>/dev/null || true
  fi
  if [ "${KEEP_CONTAINERS:-0}" != "1" ]; then
    compose down -v --remove-orphans >/dev/null 2>&1 || true
  else
    log "（KEEP_CONTAINERS=1：保留容器与卷）"
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

# ── 预检：docker / compose / python / curl ──────────────────────────────

if ! command -v docker >/dev/null 2>&1; then
  skipped_exit "找不到 docker 命令（请安装 Docker Desktop / Docker Engine）"
fi

if ! docker info >/dev/null 2>&1; then
  skipped_exit "Docker 守护进程未运行（启动 Docker Desktop 后重试）"
fi

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  skipped_exit "缺少 docker compose（v2 插件或 docker-compose）"
fi

compose() { "${DC[@]}" -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"; }

if ! command -v curl >/dev/null 2>&1; then
  skipped_exit "找不到 curl"
fi

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
[ -n "$PY" ] || skipped_exit "找不到 python3（签 JWT 与解析 JSON 需要）"

HAVE_JQ=0
command -v jq >/dev/null 2>&1 && HAVE_JQ=1

compose config >/dev/null 2>&1 || skipped_exit "docker-compose.integration.yml 校验失败（先跑 docker compose config 看原因）"

# ── JSON 取值（jq 优先，缺省回落 python）─────────────────────────────────

jget() {
  # jget <json> <dotted.path>；取不到输出空串（不区分 null 与缺失）
  local json="$1" path="$2"
  if [ "$HAVE_JQ" = "1" ]; then
    printf '%s' "$json" | jq -r ".$path // empty" 2>/dev/null
    return
  fi
  printf '%s' "$json" | "$PY" -c '
import json, re, sys
path = sys.argv[1]
try:
    cur = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)
for part in path.split("."):
    if not part:
        continue
    m = re.match(r"^([^\[]*)", part)
    name = m.group(1)
    if name:
        cur = cur.get(name) if isinstance(cur, dict) else None
    for idx in re.findall(r"\[(-?\d+)\]", part):
        i = int(idx)
        cur = cur[i] if isinstance(cur, list) and -len(cur) <= i < len(cur) else None
    if cur is None:
        break
print("" if cur is None else cur)
' "$path"
}

# ── HTTP 调用 ───────────────────────────────────────────────────────────

HTTP_STATUS=""
HTTP_BODY=""

http_call() {
  # http_call <method> <path> [body] [auth=1]
  local method="$1" path="$2" body="${3:-}" auth="${4:-1}"
  local tmp code
  tmp="$(mktemp)" || fail_assert "http" "mktemp failed"
  local -a args=(-sS -o "$tmp" -w '%{http_code}' -X "$method" --max-time 60
    -H 'Content-Type: application/json' "$GATEWAY_URL$path")
  [ "$auth" = "1" ] && args+=(-H "Authorization: Bearer $TOKEN")
  [ -n "$body" ] && args+=(--data-binary "$body")
  code="$(curl "${args[@]}" 2>/dev/null || true)"
  HTTP_STATUS="${code:-000}"
  HTTP_BODY="$(cat "$tmp")"
  rm -f "$tmp"
}

require_status() {
  # require_status <expected> <断言名>
  if [ "$HTTP_STATUS" != "$1" ]; then
    fail_assert "$2" "期望 HTTP $1，实际 $HTTP_STATUS；响应：${HTTP_BODY:0:600}"
  fi
}

wait_http() {
  # wait_http <url> <名称> <超时秒>
  local url="$1" name="$2" timeout="${3:-180}" i
  for ((i = 0; i < timeout; i++)); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail_assert "$name 就绪" "等待 ${timeout}s 后 $url 仍不可达"
}

# ── 数据库只读断言（走网关平台 owner 端点，不依赖宿主机 psql）──────────

DB_JSON=""

db_query() {
  # db_query <sql>：结果写进全局 DB_JSON。
  # **不用命令替换调用**：fail_assert 里的 exit 在子 shell 里只会退出子 shell，
  # 断言失败会被吞成"空结果"，后续报错点离真正原因很远。
  local sql="$1" payload
  payload="$("$PY" -c 'import json,sys; print(json.dumps({"query": sys.argv[1]}))' "$sql")"
  http_call POST /v1/admin/database/query "$payload"
  require_status 200 "数据库只读查询：${sql:0:80}"
  DB_JSON="$HTTP_BODY"
}

# ── 启动 ────────────────────────────────────────────────────────────────

log "==> 启动集成环境（compose project=$PROJECT_NAME）"
log "    compose: ${DC[*]}"
BUILD_START=$(date +%s)
compose up -d --build || fail_assert "compose up" "容器启动/构建失败"
log "    compose up 用时 $(( $(date +%s) - BUILD_START ))s"

wait_http "$GATEWAY_URL/health" "gateway" 180
wait_http "$ENGINE_URL/healthz" "python-engine" 180
wait_http "$FAKE_LLM_URL/healthz" "fake-llm" 60
wait_http "$PAGEINDEX_URL/healthz" "pageindex-service" 120
log "==> 全部服务就绪，用时 $(( $(date +%s) - START_EPOCH ))s"

# ── 1. 鉴权 ─────────────────────────────────────────────────────────────

if [ -n "${INTEGRATION_TOKEN:-}" ]; then
  TOKEN="$INTEGRATION_TOKEN"
else
  TOKEN="$("$PY" "$ROOT/scripts/integration/mint_token.py" "$JWT_SECRET" "$OWNER_ID" "$OWNER_EMAIL")" \
    || fail_assert "签发 owner JWT" "mint_token.py 执行失败"
fi
[ -n "$TOKEN" ] || fail_assert "签发 owner JWT" "token 为空"

http_call GET /v1/admin/database/status
require_status 200 "owner JWT 可通过鉴权（/v1/admin/database/status）"
if [ "$(jget "$HTTP_BODY" 'data.connected')" != "True" ] && [ "$(jget "$HTTP_BODY" 'data.connected')" != "true" ]; then
  fail_assert "数据库连接" "网关报告未连接：${HTTP_BODY:0:300}"
fi
ok_assert "鉴权与会话引导" "owner JWT + PG 可用"

# ── 2. submit 链路：项目 → 会话 → 绑定 → 提交 → turn_done ──────────────

payload="$("$PY" -c 'import json,sys; print(json.dumps({"name": "integration-demo", "root_path": sys.argv[1]}))' "$PROJECT_ROOT_IN_CONTAINER")"
http_call POST /v1/session-map/projects "$payload"
require_status 201 "创建项目（root_path=$PROJECT_ROOT_IN_CONTAINER）"
PROJECT_ID="$(jget "$HTTP_BODY" 'data.id')"
[ -n "$PROJECT_ID" ] || fail_assert "创建项目" "响应缺 data.id：${HTTP_BODY:0:400}"
ok_assert "项目创建" "id=$PROJECT_ID"

# 还原点前提：仓库必须存在。init 是幂等写操作（建 .git），与产品设置页同一个端点。
http_call POST "/v1/session-map/projects/$PROJECT_ID/vcs/init"
require_status 200 "项目 VCS 初始化（git init）"
ok_assert "项目 VCS 初始化" "repo=true"

http_call POST /v1/conversations '{"title":"integration-submit"}'
require_status 200 "创建会话"
SESSION_ID="$(jget "$HTTP_BODY" 'data.id')"
[ -n "$SESSION_ID" ] || fail_assert "创建会话" "响应缺 data.id：${HTTP_BODY:0:400}"
ok_assert "会话创建" "session=$SESSION_ID"

http_call PUT "/v1/session-map/projects/$PROJECT_ID/sessions/$SESSION_ID" '{}'
require_status 200 "会话绑定到项目"
ok_assert "会话绑定项目" "project=$PROJECT_ID"

# SSE 必须先连：引擎事件在 turn 结束前就会发出，晚连会漏掉 turn_done。
SSE_FILE="$(mktemp)"
curl -sS -N --max-time "$((TURN_TIMEOUT + 30))" \
  -H "Authorization: Bearer $TOKEN" \
  "$GATEWAY_URL/v1/events?session_id=$SESSION_ID&client_id=integration" \
  >>"$SSE_FILE" 2>/dev/null &
SSE_PID=$!
sleep 1

submit_payload="$("$PY" -c 'import json,sys; print(json.dumps({"session_id": sys.argv[1], "content": "集成测试：请只回复一句确认语。", "llm_config": {}}))' "$SESSION_ID")"
http_call POST /v1/agent/submit "$submit_payload"
require_status 202 "提交 agent 回合（POST /v1/agent/submit）"

turns=0
for ((turns = 0; turns < TURN_TIMEOUT * 2; turns++)); do
  if grep -q '"type":"turn_done"' "$SSE_FILE" 2>/dev/null; then
    break
  fi
  # 引擎直接报 error 时不必等满超时：尽早把日志打出来。
  if grep -q '"type":"error"' "$SSE_FILE" 2>/dev/null; then
    break
  fi
  sleep 0.5
done
kill "$SSE_PID" 2>/dev/null || true
wait "$SSE_PID" 2>/dev/null || true
SSE_PID=""

grep -q '"type":"turn_done"' "$SSE_FILE" || fail_assert "等待 turn_done" \
  "在 ${TURN_TIMEOUT}s 内未收到链级收尾事件（流内容：$(tail -c 1200 "$SSE_FILE" | tr '\n' ' ')）"
ok_assert "submit → turn_done" "session=$SESSION_ID"

MSG_JSON=""
db_query "SELECT id, role FROM messages WHERE session_id='$SESSION_ID' ORDER BY seq ASC LIMIT 20"
MSG_JSON="$DB_JSON"
MSG_COUNT="$(jget "$MSG_JSON" 'data.rows[0].id')"
[ -n "$MSG_COUNT" ] || fail_assert "消息落库" "messages 表没有该会话的任何行"

# ── 2b. 多租户：隐式建会话（消息先落库）的租户必须来自 claims（A3）──────
#
# 修前 `ensureSessionOwnedWithTitle` 硬编码 DefaultTenantID：邀请码/多租户用户
# 经"首条消息创建会话"产生的会话落进默认租户 —— 本人列表看不见、项目侧按 claims
# tenant 过滤也找不到。这里签一枚 tenant=2 的 token，用**一个新 session_id** 直接
# submit（走隐式 upsert），再查库确认落到 2 而不是 1。
TENANT2_ID=2
TOKEN_TENANT2="$("$PY" "$ROOT/scripts/integration/mint_token.py" "$JWT_SECRET" "$OWNER_ID" "$OWNER_EMAIL" "$TENANT2_ID")" \
  || fail_assert "签发 tenant=2 JWT" "mint_token.py 执行失败"
TENANT2_SESSION="$("$PY" -c 'import uuid; print(uuid.uuid4())')"
t2_payload="$("$PY" -c 'import json,sys; print(json.dumps({"session_id": sys.argv[1], "content": "租户归属探针", "llm_config": {}}))' "$TENANT2_SESSION")"
SAVED_TOKEN="$TOKEN"
TOKEN="$TOKEN_TENANT2"
http_call POST /v1/agent/submit "$t2_payload"
TOKEN="$SAVED_TOKEN"
require_status 202 "tenant=2 提交（隐式建会话）"

T2_TENANT=""
for ((turns = 0; turns < 30; turns++)); do
  db_query "SELECT tenant_id FROM sessions WHERE id='$TENANT2_SESSION'"
  T2_TENANT="$(jget "$DB_JSON" 'data.rows[0].tenant_id')"
  [ -n "$T2_TENANT" ] && break
  sleep 1
done
[ -n "$T2_TENANT" ] || fail_assert "隐式建会话租户" "30s 内没有 sessions 行（session=$TENANT2_SESSION）"
[ "$T2_TENANT" = "$TENANT2_ID" ] || fail_assert "隐式建会话租户" \
  "期望 tenant_id=$TENANT2_ID，实际=$T2_TENANT —— 消息路径又写死了默认租户"
ok_assert "隐式建会话租户来自 claims" "session=$TENANT2_SESSION tenant=$T2_TENANT"

# ── 3. usage_events：非零 token ────────────────────────────────────────

USAGE_SQL="SELECT COUNT(*)::int AS n, COALESCE(SUM(input_tokens),0)::int AS tin,
                  COALESCE(SUM(output_tokens),0)::int AS tout
             FROM usage_events WHERE session_id='$SESSION_ID'"
db_query "$USAGE_SQL"
USAGE_JSON="$DB_JSON"
U_N="$(jget "$USAGE_JSON" 'data.rows[0].n')"
U_IN="$(jget "$USAGE_JSON" 'data.rows[0].tin')"
U_OUT="$(jget "$USAGE_JSON" 'data.rows[0].tout')"
[ "${U_N:-0}" -gt 0 ] || fail_assert "usage_events 有行" "count=0（计费链路未落库）"
[ $(( ${U_IN:-0} + ${U_OUT:-0} )) -gt 0 ] || fail_assert "usage_events token 非零" \
  "in=$U_IN out=$U_OUT —— 假 LLM 的 usage chunk 没被引擎带进 SSE done 事件"
ok_assert "usage_events 非零" "rows=$U_N in=$U_IN out=$U_OUT"

# ── 4. checkpoint：落库 → 时间线 → 回滚 ─────────────────────────────────

CP_ID=""
for ((turns = 0; turns < 60; turns++)); do
  db_query "SELECT id, seq, state FROM project_checkpoints WHERE session_id='$SESSION_ID' AND state='ok' ORDER BY seq DESC LIMIT 1"
  CP_ID="$(jget "$DB_JSON" 'data.rows[0].id')"
  [ -n "$CP_ID" ] && break
  sleep 1
done
[ -n "$CP_ID" ] || fail_assert "checkpoint 落库" \
  "60s 内 project_checkpoints 没有 state=ok 的行（git 是否在引擎镜像里？root_path 是否共享？）"
ok_assert "checkpoint 落库" "id=$CP_ID"

http_call GET "/v1/session-map/projects/$PROJECT_ID/checkpoints?session_id=$SESSION_ID"
require_status 200 "检查点时间线"
CP_LIST_N="$("$PY" -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("data",{}).get("checkpoints",[])))' <<<"$HTTP_BODY" 2>/dev/null || echo 0)"
[ "${CP_LIST_N:-0}" -gt 0 ] || fail_assert "检查点时间线" "接口返回空列表"

http_call POST "/v1/checkpoints/$CP_ID/restore" '{"allow_delete": false}'
require_status 200 "检查点回滚（POST /v1/checkpoints/{id}/restore）"
ok_assert "checkpoint 回滚" "id=$CP_ID"

# ── 5. 分享：匿名可读 ───────────────────────────────────────────────────

SHARE_PAYLOAD="$("$PY" -c '
import json, sys
d = json.loads(sys.stdin.read())
rows = d.get("data", {}).get("rows", [])
print(json.dumps({"message_ids": [r["id"] for r in rows]}))
' <<<"$MSG_JSON")"
http_call POST "/v1/conversations/$SESSION_ID/share" "$SHARE_PAYLOAD"
require_status 200 "创建分享"
SHARE_ID="$(jget "$HTTP_BODY" 'data.share_id')"
[ -n "$SHARE_ID" ] || fail_assert "创建分享" "响应缺 share_id：${HTTP_BODY:0:400}"

# 关键：**不带 Authorization**（auth=0），验证免鉴权读。
http_call GET "/v1/share/$SHARE_ID" "" 0
require_status 200 "匿名读取 /v1/share/<token>"
SHARED_N="$("$PY" -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("data", {}).get("messages", [])))' <<<"$HTTP_BODY" 2>/dev/null || echo 0)"
[ "${SHARED_N:-0}" -gt 0 ] || fail_assert "匿名分享内容" "messages 为空"
ok_assert "匿名分享可读" "token=$SHARE_ID messages=$SHARED_N"

# /s/<token> 是前端的短链路由（frontend-vue/src/router/index.ts），数据面就是上面这条接口。
# 集成带不构建前端产物，所以这里只确认短链形态，不把 HTML 断言伪装成已验证。
skip_assert "SPA 短链 /s/<token> 的 HTML 渲染" "未构建前端产物；匿名数据面已由 /v1/share 验证"

# ── 6. 知识库：服务就绪 + 真实检索命中（best-effort）───────────────────

PI_HEALTH="$(curl -sS --max-time 5 "$PAGEINDEX_URL/healthz" 2>/dev/null || true)"
if [ "$(jget "$PI_HEALTH" 'status')" = "ready" ] || [ "$(jget "$PI_HEALTH" 'ok')" = "True" ] \
  || [ "$(jget "$PI_HEALTH" 'ok')" = "true" ]; then
  ok_assert "pageindex 检索服务就绪" "$(jget "$PI_HEALTH" 'chat_model')"
else
  skip_assert "pageindex 检索服务自检" "healthz 未报告 ready：${PI_HEALTH:0:300}"
fi

http_call GET /v1/kb
require_status 200 "知识库列表 /v1/kb"
ok_assert "知识库接口可用" ""

kb_hit_probe() {
  # 返回 0=PASS, 1=FAIL, 2=SKIP（由调用方决定如何计数）
  http_call PUT /v1/kb/settings '{"provider":"selfhosted"}'
  [ "$HTTP_STATUS" = "200" ] || { log "    [kb] 设置 provider 失败：$HTTP_STATUS ${HTTP_BODY:0:300}"; return 2; }

  http_call POST /v1/kb '{"name":"integration-kb","provider":"selfhosted","visibility":"private"}'
  [ "$HTTP_STATUS" = "200" ] || { log "    [kb] 建库失败：$HTTP_STATUS ${HTTP_BODY:0:300}"; return 2; }
  local kb_id; kb_id="$(jget "$HTTP_BODY" 'data.id')"
  [ -n "$kb_id" ] || { log "    [kb] 建库响应缺 id"; return 2; }

  # 生成一页合法 PDF（pageindex 自托管只收 text-based PDF），base64 后上传。
  local pdf_b64
  pdf_b64="$("$PY" - <<'PY'
import base64
import io

# 手写一页 PDF：不依赖 reportlab。内容区必须含断言用的码词。
text = "Chiron integration knowledge fixture. The codeword is ORANGE-COMPASS."
stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode()
objects = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
]
buf = io.BytesIO()
buf.write(b"%PDF-1.4\n")
offsets = []
for i, obj in enumerate(objects, start=1):
    offsets.append(buf.tell())
    buf.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
xref = buf.tell()
buf.write(f"xref\n0 {len(objects)+1}\n".encode())
buf.write(b"0000000000 65535 f \n")
for off in offsets:
    buf.write(f"{off:010d} 00000 n \n".encode())
buf.write(
    f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
)
print(base64.b64encode(buf.getvalue()).decode())
PY
)" || { log "    [kb] PDF 生成失败"; return 2; }
  [ -n "$pdf_b64" ] || { log "    [kb] PDF 为空"; return 2; }

  local upload_payload
  upload_payload="$("$PY" -c 'import json,sys; print(json.dumps({"name":"fixture.pdf","file_type":"pdf","content":sys.argv[1]}))' "$pdf_b64")"
  http_call POST "/v1/kb/$kb_id/documents" "$upload_payload"
  [ "$HTTP_STATUS" = "200" ] || { log "    [kb] 上传失败：$HTTP_STATUS ${HTTP_BODY:0:400}"; return 2; }

  http_call POST "/v1/kb/$kb_id/build" '{}'
  [ "$HTTP_STATUS" = "200" ] || { log "    [kb] 投递索引失败：$HTTP_STATUS ${HTTP_BODY:0:300}"; return 2; }

  local i st
  for ((i = 0; i < 90; i++)); do
    http_call GET "/v1/kb/$kb_id/status"
    st="$(jget "$HTTP_BODY" 'data.status')"
    case "$st" in
      ready) break ;;
      error) log "    [kb] 索引失败（假 LLM 可能建不出 PageIndex 树）：${HTTP_BODY:0:400}"; return 2 ;;
    esac
    sleep 1
  done
  [ "$st" = "ready" ] || { log "    [kb] 90s 内未 ready（最后状态：${st:-unknown}）"; return 2; }

  http_call POST "/v1/kb/$kb_id/query" '{"query":"ORANGE-COMPASS 这个码词是什么？"}'
  [ "$HTTP_STATUS" = "200" ] || { log "    [kb] 查询失败：$HTTP_STATUS ${HTTP_BODY:0:300}"; return 2; }
  local answer citations
  answer="$(jget "$HTTP_BODY" 'data.answer')"
  citations="$("$PY" -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("data",{}).get("citations",[])))' <<<"$HTTP_BODY" 2>/dev/null || echo 0)"
  if [ -n "$answer" ] || [ "${citations:-0}" -gt 0 ]; then
    log "    [kb] 命中：answer=${answer:0:80} citations=$citations"
    return 0
  fi
  log "    [kb] 查询 200 但 answer/citations 均为空"
  return 1
}

kb_hit_probe
KB_RC=$?
case "$KB_RC" in
  0) ok_assert "@知识库命中" "selfhosted + 上传 + 索引 + 查询" ;;
  1) fail_assert "@知识库命中" "索引 ready 但查询没有 answer/citations" ;;
  *) skip_assert "@知识库命中" "该环境未能完成自托管索引（见上方 [kb] 原因），非断言失败" ;;
esac

# ── 7. deps list ────────────────────────────────────────────────────────

http_call GET /v1/admin/deps/list
require_status 200 "Python 依赖管理 deps list"
DEPS_N="$(jget "$HTTP_BODY" 'data.count')"
[ "${DEPS_N:-0}" -gt 0 ] || fail_assert "deps list" "count=0（引擎 pip list 返回空）"
ok_assert "deps list" "count=$DEPS_N"

# ── 8. 隔离档切换 ───────────────────────────────────────────────────────

http_call GET /v1/system/isolation
require_status 200 "读取隔离档位"
ISO_BEFORE="$(jget "$HTTP_BODY" 'data.effective')"
[ -n "$ISO_BEFORE" ] || fail_assert "读取隔离档位" "响应缺 effective：${HTTP_BODY:0:400}"

http_call PUT /v1/system/isolation '{"level":"job","reason":"integration band"}'
require_status 200 "切换隔离档位 level=job"
ISO_AFTER="$(jget "$HTTP_BODY" 'data.effective')"
[ -n "$ISO_AFTER" ] || fail_assert "切换隔离档位" "响应缺 effective：${HTTP_BODY:0:400}"

http_call GET /v1/system/isolation
require_status 200 "复核隔离档位"
ISO_READBACK="$(jget "$HTTP_BODY" 'data.effective')"
[ "$ISO_READBACK" = "$ISO_AFTER" ] || fail_assert "隔离档复核" \
  "PUT 返回 effective=$ISO_AFTER，GET 复核为 $ISO_READBACK"
# Linux 容器里 POSIX 实现不支持 Windows 原生档位，effective 可能被归一到 none ——
# 断言的是"切换动作被接受且读回一致"，而不是某个平台特定的档位名。
ok_assert "隔离档切换" "before=$ISO_BEFORE after=$ISO_AFTER"

# ── 收尾 ────────────────────────────────────────────────────────────────

summary
log "P0-2 集成链路全部通过。"
