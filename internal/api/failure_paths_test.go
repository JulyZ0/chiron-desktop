package api

import (
	"go/ast"
	"go/parser"
	"go/token"
	"strings"
	"testing"
	"time"
)

// exprName 把 `db.GlobalDBManager.Exec` 这样的调用目标还原成点分全名。
// 只看末段（`Exec`）不足以区分"事务内的 tx.Exec"和"绕过事务的 DBManager.Exec"。
func exprName(e ast.Expr) string {
	switch x := e.(type) {
	case *ast.Ident:
		return x.Name
	case *ast.SelectorExpr:
		if p := exprName(x.X); p != "" {
			return p + "." + x.Sel.Name
		}
		return x.Sel.Name
	case *ast.CallExpr:
		return exprName(x.Fun)
	case *ast.IndexExpr:
		return exprName(x.X)
	case *ast.ParenExpr:
		return exprName(x.X)
	}
	return ""
}

// recordCall 同时登记点分全名与末段名：调用方按精度自选。
func recordCall(into map[string]bool, fun ast.Expr) {
	if full := exprName(fun); full != "" {
		into[full] = true
		if i := strings.LastIndex(full, "."); i >= 0 {
			into[full[i+1:]] = true
		}
	}
}

// ── P1 失败路径归还回归 ──
//
// 这一组盯的是"任务已经发生、但记账/归还那一步在失败路径上被跳过"的两处：
//
//  1. sessionmap：`SPOP` 认领后进程被杀，画布永久不再落库；落库非事务则可能只写一半节点；
//  2. audit：读过的消息不 ACK 就没人再读，永久滞留在 PEL。
//
// cron 租约一组随 cron_scheduler.go 一并删除（本地形态不再有定时任务）。
//
// 断言方式用 **AST** 而不是字符串匹配：这类回归最典型的写法就是"把调用注释掉"
// 或"挪进一个不会执行的 if 里"，字符串匹配对前者无效（Python 侧 tests/source_scan.py
// 记录过两次实测踩坑）。这里检查的是真实的 CallExpr。

// funcCallsIn 返回某个文件里指定函数体内出现过的被调用名（`x.Y()` 取 "Y"，`f()` 取 "f"）。
func funcCallsIn(t *testing.T, path, funcName string) map[string]bool {
	t.Helper()
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		t.Fatalf("解析 %s 失败: %v", path, err)
	}
	calls := map[string]bool{}
	found := false
	ast.Inspect(file, func(n ast.Node) bool {
		fn, ok := n.(*ast.FuncDecl)
		if !ok || fn.Name == nil || fn.Name.Name != funcName || fn.Body == nil {
			return true
		}
		found = true
		ast.Inspect(fn.Body, func(inner ast.Node) bool {
			if call, ok := inner.(*ast.CallExpr); ok {
				recordCall(calls, call.Fun)
			}
			return true
		})
		return false
	})
	if !found {
		t.Fatalf("%s 里找不到函数 %s —— 测试本身需要更新", path, funcName)
	}
	return calls
}

func TestSessionMapFlushClaimsRecoverably(t *testing.T) {
	// 认领脚本必须"先回收过期项、再领取、领取即登记 inflight"三步齐备。
	for _, want := range []string{"ZRANGEBYSCORE", "SADD", "ZREM", "SPOP", "ZADD"} {
		if !strings.Contains(smClaimLua, want) {
			t.Errorf("smClaimLua 缺少 %q —— 认领要么不可回收，要么会重复写同一张画布：\n%s",
				want, smClaimLua)
		}
	}

	calls := funcCallsIn(t, "sessionmap.go", "smFlushOnce")
	for _, want := range []string{"smClaimDirty", "smReleaseClaim", "smCacheGetChecked"} {
		if !calls[want] {
			t.Errorf("smFlushOnce 没有调用 %s：SPOP 之后崩溃的认领无法恢复", want)
		}
	}

	// 读热层失败 ≠ 画布不存在：flush 必须用可区分错误的版本。
	if calls["smCacheGet"] && !calls["smCacheGetChecked"] {
		t.Fatal("smFlushOnce 用了不区分错误的 smCacheGet：Redis 抖动会被当成'画布不存在'而丢弃写入")
	}
}

func TestSessionMapPersistIsTransactional(t *testing.T) {
	calls := funcCallsIn(t, "sessionmap.go", "smPersist")
	if !calls["db.GlobalDBManager.WithTransaction"] && !calls["WithTransaction"] {
		t.Fatal("smPersist 没有整体事务：DELETE 成功而 INSERT 中途失败会留下缺了一半节点的画布")
	}
	// 必须是事务里的 tx.Exec。裸 DBManager.Exec 会各自提交，正是本次要修的缺陷
	// ——所以这里按**全名**判断，而不是末段的 "Exec"（tx.Exec 也会命中末段）。
	if calls["db.GlobalDBManager.Exec"] {
		t.Fatal("smPersist 仍在用非事务的 db.GlobalDBManager.Exec 写节点：先删后插必须同一事务")
	}
	if !calls["tx.Exec"] {
		t.Fatal("smPersist 事务里没有用 tx.Exec 写节点")
	}
}

func TestSessionMapReleaseIsAtomic(t *testing.T) {
	// 归还 = "ZREM inflight" + "放回 dirty（需要重试时）"。这两步必须原子：
	// 分两条命令时，进程在中间被杀就会两边都没有 —— 这张画布的布局永久丢失，
	// 且不在崩溃回收范围里，事后无从查出是哪一张。
	if !strings.Contains(smReleaseLua, "ZREM") {
		t.Fatalf("smReleaseLua 没有清 inflight：认领会一直卡到 stale 阈值：\n%s", smReleaseLua)
	}
	if !strings.Contains(smReleaseLua, "SADD") {
		t.Fatalf("smReleaseLua 无法放回 dirty：落库失败的画布不会重试：\n%s", smReleaseLua)
	}

	// 必须真的走脚本。裸 Do("ZREM"/"SADD") 就是本缺陷的原始形态。
	// 也必须是 Eval：Do("EVAL", numkeys, …) 的 numkeys 与键参数错位是静默失败
	// （脚本照跑，只是 KEYS 变成 ARGV），而 Eval 把键列表与参数分开了。
	calls := funcCallsIn(t, "sessionmap.go", "smReleaseClaim")
	if !calls["Eval"] {
		t.Fatal("smReleaseClaim 没有用 Eval 执行 smReleaseLua：ZREM 与 SADD 会分成两条命令")
	}
	if calls["Do"] {
		t.Fatal("smReleaseClaim 仍在用各自的 Do 发命令：归还必须是单个脚本")
	}
}

// ── P1 常驻循环的 panic 隔离 ──
//
// 这一组盯的是"一次 panic 打崩整个进程"，而不是三处归还逻辑本身。
//
// Go 与多数运行时的直觉相反：goroutine 里**未捕获**的 panic 会终止整个进程，
// 不会只终止那个 goroutine。网关的这几个循环全是进程级常驻任务，且处理的都是
// 不可信输入（用户写的 cron 表达式/task JSON、前端传来的画布布局、引擎返回）：
// 一次 nil 解引用就足以让**所有副本**同时退出，触发条件若稳定存在就是 CrashLoop。

func TestRunBackgroundCycleRecoversPanic(t *testing.T) {
	// 行为断言：panic 被拦在单轮内，函数正常返回；若没有 recover，
	// 这个 goroutine 会带着整个测试进程一起死。
	done := make(chan struct{})
	go func() {
		defer close(done)
		runBackgroundCycle("test-cycle", func() { panic("boom") })
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("runBackgroundCycle 没有从 panic 中恢复")
	}

	// 正常路径不能被改变：func 必须恰好执行一次。
	n := 0
	runBackgroundCycle("test-cycle", func() { n++ })
	if n != 1 {
		t.Fatalf("runBackgroundCycle 应恰好执行一次，实际 %d 次", n)
	}
}

func TestSessionMapLoopsSurvivePanic(t *testing.T) {
	// 两个循环都在"处理他人的数据"：flush 落库前端传来的布局，reconcile 扫全库。
	for _, fn := range []string{"StartSessionMapFlusher", "StartSessionMapReconciler"} {
		calls := funcCallsIn(t, "sessionmap.go", fn)
		if !calls["runBackgroundCycle"] {
			t.Errorf("%s 未用 runBackgroundCycle 兜 panic：一条坏画布数据会打崩整个网关进程", fn)
		}
	}
}
