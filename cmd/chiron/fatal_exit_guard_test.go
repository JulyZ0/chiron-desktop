package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"testing"
)

// ── H12：关键配置出错必须**非零退出** ────────────────────────────────────────
//
// 修复前的事实：`main` 里 8 处致命错误都写成 `slog.Error("FATAL: …"); return`
// —— `main` 正常返回就是退出码 0。于是"配置非法、进程没起来"在 systemd / docker /
// k8s / 打包器眼里是**成功退出**：重启策略不触发、监控看不到崩溃，只剩"服务没响应"。
//
// 现在统一走 `fatalExit`（记录 + os.Exit(1)）。这条静态检查守住"别再写回去"：
// 任何 `FATAL:` 日志之后，要么调用 `fatalExit`，要么显式 `os.Exit(非零)`。
func TestFatalConfigErrorsDoNotReturnZero(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "main.go", nil, parser.ParseComments)
	if err != nil {
		t.Fatalf("解析 main.go 失败: %v", err)
	}

	// 收集"打了 FATAL 日志却只是 return"的位置。
	var offenders []string

	ast.Inspect(file, func(n ast.Node) bool {
		block, ok := n.(*ast.BlockStmt)
		if !ok {
			return true
		}
		for i, stmt := range block.List {
			if !isSlogErrorFatalCall(stmt) {
				continue
			}
			// 看后续语句：允许注释（不在 AST 里），只检查实际语句。
			fatal := false
			for _, next := range block.List[i+1:] {
				if isIdentCall(next, "fatalExit") {
					fatal = true
					break
				}
				if isOsExitNonZero(next) {
					fatal = true
					break
				}
				if isReturn(next) {
					break // 先 return 了：没退出
				}
			}
			if !fatal {
				// 同一语句块里没有 fatalExit / os.Exit → 违规。
				if hasReturnAfter(block.List[i+1:]) {
					offenders = append(offenders, fset.Position(stmt.Pos()).String())
				}
			}
		}
		return true
	})

	if len(offenders) > 0 {
		t.Fatalf("以下位置记录 FATAL 后仍然 `return`（退出码 0）：%v\n请改用 fatalExit(...)。", offenders)
	}
}

func isSlogErrorFatalCall(stmt ast.Stmt) bool {
	call := callOf(stmt)
	if call == nil {
		return false
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != "Error" {
		return false
	}
	pkg, ok := sel.X.(*ast.Ident)
	if !ok || pkg.Name != "slog" {
		return false
	}
	if len(call.Args) == 0 {
		return false
	}
	lit, ok := call.Args[0].(*ast.BasicLit)
	return ok && lit.Kind == token.STRING && containsFATAL(lit.Value)
}

func containsFATAL(s string) bool {
	for i := 0; i+5 <= len(s); i++ {
		if s[i:i+5] == "FATAL" {
			return true
		}
	}
	return false
}

func isIdentCall(stmt ast.Stmt, name string) bool {
	call := callOf(stmt)
	if call == nil {
		return false
	}
	ident, ok := call.Fun.(*ast.Ident)
	return ok && ident.Name == name
}

func isOsExitNonZero(stmt ast.Stmt) bool {
	call := callOf(stmt)
	if call == nil {
		return false
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != "Exit" {
		return false
	}
	if pkg, ok := sel.X.(*ast.Ident); !ok || pkg.Name != "os" {
		return false
	}
	if len(call.Args) != 1 {
		return true // 形态不明：交给人工看，不在此处判死
	}
	lit, ok := call.Args[0].(*ast.BasicLit)
	if !ok {
		return true
	}
	return lit.Value != "0"
}

func isReturn(stmt ast.Stmt) bool {
	_, ok := stmt.(*ast.ReturnStmt)
	return ok
}

func hasReturnAfter(stmts []ast.Stmt) bool {
	for _, s := range stmts {
		if isReturn(s) {
			return true
		}
	}
	return false
}

func callOf(stmt ast.Stmt) *ast.CallExpr {
	expr, ok := stmt.(*ast.ExprStmt)
	if !ok {
		return nil
	}
	call, ok := expr.X.(*ast.CallExpr)
	return call
}
