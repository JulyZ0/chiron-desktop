"""权威基线：一次性描述全部 schema（Resource Market 与知识库启停落地后的终态）。

由 `pg_dump --schema-only` 从「原迁移链 0001..0005 跑完、再加 kb 与 market-install
两条 0006 分支」的库导出后固化，因此**全新库 `alembic upgrade head` 即为终态**。

## 为什么这次把 0001..0005 与两个 0006 合成一个文件

分头开发让两条 0006 的 `down_revision` 都指向 `0005`，合并后形成**两个 head**：
`alembic upgrade head` 直接报 Multiple head revisions；更麻烦的是 Go 侧
`internal/db/schema_version.go` 的 `ParseMigrationHead` 会返回 "multiple migration
heads" 错误，而 `cmd/chiron/main.go` 的 `verifySchemaVersion` 对该错误只记一条 Warn
就 return —— schema 守卫被**静默绕过**，应用照常启动。与其加一条 merge revision 把
分叉永久留在链上，不如按本仓「单一权威基线」的既定架构重新固化。

## 本轮相对旧基线（0001）的增量

  - 新增表 `market_installations`（市场安装记录，唯一约束
    `(tenant_id, user_id, source, kind, market_id)` 是幂等的落点）；
  - 新增列 `knowledge_bases.enabled`（用户意图，与索引状态机 `status` 正交）。

其余 schema 与合并前的目标状态**逐字一致** —— 基线不再承载"逐步演进"的历史，
它只描述终态。

## 合并进基线时丢掉的东西

被合并的 `0005_checkpoint_seq_unique` 在 upgrade 里有一条 `DELETE FROM
project_checkpoints`，用于在建唯一索引前清掉历史重复行。基线里没有数据可清
（全新库为空），因此该 DML 不再出现；`ux_project_checkpoints_session_seq_kind`
唯一约束本身仍然存在。

**改 schema 的方式：不要修改本文件，追加新的 revision。**
单一 head 是硬要求，分叉会被 `internal/db/schema_version.go` 直接判为错误。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_authoritative_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 顺序即 pg_dump 的可重放顺序：建表 → 序列与 OWNED BY → 列默认值 → 约束 → 索引。
# 不要重排：默认值依赖先建好的序列，主键约束依赖先建好的表。
_DDL: tuple[str, ...] = (
    """CREATE TABLE public.admin_model_configs (
    id character varying(36) NOT NULL,
    model_id character varying(50),
    display_name character varying(100),
    provider character varying(50),
    priority integer,
    weight integer,
    max_rpm integer,
    status character varying(20),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.agent_runs (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    session_id character varying(128),
    turn_id character varying(36),
    run_token character varying(64),
    status character varying(16) DEFAULT 'running'::character varying NOT NULL,
    checkpoint jsonb,
    checkpoint_at timestamp with time zone,
    instance_id character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.agent_sessions (
    id character varying(128) NOT NULL,
    user_id character varying(36),
    agent_id character varying(36),
    name character varying(128),
    task text NOT NULL,
    status character varying(16),
    result text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    tenant_id smallint NOT NULL
)""",
    """CREATE TABLE public.agents (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    name character varying(255),
    description text,
    system_prompt text,
    tools json,
    llm_config json,
    max_turns integer,
    timeout_seconds integer,
    enabled boolean,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    user_id character varying(36),
    visibility character varying(16),
    kb_id character varying(255),
    skills json,
    plugins json,
    kind character varying(16) DEFAULT 'chat'::character varying NOT NULL
)""",
    """CREATE TABLE public.api_keys (
    id character varying(36) NOT NULL,
    user_id character varying(36),
    name character varying(128),
    key_hash character varying(64),
    last_used_at timestamp without time zone,
    expires_at timestamp without time zone,
    created_at timestamp without time zone,
    revoked boolean
)""",
    """CREATE TABLE public.audit_logs (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    action character varying(64),
    resource_type character varying(64),
    resource_id character varying(64),
    details json,
    ip_address character varying(45),
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.checkpoint_untracked (
    checkpoint_id character varying(64) NOT NULL,
    path character varying(1024) NOT NULL,
    sha256 character varying(64) NOT NULL,
    size bigint DEFAULT 0 NOT NULL
)""",
    """CREATE TABLE public.conversation_shares (
    id character varying(36) NOT NULL,
    session_id character varying(128),
    user_id character varying(36),
    title character varying(255),
    message_ids text[] DEFAULT ARRAY[]::text[],
    created_at timestamp without time zone,
    revoked_at timestamp without time zone,
    expires_at timestamp with time zone DEFAULT (now() + '30 days'::interval)
)""",
    """CREATE TABLE public.knowledge_bases (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    visibility character varying(16) DEFAULT 'private'::character varying NOT NULL,
    provider character varying(16) NOT NULL,
    status character varying(16) DEFAULT 'disabled'::character varying NOT NULL,
    index_ref character varying(255),
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    document_count integer DEFAULT 0 NOT NULL,
    total_size_bytes bigint DEFAULT 0 NOT NULL,
    error_message text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    enabled boolean DEFAULT true NOT NULL
)""",
    """CREATE TABLE public.knowledge_documents (
    id character varying(36) NOT NULL,
    knowledge_base_id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36) NOT NULL,
    name character varying(255) NOT NULL,
    file_type character varying(32),
    file_size_bytes bigint DEFAULT 0 NOT NULL,
    storage_key character varying(512),
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    pageindex_doc_id character varying(64),
    page_count integer DEFAULT 0 NOT NULL,
    error_message text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.llm_models (
    id character varying(36) NOT NULL,
    provider character varying(32),
    name character varying(128),
    display_name character varying(128),
    enabled boolean,
    context_window integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    max_output_tokens integer
)""",
    """CREATE TABLE public.llm_provider_keys (
    id character varying(36) NOT NULL,
    provider character varying(50) NOT NULL,
    encrypted_key text NOT NULL,
    key_hash character varying(64) NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    remark text,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    weight integer DEFAULT 1 NOT NULL
)""",
    """CREATE TABLE public.market_installations (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36) NOT NULL,
    source character varying(64) NOT NULL,
    kind character varying(16) NOT NULL,
    market_id character varying(255) NOT NULL,
    ref json DEFAULT '{}'::json NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.media_assets (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    type character varying(16),
    name character varying(255),
    file_url character varying(1024),
    file_path character varying(512),
    mime_type character varying(64),
    thumbnail character varying(512),
    metadata json,
    tags character varying(255),
    category character varying(64),
    size bigint,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    parent_id character varying(64)
)""",
    """CREATE TABLE public.memory_summaries (
    id character varying(64) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(64),
    session_id character varying(64),
    content text NOT NULL,
    topics json,
    entities json,
    turn_start integer,
    turn_end integer,
    content_hash character varying(80),
    access_count integer,
    last_accessed_at timestamp without time zone,
    status character varying(16),
    created_at timestamp without time zone,
    embedding double precision[],
    embedding_model text,
    embedding_dim integer
)""",
    """CREATE TABLE public.message_archives (
    ref character varying(64) NOT NULL,
    session_id character varying(36) NOT NULL,
    byte_size bigint NOT NULL,
    storage_key text NOT NULL,
    sha256 character(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.messages (
    id character varying(36) NOT NULL,
    session_id character varying(36),
    role character varying(16),
    content text NOT NULL,
    tool_calls json,
    created_at timestamp without time zone,
    turn_id character varying(36),
    source character varying(32) DEFAULT ''::character varying NOT NULL,
    seq bigint NOT NULL,
    tool_call_id character varying(128),
    blob_ref character varying(64)
)""",
    """CREATE TABLE public.model_prices (
    model character varying(128) NOT NULL,
    provider character varying(50) DEFAULT ''::character varying NOT NULL,
    input_per_1k numeric(12,6) DEFAULT 0 NOT NULL,
    output_per_1k numeric(12,6) DEFAULT 0 NOT NULL,
    currency character varying(8) DEFAULT 'USD'::character varying NOT NULL,
    sources text DEFAULT ''::text NOT NULL,
    source_count integer DEFAULT 0 NOT NULL,
    verified_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.pending_interactions (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    session_id character varying(128),
    run_id character varying(64),
    turn_id character varying(36),
    kind character varying(16) NOT NULL,
    tool_call_id character varying(128) NOT NULL,
    tool_name character varying(128) DEFAULT ''::character varying NOT NULL,
    args_hash character varying(64) DEFAULT ''::character varying NOT NULL,
    request jsonb,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    decided_by character varying(36),
    decision jsonb,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '24:00:00'::interval) NOT NULL
)""",
    """CREATE TABLE public.project_checkpoints (
    id character varying(64) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    workspace_id character varying(64) NOT NULL,
    session_id character varying(36),
    turn_id character varying(36),
    seq bigint NOT NULL,
    commit_sha character varying(64) NOT NULL,
    tree_sha character varying(64) NOT NULL,
    parent_sha character varying(64),
    head_sha character varying(64),
    ref_name character varying(255) NOT NULL,
    kind character varying(16) NOT NULL,
    state character varying(16) DEFAULT 'ok'::character varying NOT NULL,
    error_code character varying(64),
    error_msg text,
    label character varying(255) DEFAULT ''::character varying NOT NULL,
    file_count integer DEFAULT 0 NOT NULL,
    skipped_count integer DEFAULT 0 NOT NULL,
    byte_size bigint DEFAULT 0 NOT NULL,
    branch_name character varying(255),
    applied_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.run_lineage_ledger (
    root_run_id character varying(64) NOT NULL,
    tenant_id smallint NOT NULL,
    cap_tokens bigint DEFAULT 0 NOT NULL,
    paused boolean DEFAULT false NOT NULL,
    pause_reason character varying(64),
    updated_by character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.session_folds (
    id character varying(36) NOT NULL,
    session_id character varying(36) NOT NULL,
    covered_from bigint NOT NULL,
    covered_to bigint NOT NULL,
    digest text NOT NULL,
    digest_model character varying(128) NOT NULL,
    digest_prompt character varying(32) NOT NULL,
    body_suffix bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT session_folds_range_ordered CHECK ((covered_to > covered_from))
)""",
    """CREATE TABLE public.session_map_nodes (
    id character varying(64) NOT NULL,
    workspace_id character varying(64) NOT NULL,
    session_id character varying(36),
    parent_node_id character varying(64),
    edge_kind character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    x integer DEFAULT 0 NOT NULL,
    y integer DEFAULT 0 NOT NULL,
    title character varying(255),
    color character varying(16),
    collapsed boolean DEFAULT false NOT NULL,
    hidden boolean DEFAULT false NOT NULL,
    pinned boolean DEFAULT false NOT NULL,
    branch_from_seq integer,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.session_map_workspaces (
    id character varying(64) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    name character varying(255) DEFAULT ''::character varying NOT NULL,
    viewport jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    root_path character varying(1024) DEFAULT ''::character varying NOT NULL,
    sort_index integer DEFAULT 0 NOT NULL
)""",
    """CREATE TABLE public.sessions (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    agent_id character varying(36),
    title character varying(255),
    status character varying(16),
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    pinned boolean,
    tag character varying(64),
    parent_session_id character varying(36),
    branch_from_seq integer,
    alias character varying(64),
    branch_mode character varying(16),
    branch_state character varying(16),
    branch_keep_tail integer
)""",
    """CREATE TABLE public.subagent_run_steps (
    id bigint NOT NULL,
    run_id character varying(64) NOT NULL,
    seq integer NOT NULL,
    kind character varying(16) NOT NULL,
    role character varying(16),
    tool_name character varying(64),
    tool_call_id character varying(128),
    content text,
    truncated boolean DEFAULT false NOT NULL,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.subagent_runs (
    id character varying(64) NOT NULL,
    root_session_id character varying(128) NOT NULL,
    turn_id character varying(64),
    parent_run_id character varying(64),
    depth integer DEFAULT 1 NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    agent_id character varying(36),
    profile_name character varying(128),
    task text NOT NULL,
    status character varying(16) NOT NULL,
    summary text,
    summary_format character varying(16) DEFAULT 'markdown'::character varying NOT NULL,
    artifacts jsonb DEFAULT '[]'::jsonb,
    write_paths jsonb DEFAULT '[]'::jsonb,
    read_only boolean DEFAULT false NOT NULL,
    input_tokens bigint DEFAULT 0 NOT NULL,
    output_tokens bigint DEFAULT 0 NOT NULL,
    steps integer DEFAULT 0 NOT NULL,
    cost_cents integer DEFAULT 0 NOT NULL,
    redacted_count integer DEFAULT 0 NOT NULL,
    error text,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    rerun_of character varying(64),
    inherited_messages integer,
    retryable boolean DEFAULT false NOT NULL,
    output_bytes bigint DEFAULT 0 NOT NULL,
    validator_mode character varying(16),
    validator_outcome character varying(16),
    validator_attempt integer DEFAULT 0 NOT NULL,
    error_code character varying(32),
    resumed_from character varying(64),
    resume_at_step integer,
    root_run_id character varying(64)
)""",
    """CREATE TABLE public.superagent_settings (
    tenant_id smallint NOT NULL,
    user_id character varying(36) NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    max_sessions integer DEFAULT 5 NOT NULL,
    day_token_budget integer DEFAULT 600000 NOT NULL,
    idle_minutes integer DEFAULT 10 NOT NULL,
    updated_at timestamp without time zone DEFAULT now()
)""",
    """CREATE TABLE public.superagent_takeovers (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36) NOT NULL,
    session_id character varying(64) NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    schedule_kind character varying(16) DEFAULT 'interval'::character varying NOT NULL,
    once_at timestamp without time zone,
    interval_seconds integer DEFAULT 1800,
    tools_mode character varying(16) DEFAULT 'auto'::character varying NOT NULL,
    daily_token_budget integer DEFAULT 120000 NOT NULL,
    tokens_used_today integer DEFAULT 0 NOT NULL,
    budget_day date,
    last_run_at timestamp without time zone,
    last_verdict character varying(24),
    last_reason text,
    melt_state character varying(16) DEFAULT ''::character varying NOT NULL,
    melt_reason text,
    cooldown_until timestamp without time zone,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
)""",
    """CREATE TABLE public.system_settings (
    id integer NOT NULL,
    category character varying(32),
    key character varying(64),
    value json,
    updated_at timestamp without time zone,
    updated_by character varying(36),
    encrypted boolean
)""",
    """CREATE TABLE public.task_idempotency (
    idempotency_key character varying(255) NOT NULL,
    task_id character varying(64),
    task_type character varying(64),
    tenant_id smallint NOT NULL,
    status character varying(16) DEFAULT 'running'::character varying NOT NULL,
    attempt integer DEFAULT 1 NOT NULL,
    result_ref text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
)""",
    """CREATE TABLE public.tasks (
    id character varying(36) NOT NULL,
    user_id character varying(36),
    type character varying(32),
    status character varying(16),
    priority integer,
    payload json,
    result json,
    error text,
    retries integer,
    max_retries integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.tenants (
    id smallint NOT NULL,
    name character varying(255),
    created_at timestamp without time zone,
    status character varying(16)
)""",
    """CREATE TABLE public.tool_calls (
    id character varying(36) NOT NULL,
    session_id character varying(36),
    message_id character varying(36),
    tool_name character varying(128),
    input json,
    output text NOT NULL,
    is_error boolean,
    duration_ms bigint,
    created_at timestamp without time zone,
    turn_id character varying(36),
    output_bytes integer
)""",
    """CREATE TABLE public.turns (
    id character varying(36) NOT NULL,
    session_id character varying(36),
    user_id character varying(36),
    status character varying(16),
    error text,
    input_tokens bigint,
    output_tokens bigint,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    created_at timestamp without time zone,
    cached_tokens bigint DEFAULT 0 NOT NULL,
    cache_hit boolean DEFAULT false NOT NULL,
    model character varying(128),
    origin character varying(32) DEFAULT ''::character varying NOT NULL
)""",
    """CREATE TABLE public.unified_messages (
    id integer NOT NULL,
    session_id character varying(36),
    role character varying(16),
    content text NOT NULL,
    metadata json,
    error text NOT NULL,
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.unified_sessions (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    title character varying(255),
    mode character varying(16),
    shared_context json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    runtime jsonb DEFAULT '{}'::jsonb NOT NULL
)""",
    """CREATE TABLE public.uploads (
    id character varying(64) NOT NULL,
    user_id character varying(64),
    name character varying(255),
    size bigint,
    mime_type character varying(64),
    purpose character varying(16),
    parent_id character varying(64),
    category character varying(64),
    chunk_size integer,
    chunk_count integer,
    chunks_received character varying(255),
    status character varying(16),
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    tenant_id smallint NOT NULL
)""",
    """CREATE TABLE public.usage_events (
    id bigint NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    session_id character varying(128),
    turn_id character varying(36),
    model character varying(128),
    provider character varying(64),
    input_tokens bigint DEFAULT 0 NOT NULL,
    output_tokens bigint DEFAULT 0 NOT NULL,
    cached_tokens bigint DEFAULT 0 NOT NULL,
    cost_cents integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.user_knowledge_settings (
    user_id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    provider character varying(16) NOT NULL,
    cloud_api_key_enc text,
    cloud_key_hash character varying(64),
    status character varying(16) DEFAULT 'disabled'::character varying NOT NULL,
    last_checked_at timestamp without time zone,
    error_message text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    cloud_folder_id character varying(64)
)""",
    """CREATE TABLE public.user_memory_entries (
    id character varying(64) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(64) NOT NULL,
    slot character varying(64) NOT NULL,
    item_key character varying(255) NOT NULL,
    item_value jsonb,
    confidence integer,
    source character varying(32),
    embedding jsonb,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    project character varying(120) DEFAULT ''::character varying NOT NULL
)""",
    """CREATE TABLE public.user_memory_profile (
    tenant_id smallint NOT NULL,
    user_id character varying(64) NOT NULL,
    slot character varying(32) NOT NULL,
    item_key character varying(128) NOT NULL,
    item_value json,
    confidence integer,
    source character varying(16),
    version integer,
    confirmed_at timestamp without time zone,
    last_referenced_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.user_onboarding (
    user_id character varying(36) NOT NULL,
    step character varying(32) NOT NULL,
    completed_at timestamp with time zone,
    skipped_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.users (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    email character varying(255),
    name character varying(128),
    password_hash character varying(255),
    role character varying(16),
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    phone character varying(32),
    password_set boolean,
    settings json
)""",
    """ALTER TABLE public.messages ALTER COLUMN seq ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.messages_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
)""",
    """CREATE SEQUENCE public.subagent_run_steps_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1""",
    """CREATE SEQUENCE public.system_settings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1""",
    """CREATE SEQUENCE public.tenants_id_seq
    AS smallint
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1""",
    """CREATE SEQUENCE public.unified_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1""",
    """CREATE SEQUENCE public.usage_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1""",
    """ALTER SEQUENCE public.subagent_run_steps_id_seq OWNED BY public.subagent_run_steps.id""",
    """ALTER SEQUENCE public.system_settings_id_seq OWNED BY public.system_settings.id""",
    """ALTER SEQUENCE public.tenants_id_seq OWNED BY public.tenants.id""",
    """ALTER SEQUENCE public.unified_messages_id_seq OWNED BY public.unified_messages.id""",
    """ALTER SEQUENCE public.usage_events_id_seq OWNED BY public.usage_events.id""",
    """ALTER TABLE ONLY public.subagent_run_steps ALTER COLUMN id SET DEFAULT nextval('public.subagent_run_steps_id_seq'::regclass)""",
    """ALTER TABLE ONLY public.system_settings ALTER COLUMN id SET DEFAULT nextval('public.system_settings_id_seq'::regclass)""",
    """ALTER TABLE ONLY public.tenants ALTER COLUMN id SET DEFAULT nextval('public.tenants_id_seq'::regclass)""",
    """ALTER TABLE ONLY public.unified_messages ALTER COLUMN id SET DEFAULT nextval('public.unified_messages_id_seq'::regclass)""",
    """ALTER TABLE ONLY public.usage_events ALTER COLUMN id SET DEFAULT nextval('public.usage_events_id_seq'::regclass)""",
    """ALTER TABLE ONLY public.admin_model_configs
    ADD CONSTRAINT admin_model_configs_model_id_key UNIQUE (model_id)""",
    """ALTER TABLE ONLY public.admin_model_configs
    ADD CONSTRAINT admin_model_configs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.checkpoint_untracked
    ADD CONSTRAINT checkpoint_untracked_pkey PRIMARY KEY (checkpoint_id, path)""",
    """ALTER TABLE ONLY public.conversation_shares
    ADD CONSTRAINT conversation_shares_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.knowledge_bases
    ADD CONSTRAINT knowledge_bases_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.knowledge_documents
    ADD CONSTRAINT knowledge_documents_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.llm_models
    ADD CONSTRAINT llm_models_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.llm_provider_keys
    ADD CONSTRAINT llm_provider_keys_key_hash_key UNIQUE (key_hash)""",
    """ALTER TABLE ONLY public.llm_provider_keys
    ADD CONSTRAINT llm_provider_keys_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.market_installations
    ADD CONSTRAINT market_installations_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.media_assets
    ADD CONSTRAINT media_assets_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.memory_summaries
    ADD CONSTRAINT memory_summaries_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.message_archives
    ADD CONSTRAINT message_archives_pkey PRIMARY KEY (ref)""",
    """ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.model_prices
    ADD CONSTRAINT model_prices_pkey PRIMARY KEY (model, provider)""",
    """ALTER TABLE ONLY public.pending_interactions
    ADD CONSTRAINT pending_interactions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.project_checkpoints
    ADD CONSTRAINT project_checkpoints_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.run_lineage_ledger
    ADD CONSTRAINT run_lineage_ledger_pkey PRIMARY KEY (root_run_id)""",
    """ALTER TABLE ONLY public.session_folds
    ADD CONSTRAINT session_folds_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.session_map_nodes
    ADD CONSTRAINT session_map_nodes_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.session_map_workspaces
    ADD CONSTRAINT session_map_workspaces_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.subagent_run_steps
    ADD CONSTRAINT subagent_run_steps_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.subagent_runs
    ADD CONSTRAINT subagent_runs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.superagent_settings
    ADD CONSTRAINT superagent_settings_pkey PRIMARY KEY (tenant_id, user_id)""",
    """ALTER TABLE ONLY public.superagent_takeovers
    ADD CONSTRAINT superagent_takeovers_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.superagent_takeovers
    ADD CONSTRAINT superagent_takeovers_tenant_id_user_id_session_id_key UNIQUE (tenant_id, user_id, session_id)""",
    """ALTER TABLE ONLY public.system_settings
    ADD CONSTRAINT system_settings_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.task_idempotency
    ADD CONSTRAINT task_idempotency_pkey PRIMARY KEY (idempotency_key)""",
    """ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.tool_calls
    ADD CONSTRAINT tool_calls_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.unified_messages
    ADD CONSTRAINT unified_messages_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.unified_sessions
    ADD CONSTRAINT unified_sessions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.usage_events
    ADD CONSTRAINT usage_events_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.user_knowledge_settings
    ADD CONSTRAINT user_knowledge_settings_pkey PRIMARY KEY (user_id)""",
    """ALTER TABLE ONLY public.user_memory_entries
    ADD CONSTRAINT user_memory_entries_identity_uniq UNIQUE (tenant_id, user_id, project, slot, item_key)""",
    """ALTER TABLE ONLY public.user_memory_entries
    ADD CONSTRAINT user_memory_entries_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.user_memory_profile
    ADD CONSTRAINT user_memory_profile_pkey PRIMARY KEY (tenant_id, user_id, slot, item_key)""",
    """ALTER TABLE ONLY public.user_onboarding
    ADD CONSTRAINT user_onboarding_pkey PRIMARY KEY (user_id, step)""",
    """ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id)""",
    """ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.checkpoint_untracked
    ADD CONSTRAINT checkpoint_untracked_checkpoint_fk FOREIGN KEY (checkpoint_id) REFERENCES public.project_checkpoints(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.knowledge_bases
    ADD CONSTRAINT knowledge_bases_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.knowledge_bases
    ADD CONSTRAINT knowledge_bases_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.knowledge_documents
    ADD CONSTRAINT knowledge_documents_knowledge_base_id_fkey FOREIGN KEY (knowledge_base_id) REFERENCES public.knowledge_bases(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.knowledge_documents
    ADD CONSTRAINT knowledge_documents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.knowledge_documents
    ADD CONSTRAINT knowledge_documents_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.media_assets
    ADD CONSTRAINT media_assets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.message_archives
    ADD CONSTRAINT message_archives_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id)""",
    """ALTER TABLE ONLY public.session_folds
    ADD CONSTRAINT session_folds_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.session_map_nodes
    ADD CONSTRAINT session_map_nodes_parent_node_id_fkey FOREIGN KEY (parent_node_id) REFERENCES public.session_map_nodes(id) ON DELETE SET NULL""",
    """ALTER TABLE ONLY public.session_map_nodes
    ADD CONSTRAINT session_map_nodes_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.session_map_nodes
    ADD CONSTRAINT session_map_nodes_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.session_map_workspaces(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id)""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_parent_session_id_fkey FOREIGN KEY (parent_session_id) REFERENCES public.sessions(id) ON DELETE SET NULL""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.subagent_run_steps
    ADD CONSTRAINT subagent_run_steps_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.subagent_runs(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.subagent_runs
    ADD CONSTRAINT subagent_runs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL""",
    """ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id)""",
    """ALTER TABLE ONLY public.unified_messages
    ADD CONSTRAINT unified_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.unified_sessions(id)""",
    """ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.user_knowledge_settings
    ADD CONSTRAINT user_knowledge_settings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.user_knowledge_settings
    ADD CONSTRAINT user_knowledge_settings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.user_onboarding
    ADD CONSTRAINT user_onboarding_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """CREATE INDEX agents_kind_idx ON public.agents USING btree (kind)""",
    """CREATE INDEX idx_superagent_takeovers_user ON public.superagent_takeovers USING btree (tenant_id, user_id, enabled)""",
    """CREATE INDEX idx_usage_events_session ON public.usage_events USING btree (session_id)""",
    """CREATE INDEX idx_usage_events_user_model_time ON public.usage_events USING btree (user_id, model, created_at DESC)""",
    """CREATE INDEX idx_usage_events_user_time ON public.usage_events USING btree (user_id, created_at DESC)""",
    """CREATE INDEX idx_user_onboarding_step ON public.user_onboarding USING btree (step)""",
    """CREATE INDEX ix_agent_runs_session_updated ON public.agent_runs USING btree (session_id, updated_at DESC)""",
    """CREATE INDEX ix_agent_runs_status_cp ON public.agent_runs USING btree (status, checkpoint_at)""",
    """CREATE INDEX ix_agent_sessions_user_created ON public.agent_sessions USING btree (user_id, created_at DESC)""",
    """CREATE INDEX ix_audit_logs_created ON public.audit_logs USING btree (created_at DESC)""",
    """CREATE INDEX ix_checkpoint_untracked_sha ON public.checkpoint_untracked USING btree (sha256)""",
    """CREATE INDEX ix_knowledge_bases_disabled ON public.knowledge_bases USING btree (tenant_id, user_id) WHERE (enabled = false)""",
    """CREATE INDEX ix_knowledge_bases_owner ON public.knowledge_bases USING btree (tenant_id, user_id)""",
    """CREATE INDEX ix_knowledge_bases_status ON public.knowledge_bases USING btree (tenant_id, status)""",
    """CREATE INDEX ix_knowledge_documents_kb ON public.knowledge_documents USING btree (knowledge_base_id, status)""",
    """CREATE INDEX ix_llm_provider_keys_provider ON public.llm_provider_keys USING btree (provider)""",
    """CREATE INDEX ix_market_installations_owner ON public.market_installations USING btree (tenant_id, user_id, kind)""",
    """CREATE INDEX ix_media_assets_user_parent ON public.media_assets USING btree (user_id, parent_id)""",
    """CREATE INDEX ix_message_archives_session ON public.message_archives USING btree (session_id)""",
    """CREATE INDEX ix_messages_session_created ON public.messages USING btree (session_id, created_at DESC, id DESC)""",
    """CREATE UNIQUE INDEX ix_messages_session_seq ON public.messages USING btree (session_id, seq)""",
    """CREATE INDEX ix_messages_turn_id ON public.messages USING btree (turn_id)""",
    """CREATE INDEX ix_model_prices_verified_at ON public.model_prices USING btree (verified_at)""",
    """CREATE INDEX ix_pending_interactions_expiry ON public.pending_interactions USING btree (status, expires_at) WHERE ((status)::text = ANY (ARRAY[('pending'::character varying)::text, ('answered'::character varying)::text]))""",
    """CREATE INDEX ix_pending_interactions_grant ON public.pending_interactions USING btree (session_id, args_hash) WHERE ((status)::text = 'answered'::text)""",
    """CREATE INDEX ix_pending_interactions_session_open ON public.pending_interactions USING btree (session_id, created_at) WHERE ((status)::text = ANY (ARRAY[('pending'::character varying)::text, ('answered'::character varying)::text]))""",
    """CREATE INDEX ix_project_checkpoints_project ON public.project_checkpoints USING btree (workspace_id, created_at DESC)""",
    """CREATE INDEX ix_project_checkpoints_session ON public.project_checkpoints USING btree (session_id, seq DESC)""",
    """CREATE INDEX ix_run_lineage_ledger_paused ON public.run_lineage_ledger USING btree (tenant_id, updated_at) WHERE paused""",
    """CREATE INDEX ix_session_folds_session ON public.session_folds USING btree (session_id, covered_to DESC)""",
    """CREATE INDEX ix_sessions_parent_session_id ON public.sessions USING btree (parent_session_id)""",
    """CREATE INDEX ix_sessions_tag ON public.sessions USING btree (tag)""",
    """CREATE INDEX ix_smn_project_lookup ON public.session_map_nodes USING btree (workspace_id, edge_kind, created_at)""",
    """CREATE INDEX ix_subagent_runs_root_run_id ON public.subagent_runs USING btree (root_run_id)""",
    """CREATE INDEX ix_task_idempotency_status ON public.task_idempotency USING btree (status)""",
    """CREATE INDEX ix_task_idempotency_status_updated ON public.task_idempotency USING btree (status, updated_at)""",
    """CREATE INDEX ix_task_idempotency_task_id ON public.task_idempotency USING btree (task_id)""",
    """CREATE INDEX ix_tool_calls_session_created ON public.tool_calls USING btree (session_id, created_at DESC, id DESC)""",
    """CREATE INDEX ix_tool_calls_turn_id ON public.tool_calls USING btree (turn_id)""",
    """CREATE INDEX ix_turns_created_at ON public.turns USING btree (created_at)""",
    """CREATE INDEX ix_turns_session_id ON public.turns USING btree (session_id)""",
    """CREATE INDEX ix_turns_status ON public.turns USING btree (status)""",
    """CREATE INDEX ix_turns_status_created ON public.turns USING btree (status, created_at)""",
    """CREATE INDEX ix_uploads_user_status ON public.uploads USING btree (user_id, status)""",
    """CREATE INDEX ix_user_memory_entries_lookup ON public.user_memory_entries USING btree (tenant_id, user_id, status)""",
    """CREATE INDEX ix_user_memory_entries_project ON public.user_memory_entries USING btree (tenant_id, user_id, project, status)""",
    """CREATE INDEX ix_users_created ON public.users USING btree (created_at DESC)""",
    """CREATE INDEX session_map_nodes_session_idx ON public.session_map_nodes USING btree (session_id)""",
    """CREATE INDEX session_map_nodes_workspace_idx ON public.session_map_nodes USING btree (workspace_id)""",
    """CREATE INDEX session_map_workspaces_owner_idx ON public.session_map_workspaces USING btree (tenant_id, user_id)""",
    """CREATE UNIQUE INDEX subagent_run_steps_seq_idx ON public.subagent_run_steps USING btree (run_id, seq)""",
    """CREATE INDEX subagent_runs_parent_idx ON public.subagent_runs USING btree (parent_run_id)""",
    """CREATE INDEX subagent_runs_tree_idx ON public.subagent_runs USING btree (root_session_id, created_at)""",
    """CREATE INDEX subagent_runs_turn_idx ON public.subagent_runs USING btree (turn_id)""",
    """CREATE INDEX turns_session_created_idx ON public.turns USING btree (session_id, created_at)""",
    """CREATE UNIQUE INDEX ux_agent_runs_session_active ON public.agent_runs USING btree (session_id) WHERE ((status)::text = ANY (ARRAY[('running'::character varying)::text, ('checkpointed'::character varying)::text, ('resuming'::character varying)::text]))""",
    """CREATE UNIQUE INDEX ux_api_keys_key_hash_active ON public.api_keys USING btree (key_hash) WHERE (COALESCE(revoked, false) = false)""",
    """CREATE UNIQUE INDEX ux_knowledge_bases_index_ref ON public.knowledge_bases USING btree (provider, index_ref) WHERE (index_ref IS NOT NULL)""",
    """CREATE UNIQUE INDEX ux_knowledge_documents_pageindex ON public.knowledge_documents USING btree (knowledge_base_id, pageindex_doc_id) WHERE (pageindex_doc_id IS NOT NULL)""",
    """CREATE UNIQUE INDEX ux_market_installations_owner_item ON public.market_installations USING btree (tenant_id, user_id, source, kind, market_id)""",
    """CREATE UNIQUE INDEX ux_pending_interactions_open ON public.pending_interactions USING btree (session_id, tool_call_id) WHERE ((status)::text = ANY (ARRAY[('pending'::character varying)::text, ('answered'::character varying)::text]))""",
    """CREATE UNIQUE INDEX ux_project_checkpoints_session_seq_kind ON public.project_checkpoints USING btree (session_id, seq, kind)""",
    """CREATE UNIQUE INDEX ux_session_folds_range ON public.session_folds USING btree (session_id, covered_from, covered_to)""",
    """CREATE UNIQUE INDEX ux_smn_project_session ON public.session_map_nodes USING btree (session_id) WHERE (((edge_kind)::text = 'project'::text) AND (session_id IS NOT NULL) AND ((session_id)::text <> ''::text))""",
    """CREATE UNIQUE INDEX ux_smn_project_workspace_session ON public.session_map_nodes USING btree (workspace_id, session_id) WHERE (((edge_kind)::text = 'project'::text) AND (session_id IS NOT NULL))""",
    """CREATE UNIQUE INDEX ux_system_settings_category_key ON public.system_settings USING btree (category, key)""",
    """CREATE UNIQUE INDEX ux_users_email ON public.users USING btree (email)""",
)

_TABLES: tuple[str, ...] = (
    "admin_model_configs",
    "agent_runs",
    "agent_sessions",
    "agents",
    "api_keys",
    "audit_logs",
    "checkpoint_untracked",
    "conversation_shares",
    "knowledge_bases",
    "knowledge_documents",
    "llm_models",
    "llm_provider_keys",
    "market_installations",
    "media_assets",
    "memory_summaries",
    "message_archives",
    "messages",
    "model_prices",
    "pending_interactions",
    "project_checkpoints",
    "run_lineage_ledger",
    "session_folds",
    "session_map_nodes",
    "session_map_workspaces",
    "sessions",
    "subagent_run_steps",
    "subagent_runs",
    "superagent_settings",
    "superagent_takeovers",
    "system_settings",
    "task_idempotency",
    "tasks",
    "tenants",
    "tool_calls",
    "turns",
    "unified_messages",
    "unified_sessions",
    "uploads",
    "usage_events",
    "user_knowledge_settings",
    "user_memory_entries",
    "user_memory_profile",
    "user_onboarding",
    "users",
)

_SEQUENCES: tuple[str, ...] = (
    "subagent_run_steps_id_seq",
    "system_settings_id_seq",
    "tenants_id_seq",
    "unified_messages_id_seq",
    "usage_events_id_seq",
)


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)


def downgrade() -> None:
    """基线没有"上一个版本"可回退，只能把 schema 清空。

    CASCADE 是必须的：表之间有外键；序列先随表走，末尾再兜一次。
    """
    for name in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS public.{name} CASCADE")
    for name in _SEQUENCES:
        op.execute(f"DROP SEQUENCE IF EXISTS public.{name} CASCADE")
