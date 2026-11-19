package config

import (
	"bufio"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	AppSecret string

	// Server
	Port string
	// BindHost 是 HTTP 监听地址（空 = 监听全部网卡，即历史上 `":port"` 的行为）。
	//
	// 它是"谁能访问这台机器上的服务"的根开关：只填 `127.0.0.1` 就只有本机可达，
	// 填 `0.0.0.0` 或留空则局域网/公网可达。后台「服务」页改的就是它（见
	// internal/api/admin_service.go），但监听套接字绑在启动时，改完要重启网关。
	//
	// 注意：桌面壳（frontend-vue/electron/local-stack.cjs）只给网关设 `PORT`，
	// **从不设 `BIND_HOST`** —— 本地模式默认同样是全部网卡，别指望壳会兜回环。
	BindHost     string
	ReadTimeout  time.Duration
	WriteTimeout time.Duration
	IdleTimeout  time.Duration

	// Database
	PostgresDSN      string
	PostgresMaxConn  int
	PostgresMinConn  int
	PostgresReadDSNs []string // read-replica DSNs (comma-separated)

	// Redis
	RedisMode          string // "single", "cluster", "sentinel"
	RedisAddr          string
	RedisPassword      string
	RedisDB            int
	RedisAddrs         []string // for cluster mode
	RedisMasterName    string   // for sentinel mode
	RedisSentinelAddrs []string // for sentinel mode
	RedisPoolSize      int

	// DegradedMode 显式允许依赖降级（仅单机开发用）。默认 false：
	// 生产模式下 Redis 不可用直接拒绝启动——进程内降级会让副本看到不同的
	// 会话/限流/事件（限流按副本放大、run 锁退化为本地锁），破坏一致性。
	DegradedMode bool

	// AllowSchemaDrift 允许"数据库 schema 版本与代码期望不一致"时仍启动。
	// 默认 false：迁移由发布流程/DBA 执行（见 requirements-migrate.txt），
	// 启动只做只读校验，不一致即拒绝启动，避免"代码已升级、迁移未跑"的静默漂移。
	AllowSchemaDrift bool

	// LocalBootstrap 打开本地模式的免口令会话引导（POST /v1/auth/session）。
	// 默认 false：这个端点会**不校验任何口令就铸出一个 owner JWT**，只有在
	// "数据只在这台机器上、进程由本机用户自己启动"的桌面本地部署里才成立。
	// 桌面壳拉起网关时显式置 1（frontend-vue/electron/local-stack.cjs），
	// 云端/多人部署保持关闭即完全不会注册这条路由。
	// 处理链上还有第二道闸门：只认 loopback 来源，局域网请求一律 403
	//（局域网设备只能用口令走 POST /v1/auth/login）。
	LocalBootstrap bool

	// Auth
	JWTSecret     string
	JWTExpiration time.Duration
	InternalToken string

	// Registration
	DisableRegistration bool

	// EmailBloom 是注册流程里"邮箱是否已存在"判重的布隆过滤器（按租户分片）。
	// 它只做**快速否定**：命中"一定不存在"才跳过 DB 判重，真相源始终是
	// users(tenant_id, email) 唯一索引。默认开启 —— 它是纯性能优化，
	// 任何漏记都只会退化为回落 DB，不会放进重复邮箱。
	EmailBloomEnabled  bool
	EmailBloomCapacity uint64  // 每个租户分片的默认预期容量
	EmailBloomFPRate   float64 // 目标误报率

	// NotificationRetentionDays 站内通知的保留天数（默认 90）。
	//
	// 通知表只增不减：四类事件里 `quota_changed` 会按租户 owner/admin 数量
	// 成倍写入。90 天够覆盖"上季度那笔扣费怎么回事"这类对账追溯，又不会让表
	// 无限膨胀。设为 <= 0 时回落默认值（**不会**变成"立即删光"）。
	NotificationRetentionDays int

	// Cookie
	CookieSecure bool

	// CORS
	CORSOrigins string

	// Storage
	StorageBackend string // "local" or "s3"
	StorageRoot    string // local root path
	S3Endpoint     string
	S3Bucket       string
	S3AccessKey    string
	S3SecretKey    string
	S3UseSSL       bool // S3/MinIO use SSL

	// Rate Limit
	RateLimitRPM       int
	RateLimitFailClose bool
	RateLimitGlobal    int // global requests per minute
	// 注：三级令牌桶（global/tenant/user）均为"每分钟总量配额"，由 Redis 原子计数
	// 承载，与网关副本数无关 —— 历史上曾按副本数线性放大的 RATE_LIMIT_INSTANCES
	// 已移除（扩缩容无需调参，见 docs/deployment-multi-instance.md 第 5 节）。

	// TrustedProxyCIDRs trusted reverse-proxy CIDRs (comma separated).
	// X-Forwarded-For / X-Real-IP are only honored when the direct peer
	// matches one of these CIDRs; otherwise clients could spoof IP-based limits.
	TrustedProxyCIDRs []string

	// MetricsToken shared bearer token for Prometheus to scrape /metrics.
	// When empty, /metrics still requires JWT admin permission.
	MetricsToken string

	// Log
	LogLevel string // debug / info / warn / error

	PublicBaseURL         string
	FrontendURL           string
	AlipayAppID           string
	AlipayPrivateKey      string
	AlipayPublicKey       string
	AlipayGateway         string
	WechatMchID           string
	WechatAppID           string
	WechatAPIv3Key        string
	WechatMchCertSerialNo string
	WechatMchPrivateKey   string

	// Agent behavior
	//
	// ⚠ 这三个字段的命名在 M20 做过一次**破坏性改名**，起因是"同名不同义"：
	//   * `AGENT_MAX_TOKENS` 在网关是"单次调用的 max output tokens"，在**引擎**却是
	//     "一个 run 全程的 token 预算"（默认 400k）。共享 env 的部署按网关口径设 8192，
	//     引擎几乎每个 run 都以 `budget_exceeded:tokens` 收口，两侧还都没有校验；
	//   * `AGENT_MAX_TURNS` 同样是"网关的默认提交回合数"与"引擎的 run 回合上限"两回事。
	// 现在：单次/默认一类的裸名归网关，run 级的归引擎并带 `RUN_`（见 .env.example）。
	// 旧名一律**不再读取**，启动时由 `ValidateRenamedAgentEnv` 直接拒绝（失败要显式）。
	//
	// AgentDefaultMaxTurns 每次提交（一条 SSE 回合）的默认回合数；请求里的
	// `llm_config.max_turns` 优先（前端与 Agent 档案都走这条路）。
	AgentDefaultMaxTurns int
	// AgentMaxOutputTokens 单次 LLM 调用的 max output tokens。它不是 run 预算：
	// run 全程的 token 上限在**引擎**侧（`AGENT_RUN_MAX_TOKENS`，默认 400k）。
	AgentMaxOutputTokens int
	AgentMaxConcurrency  int // max concurrent agent runs (default 20)
	// AgentSubmitTimeout 单次提交（一条 SSE 回合）在网关侧的后台执行上限。
	// 必须 >= api.DefaultAgentTimeout（300s）：曾经硬编码 180s，比它短，
	// 长回合（多轮工具调用）必然被提前取消，表现为"思考/工具调用做一半就断"。
	AgentSubmitTimeout time.Duration
	// AgentGoalTimeout 是**整条续跑链**（Goal FSM 的所有轮次）在网关侧的总上限。
	//
	// 必须与 AgentSubmitTimeout 分成两个预算：后者只管**单轮**，而续跑链是
	// N 轮串行。此前两者共用同一个 ctx 预算（5 分钟管住整条链），于是第一轮
	// 用完时间后外层就 DeadlineExceeded，循环里的 `ctx.Err() != nil` 立刻退出 ——
	// 轮数上限（agentgoal.MaxRounds）根本没有机会被执行，表现就是"做一半就停"。
	AgentGoalTimeout time.Duration

	// Python AI 引擎
	PythonEngineAddress string // HTTP 地址，如 "localhost:8000"
	PythonEngineTimeout time.Duration

	// Temporal / LLMGateway 为遗留配置（未使用），已移除

	// PayPal
	PayPalClientID string
	PayPalSecret   string
	PayPalSandbox  bool

	// Plugins
	PluginsConfigPath string // path to plugins.json (MCP server config)
	PluginDataDir     string // per-user plugin config root: {PluginDataDir}/{user_id}/plugins.json

	// Market（资源市场：agents / skills / knowledge / mcp）
	//
	// 内置目录随二进制发布（internal/market/catalog 的 //go:embed），**不是**配置项：
	// 它必须永远可用，否则离线桌面版的市场页就是空的。
	//
	// 外部源全部默认关闭 —— 初装即"只吃内置目录"是发布形态的默认语义，
	// 打开外部源意味着本机开始访问第三方 URL，这必须由部署方或用户显式决定。
	MarketRemoteEnabled bool     // MARKET_REMOTE_ENABLED：总开关，默认 false
	MarketCatalogSources []string // MARKET_CATALOG_SOURCES：运维级源 URL（唯一信任入口）
	MarketAllowUserSources bool  // MARKET_ALLOW_USER_SOURCES：是否允许用户自行加源，默认 false
	MarketAllowRemoteMCP bool    // MARKET_ALLOW_REMOTE_MCP：外部源能否装 MCP，默认 false
	// MarketMaxSources 是源总数上限，默认 20。
	//
	// **>20 会被 internal/market 截到 20**：上限若可配大就等于没有上限，
	// 每个外部源都是一条能拖慢市场页的抓取链路。只允许往小配。
	MarketMaxSources int // MARKET_MAX_SOURCES：源总数上限，默认 20，>20 会被截到 20
	MarketCatalogTTL time.Duration // MARKET_CATALOG_TTL：目录缓存时长，默认 5m
	MarketFetchTimeout time.Duration // MARKET_FETCH_TIMEOUT：单次抓取超时，默认 8s
	// MarketTrustedKeys 是 key_id → base64 Ed25519 公钥（MARKET_TRUSTED_KEYS）。
	//
	// **签名不强制**：为空时外部条目一律标"未签名"并给出提醒，但仍可安装。
	// 配置了才具备验证能力 —— 把"签名"做成必填只会让发布方绕过它（自签自验），
	// 不如让用户看见"这条有没有来源背书"。
	MarketTrustedKeys map[string]string

	// DataDir is the runtime data directory (backups, skills, media, etc.)
	DataDir string
}

func Load() (*Config, error) {
	cfg := loadConfig()

	// APP_SECRET is required（部署级主密钥）。
	if !cfg.ValidateAppSecret() {
		return nil, errors.New("APP_SECRET environment variable must be set to a strong, unique value (32+ chars)")
	}

	if cfg.JWTSecret == "" {
		cfg.JWTSecret = deriveSubsecret(cfg.AppSecret, "chiron-jwt")
	}
	if cfg.InternalToken == "" {
		cfg.InternalToken = deriveSubsecret(cfg.AppSecret, "chiron-internal")
	}

	// JWT_SECRET is required (derived or explicit).
	if !ValidateJWTSecret(cfg.JWTSecret) {
		return nil, errors.New("JWT_SECRET (or its source APP_SECRET) must be set to a strong, unique value")
	}

	return cfg, nil
}

func LoadAllowUnconfigured() *Config {
	cfg := loadConfig()

	if cfg.JWTSecret == "" {
		cfg.JWTSecret = deriveSubsecret(cfg.AppSecret, "chiron-jwt")
	}
	if cfg.InternalToken == "" {
		cfg.InternalToken = deriveSubsecret(cfg.AppSecret, "chiron-internal")
	}
	return cfg
}

func loadConfig() *Config {
	loadDotEnv()     // .env file overrides config file
	loadConfigFile() // JSON config file (lowest priority)
	cfg := &Config{
		AppSecret:                 getSecret("APP_SECRET", ""),
		Port:                      getEnv("PORT", "8080"),
		BindHost:                  getEnv("BIND_HOST", ""),
		ReadTimeout:               getDuration("READ_TIMEOUT", 10*time.Second),
		WriteTimeout:              getDuration("WRITE_TIMEOUT", 60*time.Second),
		IdleTimeout:               getDuration("IDLE_TIMEOUT", 120*time.Second),
		PostgresDSN:               getEnv("POSTGRES_DSN", ""),
		PostgresMaxConn:           getIntAliases(20, "POSTGRES_MAX_CONN", "POSTGRES_MAX_CONNS"),
		PostgresMinConn:           getInt("POSTGRES_MIN_CONN", 2),
		PostgresReadDSNs:          getStringSlice("POSTGRES_READ_DSNS", []string{}),
		RedisMode:                 getEnv("REDIS_MODE", "single"),
		RedisAddr:                 getEnv("REDIS_ADDR", "localhost:6379"),
		RedisPassword:             getEnv("REDIS_PASSWORD", ""),
		RedisDB:                   getInt("REDIS_DB", 0),
		RedisAddrs:                getStringSlice("REDIS_ADDRS", []string{}),
		RedisMasterName:           getEnv("REDIS_MASTER_NAME", ""),
		RedisSentinelAddrs:        getStringSlice("REDIS_SENTINEL_ADDRS", []string{}),
		RedisPoolSize:             getInt("REDIS_POOL_SIZE", 100),
		JWTSecret:                 getSecret("JWT_SECRET", ""),
		JWTExpiration:             getDuration("JWT_EXPIRATION", 24*time.Hour),
		InternalToken:             getSecret("INTERNAL_TOKEN", ""),
		DisableRegistration:       isTruthy(getEnv("DISABLE_REGISTRATION", "")),
		EmailBloomEnabled:         isTruthy(getEnv("EMAIL_BLOOM_FILTER_ENABLED", "true")),
		EmailBloomCapacity:        uint64(getInt("EMAIL_BLOOM_CAPACITY", 100000)),
		NotificationRetentionDays: getInt("NOTIFICATION_RETENTION_DAYS", 90),
		EmailBloomFPRate:          getFloat("EMAIL_BLOOM_FP_RATE", 0.001),
		// Cookie Secure：显式配置优先；未配置时**按部署协议推导**（见 cookieSecure()）。
		CookieSecure:       cookieSecure(),
		CORSOrigins:        getEnv("CORS_ORIGINS", ""),
		StorageBackend:     getEnv("STORAGE_BACKEND", "local"),
		StorageRoot:        getEnv("STORAGE_ROOT", filepath.Join(GetDefaultDataDir(), "workspace")),
		S3Endpoint:         getEnv("S3_ENDPOINT", ""),
		S3Bucket:           getEnv("S3_BUCKET", "chiron"),
		S3AccessKey:        getSecret("S3_ACCESS_KEY", ""),
		S3SecretKey:        getSecret("S3_SECRET_KEY", ""),
		S3UseSSL:           isTruthy(getEnv("S3_USE_SSL", "")),
		RateLimitRPM:       getInt("RATE_LIMIT_RPM", 100),
		RateLimitFailClose: isTruthy(getEnv("RATE_LIMIT_FAIL_CLOSE", "")),
		RateLimitGlobal:    getInt("RATE_LIMIT_GLOBAL", 10000),
		TrustedProxyCIDRs:  getStringSlice("TRUSTED_PROXY_CIDRS", []string{}),
		MetricsToken:       getSecret("METRICS_TOKEN", ""),
		DegradedMode:       isTruthy(getEnv("DEGRADED_MODE", "")),
		AllowSchemaDrift:   isTruthy(getEnv("ALLOW_SCHEMA_DRIFT", "")),
		LocalBootstrap:     isTruthy(getEnv("LOCAL_BOOTSTRAP", "")),
		LogLevel:           getEnv("LOG_LEVEL", "info"),

		// 支付（支付宝/微信）
		PublicBaseURL:         getEnv("PUBLIC_BASE_URL", ""),
		FrontendURL:           getEnv("FRONTEND_URL", ""),
		AlipayAppID:           getEnv("ALIPAY_APP_ID", ""),
		AlipayPrivateKey:      getEnv("ALIPAY_PRIVATE_KEY", ""),
		AlipayPublicKey:       getEnv("ALIPAY_PUBLIC_KEY", ""),
		AlipayGateway:         getEnv("ALIPAY_GATEWAY", ""),
		WechatMchID:           getEnv("WXPAY_MCH_ID", ""),
		WechatAppID:           getEnv("WXPAY_APP_ID", ""),
		WechatAPIv3Key:        getEnv("WXPAY_API_V3_KEY", ""),
		WechatMchCertSerialNo: getEnv("WXPAY_MCH_CERT_SERIAL_NO", ""),
		WechatMchPrivateKey:   getEnv("WXPAY_MCH_PRIVATE_KEY", ""),
		// 与 api.DefaultMaxTurns 同值（40）。网关每次提交都显式下发它，所以这里才是
		// 实际生效的默认值；引擎侧 `settings.max_turns` 只是兜底。
		AgentDefaultMaxTurns: getInt("AGENT_DEFAULT_MAX_TURNS", 40),
		AgentMaxOutputTokens: getInt("AGENT_MAX_OUTPUT_TOKENS", 8192),
		AgentMaxConcurrency:  getInt("AGENT_MAX_CONCURRENCY", 20),
		// 默认 5 分钟：这是**单轮**上限（一次"读文件 → 改 → 跑测试"的预算），
		// 不是整条长任务的预算。轮次上限按**单个小目标**计 20 轮（见
		// agentgoal.DefaultGoalRounds），整条长任务由链级预算
		// AGENT_GOAL_TIMEOUT 兜底 —— 三层各管一层，互不吃掉。
		AgentSubmitTimeout: getDuration("AGENT_SUBMIT_TIMEOUT", 5*time.Minute),
		// 链级预算是**整条长任务**的墙钟兜底（不是轮数闸门）：单个小目标 20 轮
		// （agentgoal.DefaultGoalRounds）× 单轮 5 分钟 = 100 分钟，而整条任务允许
		// 200 轮（agentgoal.DefaultMaxTotalRounds）× 5 分钟 = 1000 分钟 ≈ 16.7 小时。
		// 默认 24 小时必须装得下这个理论上限，否则撞的会是墙钟而不是轮数闸门，
		// 用户看到的是"轮数还没用完就停了"。三层各管一层，见 internal/agentgoal。
		AgentGoalTimeout: getDuration("AGENT_GOAL_TIMEOUT", 24*time.Hour),

		PythonEngineAddress: getEnv("PYTHON_ENGINE_ADDRESS", "localhost:8000"),
		PythonEngineTimeout: getDuration("PYTHON_ENGINE_TIMEOUT", 5*time.Minute),

		PayPalClientID: getEnv("PAYPAL_CLIENT_ID", ""),
		PayPalSecret:   getEnv("PAYPAL_SECRET", ""),
		PayPalSandbox:  isTruthy(getEnv("PAYPAL_SANDBOX", "")),

		PluginsConfigPath: getEnv("PLUGINS_CONFIG_PATH", filepath.Join(GetDefaultDataDir(), "config", "plugins.json")),
		PluginDataDir:     getEnv("PLUGIN_DATA_DIR", filepath.Join(GetDefaultDataDir(), "plugins")),
		// 市场：外部源**默认全关**。初装只吃随包发布的内置目录，
		// 打开外部源必须显式配置 MARKET_REMOTE_ENABLED=1。
		MarketRemoteEnabled:   isTruthy(getEnv("MARKET_REMOTE_ENABLED", "")),
		MarketCatalogSources:  getStringSlice("MARKET_CATALOG_SOURCES", []string{}),
		MarketAllowUserSources: isTruthy(getEnv("MARKET_ALLOW_USER_SOURCES", "")),
		MarketAllowRemoteMCP:  isTruthy(getEnv("MARKET_ALLOW_REMOTE_MCP", "")),
		// 默认 20；填更大也会被 internal/market 截到 20（见字段注释）。
		MarketMaxSources:      getInt("MARKET_MAX_SOURCES", 20),
		MarketCatalogTTL:      getDuration("MARKET_CATALOG_TTL", 5*time.Minute),
		MarketFetchTimeout:    getDuration("MARKET_FETCH_TIMEOUT", 8*time.Second),
		MarketTrustedKeys:     getStringMap("MARKET_TRUSTED_KEYS"),
		DataDir:           getEnv("CHIRON_DATA_DIR", GetDefaultDataDir()),
	}

	return cfg
}

// GetDefaultDataDir returns the default data directory based on environment:
// - CHIRON_DATA_DIR env var (highest priority, set by caller)
// - ~/.chiron (local development)
// - data (fallback)
// This is the canonical implementation; do not duplicate elsewhere.
func GetDefaultDataDir() string {
	if v := os.Getenv("CHIRON_DATA_DIR"); v != "" {
		return v
	}
	// Try to detect production vs development
	home, err := os.UserHomeDir()
	if err == nil {
		return filepath.Join(home, ".chiron")
	}
	return "data"
}

func (c *Config) ValidateAppSecret() bool {
	return ValidateJWTSecret(c.AppSecret)
}

func deriveSubsecret(secret, domain string) string {
	if secret == "" {
		return ""
	}
	h := hmac.New(sha256.New, []byte(secret))
	h.Write([]byte(domain))
	return base64.RawURLEncoding.EncodeToString(h.Sum(nil))
}

// ValidateJWTSecret returns true if the secret is valid for production use.
func ValidateJWTSecret(secret string) bool {
	if secret == "" {
		return false
	}
	// Reject weak/known secrets
	weakSecrets := []string{
		"dev-secret-change-in-production",
		"dev-secret-change-in-production-12345678",
		"secret",
		"test-secret",
		"change-me",
		"changeme",
	}
	for _, ws := range weakSecrets {
		if secret == ws {
			return false
		}
	}
	// Require minimum length for security (at least 32 chars for strong encryption)
	if len(secret) < 32 {
		return false
	}
	return true
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// getSecret 读取**敏感**配置：优先 `${key}_FILE` 指向的**文件内容**，否则回退 `${key}` 环境变量。
//
// 为什么需要：企业部署的密钥通常不在 env 里，而是由 Vault Agent Injector / K8s Secret
// 卷挂载 / 云 Secrets Manager / CSI driver 以**文件**形式投递（避免密钥出现在 `env`、
// 进程列表或 `docker inspect` 里）。本函数是这类投递方式的通用桥接 —— 设
// `APP_SECRET_FILE=/vault/secrets/app_secret` 即可，无需在部署层把密钥展开成 env。
//
// 文件内容两端的空白（含尾部换行）会被裁掉；文件为空 / 不可读时回退到环境变量。
func getSecret(key, fallback string) string {
	if p := os.Getenv(key + "_FILE"); p != "" {
		if b, err := os.ReadFile(p); err == nil {
			if v := strings.TrimSpace(string(b)); v != "" {
				return v
			}
		}
	}
	return getEnv(key, fallback)
}

func getInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return fallback
}

func getFloat(key string, fallback float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return fallback
}

func getDuration(key string, fallback time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return fallback
}

func getStringSlice(key string, fallback []string) []string {
	if v := os.Getenv(key); v != "" {
		// Split by comma and trim whitespace
		parts := strings.Split(v, ",")
		result := make([]string, 0, len(parts))
		for _, p := range parts {
			p = strings.TrimSpace(p)
			if p != "" {
				result = append(result, p)
			}
		}
		if len(result) > 0 {
			return result
		}
	}
	return fallback
}

// getStringMap 解析 `a=v1,b=v2` 形态的配置项。
//
// 缺失或格式不良的项**被跳过**而不是让整项失效：`MARKET_TRUSTED_KEYS` 里
// 一个写错的行不应该让其它公钥一起消失（那会把所有已签名的源判成验签失败）。
func getStringMap(key string) map[string]string {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return nil
	}
	out := make(map[string]string, 4)
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		k, v, ok := strings.Cut(part, "=")
		if !ok {
			continue
		}
		k, v = strings.TrimSpace(k), strings.TrimSpace(v)
		if k == "" || v == "" {
			continue
		}
		out[k] = v
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// isTruthy returns true if s is "true", "1", "yes", or "on" (case-insensitive).
func isTruthy(s string) bool {
	switch s {
	case "true", "1", "yes", "on", "TRUE", "YES", "ON":
		return true
	}
	return false
}

// ListenAddr 返回 http.Server 用的监听地址（`host:port`）。
//
// BindHost 为空时退回 `":port"` —— 与历史行为逐字一致（监听全部网卡），
// 这样没配 BIND_HOST 的部署升级后不会突然只在回环上，把外部访问断掉。
func (c *Config) ListenAddr() string {
	host := strings.TrimSpace(c.BindHost)
	if host == "" {
		return ":" + c.Port
	}
	// IPv6 字面量必须带方括号，否则 `::1:8080` 会被 SplitHostPort 判成非法。
	if strings.Contains(host, ":") && !strings.HasPrefix(host, "[") {
		host = "[" + host + "]"
	}
	return host + ":" + c.Port
}

// cookieSecure 决定会话 cookie 是否带 Secure 标志。
//
// 取值顺序：
//  1. `COOKIE_SECURE` **显式设置**时以它为准（便于"反代终止 TLS、容器内仍是 HTTP"等拓扑）；
//  2. 否则按 `PUBLIC_BASE_URL` 的 scheme 推导：https ⇒ true。
//
// 为什么不沿用"默认 false"：生产漏配 COOKIE_SECURE 会让会话 cookie 在明文 HTTP 上
// 可被窃取（缺 Secure 时浏览器允许经 HTTP 发送）。生产必然设置 PUBLIC_BASE_URL
// （SSO 回调地址也依赖它），因此推导是可靠的；本地 HTTP 开发则推导为 false，
// 不会出现"带 Secure 的 cookie 在 http://localhost 下不被回传、登录不上"的问题。
func cookieSecure() bool {
	if v := strings.TrimSpace(getEnv("COOKIE_SECURE", "")); v != "" {
		return isTruthy(v)
	}
	return strings.HasPrefix(
		strings.ToLower(strings.TrimSpace(getEnv("PUBLIC_BASE_URL", ""))), "https://")
}

// loadDotEnv reads .env file and sets environment variables if not already set.
// findFileUpward searches for a file starting from the current directory
// and walking up to the filesystem root. Returns the first match.
func findFileUpward(name string) string {
	dir, _ := os.Getwd()
	for {
		candidate := filepath.Join(dir, name)
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break // reached filesystem root
		}
		dir = parent
	}
	return name // fall back to original relative path (will fail with useful error)
}

func loadDotEnv() {
	path := findFileUpward(".env")
	data, err := os.ReadFile(path)
	if err != nil {
		return // .env file not found, skip
	}
	scanner := bufio.NewScanner(strings.NewReader(string(data)))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.SplitN(line, "=", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.TrimSpace(parts[0])
		val := strings.TrimSpace(parts[1])
		// Strip quotes if present
		if len(val) >= 2 && ((val[0] == '"' && val[len(val)-1] == '"') || (val[0] == '\'' && val[len(val)-1] == '\'')) {
			val = val[1 : len(val)-1]
		}
		// Only set if not already set (env vars take precedence)
		if os.Getenv(key) == "" {
			os.Setenv(key, val)
		}
	}
}
