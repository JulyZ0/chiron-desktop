package api

import (
	"strings"
	"testing"
	"time"
)

// ── validateTakeover：硬错误（只允许 422）──
//
// 这组断言守的是"存进去的配置必须真的能跑"。放行一条 interval=60s 的配置，
// 巡检循环每一拍都会判到点，结果是每分钟起一次 run —— 用户设错了数字，
// 代价是他的 token 账单和一台被占满的引擎。
//
// 时间基准用固定时刻而不是 time.Now()：`once_at` 的合法性依赖"相对于现在"，
// 用真实时钟写断言会在某些时刻飘（比如 `+24h` 恰好卡在边界的那一秒）。

func fixedNow() time.Time {
	return time.Date(2026, 11, 5, 10, 0, 0, 0, time.UTC)
}

func baseOnce(at time.Time) SuperagentTakeover {
	return SuperagentTakeover{
		SessionID:        "s-1",
		ScheduleKind:     superagentKindOnce,
		OnceAt:           &at,
		DailyTokenBudget: 100_000,
	}
}

func baseInterval(secs int) SuperagentTakeover {
	return SuperagentTakeover{
		SessionID:        "s-1",
		ScheduleKind:     superagentKindInterval,
		IntervalSeconds:  secs,
		DailyTokenBudget: 100_000,
	}
}

func TestValidateTakeover_ValidInterval(t *testing.T) {
	if errs := validateTakeover(baseInterval(1800), fixedNow()); len(errs) != 0 {
		t.Fatalf("want no errors, got %v", errs)
	}
}

// 15 分钟是硬下限。正好等于下限必须放行 —— 边界上「>=」写成「>」会让
// 用户按提示设成 900 却被拒，而他没有任何线索知道该怎么改。
func TestValidateTakeover_IntervalAtMinimumIsAllowed(t *testing.T) {
	if errs := validateTakeover(baseInterval(superagentMinInterval), fixedNow()); len(errs) != 0 {
		t.Fatalf("interval == %d must be accepted, got %v", superagentMinInterval, errs)
	}
}

func TestValidateTakeover_IntervalTooShort(t *testing.T) {
	errs := validateTakeover(baseInterval(superagentMinInterval-1), fixedNow())
	if len(errs) == 0 {
		t.Fatal("interval below the minimum must be rejected")
	}
	if !strings.Contains(errs[0], "900") {
		t.Errorf("error should name the 900s floor, got %q", errs[0])
	}
}

func TestValidateTakeover_SessionIDRequired(t *testing.T) {
	tk := baseInterval(1800)
	tk.SessionID = "   "
	errs := validateTakeover(tk, fixedNow())
	if len(errs) == 0 {
		t.Fatal("blank session_id must be rejected")
	}
}

func TestValidateTakeover_UnknownScheduleKind(t *testing.T) {
	tk := baseInterval(1800)
	tk.ScheduleKind = "cron"
	errs := validateTakeover(tk, fixedNow())
	if len(errs) == 0 {
		t.Fatal("unknown schedule_kind must be rejected")
	}
}

func TestValidateTakeover_OnceNeedsTimestamp(t *testing.T) {
	tk := baseInterval(1800)
	tk.ScheduleKind = superagentKindOnce
	tk.OnceAt = nil
	errs := validateTakeover(tk, fixedNow())
	if len(errs) == 0 {
		t.Fatal("schedule_kind=once without once_at must be rejected")
	}
}

func TestValidateTakeover_OnceInPast(t *testing.T) {
	errs := validateTakeover(baseOnce(fixedNow().Add(-time.Second)), fixedNow())
	if len(errs) == 0 {
		t.Fatal("once_at in the past must be rejected")
	}
}

func TestValidateTakeover_OnceExactlyNowIsRejected(t *testing.T) {
	// "不晚于现在" 一律拒绝：那一拍已经过去了，存进去只会立刻被判 NOT_DUE 或
	// 反复触发，两种都不是用户想要的。
	errs := validateTakeover(baseOnce(fixedNow()), fixedNow())
	if len(errs) == 0 {
		t.Fatal("once_at == now must be rejected")
	}
}

func TestValidateTakeover_OnceBeyondHorizon(t *testing.T) {
	errs := validateTakeover(baseOnce(fixedNow().Add(superagentMaxOnceHorizon+time.Minute)), fixedNow())
	if len(errs) == 0 {
		t.Fatal("once_at beyond 24h must be rejected")
	}
}

// 正好 24 小时必须放行（边界属于允许范围）。
func TestValidateTakeover_OnceAtHorizonIsAllowed(t *testing.T) {
	if errs := validateTakeover(baseOnce(fixedNow().Add(superagentMaxOnceHorizon)), fixedNow()); len(errs) != 0 {
		t.Fatalf("once_at == now+24h must be accepted, got %v", errs)
	}
}

func TestValidateTakeover_BudgetMustBePositive(t *testing.T) {
	for _, budget := range []int{0, -1} {
		tk := baseInterval(1800)
		tk.DailyTokenBudget = budget
		if len(validateTakeover(tk, fixedNow())) == 0 {
			t.Errorf("budget %d must be rejected", budget)
		}
	}
}

// 多条错误要全部返回：只回第一条会逼用户改一次提交一次。
func TestValidateTakeover_ReportsAllErrors(t *testing.T) {
	tk := SuperagentTakeover{
		SessionID:        "",
		ScheduleKind:     "cron",
		DailyTokenBudget: 0,
	}
	errs := validateTakeover(tk, fixedNow())
	if len(errs) < 3 {
		t.Fatalf("want >= 3 errors (session/kind/budget), got %d: %v", len(errs), errs)
	}
}

// ── takeoverWarnings：软提示（保存成功但要提醒）──

func TestTakeoverWarnings_YoloDowngradeIsExplained(t *testing.T) {
	tk := baseInterval(1800)
	tk.ToolsMode = superagentUnsafeMode
	warns := takeoverWarnings(tk, nil)

	if len(warns) == 0 {
		t.Fatal("yolo must produce a warning")
	}
	// 提示必须说清"会发生什么"（会挂起等你输入），不能只说"不建议"。
	joined := strings.Join(warns, " ")
	if !strings.Contains(joined, supersafeModeName()) {
		t.Errorf("warning should name the fallback mode %q, got %q", supersafeModeName(), joined)
	}
	if !strings.Contains(strings.ToLower(joined), "suspend") {
		t.Errorf("warning should say tools will suspend for input, got %q", joined)
	}
}

func supersafeModeName() string { return superagentSafeMode }

func TestTakeoverWarnings_SafeModeIsQuiet(t *testing.T) {
	tk := baseInterval(1800)
	tk.ToolsMode = superagentSafeMode
	if warns := takeoverWarnings(tk, nil); len(warns) != 0 {
		t.Errorf("auto mode should not warn, got %v", warns)
	}
}

// 同一时刻（5 分钟内）的多个 once 会争抢引擎并发槽，排队那个看起来像"没跑"。
func TestTakeoverWarnings_NearbyOnceContention(t *testing.T) {
	at := fixedNow().Add(3 * time.Hour)
	tk := baseOnce(at)
	tk.ID = "tk-1"
	tk.Enabled = true
	tk.DailyTokenBudget = 10_000

	otherAt := at.Add(4 * time.Minute)
	sibling := SuperagentTakeover{
		ID: "tk-2", SessionID: "s-2", Enabled: true,
		ScheduleKind: superagentKindOnce, OnceAt: &otherAt,
		DailyTokenBudget: 10_000,
	}

	warns := takeoverWarnings(tk, []SuperagentTakeover{sibling})
	if len(warns) == 0 {
		t.Fatal("once schedules 4 minutes apart should warn about contention")
	}
	joined := strings.Join(warns, " ")
	if !strings.Contains(joined, "s-2") {
		t.Errorf("warning should name the other session, got %q", joined)
	}
}

func TestTakeoverWarnings_FarApartOnceIsQuiet(t *testing.T) {
	at := fixedNow().Add(3 * time.Hour)
	tk := baseOnce(at)
	tk.ID = "tk-1"
	tk.Enabled = true
	tk.DailyTokenBudget = 10_000

	otherAt := at.Add(2 * time.Hour)
	sibling := SuperagentTakeover{
		ID: "tk-2", SessionID: "s-2", Enabled: true,
		ScheduleKind: superagentKindOnce, OnceAt: &otherAt,
		DailyTokenBudget: 10_000,
	}

	if warns := takeoverWarnings(tk, []SuperagentTakeover{sibling}); len(warns) != 0 {
		t.Errorf("schedules 2h apart should not warn, got %v", warns)
	}
}

// 关掉的兄弟行不参与冲突判断：用户把它关掉就是为了"暂时别撞"。
func TestTakeoverWarnings_DisabledSiblingIgnored(t *testing.T) {
	at := time.Now().Add(3 * time.Hour)
	tk := baseOnce(at)
	tk.ID = "tk-1"
	tk.Enabled = true
	tk.DailyTokenBudget = 10_000

	otherAt := at.Add(time.Minute)
	sibling := SuperagentTakeover{
		ID: "tk-2", SessionID: "s-2", Enabled: false,
		ScheduleKind: superagentKindOnce, OnceAt: &otherAt,
		DailyTokenBudget: 10_000,
	}

	if warns := takeoverWarnings(tk, []SuperagentTakeover{sibling}); len(warns) != 0 {
		t.Errorf("disabled sibling should be ignored, got %v", warns)
	}
}

func TestTakeoverWarnings_HighBudget(t *testing.T) {
	tk := baseInterval(1800)
	tk.Enabled = true
	tk.DailyTokenBudget = 500_000
	if warns := takeoverWarnings(tk, nil); len(warns) == 0 {
		t.Error("a 500k per-session budget should warn")
	}
}

func TestTakeoverWarnings_DisabledHighBudgetIsQuiet(t *testing.T) {
	// 关着的配置不花钱，不必提醒。
	tk := baseInterval(1800)
	tk.Enabled = false
	tk.DailyTokenBudget = 500_000
	if warns := takeoverWarnings(tk, nil); len(warns) != 0 {
		t.Errorf("a disabled takeover should not warn about budget, got %v", warns)
	}
}

// ── countEnabled：上限按启用数算，排除自己 ──

func TestCountEnabled(t *testing.T) {
	items := []SuperagentTakeover{
		{ID: "a", Enabled: true},
		{ID: "b", Enabled: true},
		{ID: "c", Enabled: false},
	}
	if got := countEnabled(items, ""); got != 2 {
		t.Errorf("want 2 enabled, got %d", got)
	}
	// 更新自己时不能把自己算进去，否则"已启用的那条一改就超限"。
	if got := countEnabled(items, "a"); got != 1 {
		t.Errorf("excluding self: want 1, got %d", got)
	}
}

// ── withTakeoverDefaults ──

func TestWithTakeoverDefaults_FillsInterval(t *testing.T) {
	tk := withTakeoverDefaults(SuperagentTakeover{})
	if tk.ScheduleKind != superagentKindInterval {
		t.Errorf("default kind: want %q, got %q", superagentKindInterval, tk.ScheduleKind)
	}
	if tk.IntervalSeconds != 1800 {
		t.Errorf("default interval: want 1800, got %d", tk.IntervalSeconds)
	}
	if tk.ToolsMode != superagentSafeMode {
		t.Errorf("default tools_mode: want %q, got %q", superagentSafeMode, tk.ToolsMode)
	}
	if tk.DailyTokenBudget <= 0 {
		t.Errorf("default budget must be positive, got %d", tk.DailyTokenBudget)
	}
}

// once 模式下不该被塞进一个 interval：那会让同一条记录同时有两种定时语义，
// 引擎按 kind 分支时另一份数据是垃圾。
func TestWithTakeoverDefaults_OnceKeepsZeroInterval(t *testing.T) {
	tk := withTakeoverDefaults(SuperagentTakeover{ScheduleKind: superagentKindOnce})
	if tk.IntervalSeconds != 0 {
		t.Errorf("once mode should keep interval 0, got %d", tk.IntervalSeconds)
	}
}

// 用户显式设的值不能被默认值覆盖。
func TestWithTakeoverDefaults_KeepsExplicitValues(t *testing.T) {
	tk := withTakeoverDefaults(SuperagentTakeover{
		ScheduleKind:     superagentKindInterval,
		IntervalSeconds:  900,
		ToolsMode:        "ask",
		DailyTokenBudget: 50_000,
	})
	if tk.IntervalSeconds != 900 || tk.ToolsMode != "ask" || tk.DailyTokenBudget != 50_000 {
		t.Errorf("explicit values were overwritten: %+v", tk)
	}
}

// ── 与引擎侧常量的一致性 ──
//
// 这些数字在两处硬编码（Go 校验、Python 判定）。不一致会导致"网关放行、引擎拒绝"，
// 而这种错误只在运行时暴露 —— 用户保存成功，功能却不触发。

func TestConstantsMatchEngine(t *testing.T) {
	if superagentMaxSessions != 5 {
		t.Errorf("max sessions: Go=%d, engine=5", superagentMaxSessions)
	}
	if superagentMinInterval != 15*60 {
		t.Errorf("min interval: Go=%d, engine=900", superagentMinInterval)
	}
	if superagentMaxOnceHorizon != 24*time.Hour {
		t.Errorf("once horizon: Go=%v, engine=24h", superagentMaxOnceHorizon)
	}
	if superagentKindOnce != "once" || superagentKindInterval != "interval" {
		t.Errorf("kind names drifted from engine: %q/%q", superagentKindOnce, superagentKindInterval)
	}
	if superagentUnsafeMode != "yolo" || superagentSafeMode != "auto" {
		t.Errorf("tools mode names drifted from engine: %q/%q", superagentUnsafeMode, superagentSafeMode)
	}
}
