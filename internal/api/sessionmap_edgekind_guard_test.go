package api

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// 这一组测试**不连数据库**，只扫描本包的源码文本。
//
// 存在的理由：`session_map_nodes` 这张表现在同时存两类语义完全不同的记录 ——
// 画布卡片（`edge_kind` 为 'manual' 等）与项目归属（`edge_kind='project'`）。
// 二者靠一列区分，而漏写过滤**不会报错**，只会静默损坏数据：
//
//   - 保存画布时漏过滤 ⇒ `DELETE` 清掉归属 ⇒ 侧边栏项目瞬间变空；
//   - 读取画布时漏过滤 ⇒ 归属记录被当成 0,0 的幽灵卡片画出来。
//
// 这两种失败都不会让编译或测试变红，只会在用户那里表现为"数据丢了"。
// 代码审查靠不住（这已经在本项目里真实发生过一次），所以把判据钉成测试。
func TestSessionMapEdgeKindGuard(t *testing.T) {
	src := readSessionMapSources(t)

	// 判据一：任何对 session_map_nodes 的 DELETE 都必须带 edge_kind 过滤。
	//
	// 唯一的合法例外是删会话时的级联清理 —— 那种场景要删的是"这个会话的全部痕迹"，
	// 包括归属，因此按 session_id 删除本身就该全删。
	for _, stmt := range extractStatements(src, "DELETE FROM session_map_nodes") {
		if strings.Contains(stmt, "session_id") && !strings.Contains(stmt, "workspace_id") {
			// 按会话删除：级联语义，允许不带 edge_kind。
			continue
		}
		if !strings.Contains(stmt, "edge_kind") {
			t.Fatalf("对 session_map_nodes 的 DELETE 缺少 edge_kind 过滤，会误删项目归属：\n%s", stmt)
		}
	}

	// 判据二：画布读取路径必须排除归属记录。
	//
	// `smLoadFromPG` 里那条节点 SELECT 是画布的唯一数据来源，漏了过滤就会画出幽灵卡片。
	if !strings.Contains(src, "n.edge_kind <> 'project'") {
		t.Fatal("smLoadFromPG 读取画布节点时必须排除 edge_kind='project' 的归属记录")
	}
}

// TestSessionMapQuotaExcludesBindings 钉住配额口径。
//
// 配额（smMaxNodes）必须只数画布节点：归属记录和画布卡片同表，若一起计入，
// 用户在项目里归几百个会话就会挤爆画布额度 —— 那时画布"空着"却存不进去，
// 而错误信息说的是"节点太多"，完全无法排查。
func TestSessionMapQuotaExcludesBindings(t *testing.T) {
	src := readSessionMapSources(t)
	if !strings.Contains(src, "smEdgeKindProject") {
		t.Fatal("配额统计必须排除 smEdgeKindProject，否则项目归属会挤占画布额度")
	}
	// 防退化：不能再退回「直接数 len(body.Nodes)」的写法。
	if strings.Contains(src, "if len(body.Nodes) > smMaxNodes") {
		t.Fatal("配额不能直接数 len(body.Nodes)：这会连项目归属一起计入")
	}
}

// TestSessionMapProjectBindingPathIsSeparate 钉住「归属写入不经过画布端点」。
//
// `PUT /workspaces/{id}` 是**整体保存画布**，它的 DELETE 会重建节点的全部行。
// 若归属也走这条路径写，一次画布自动保存就可能把归属抹掉。
// 归属必须走独立端点（`/projects/{id}/sessions/{sid}`）。
func TestSessionMapProjectBindingPathIsSeparate(t *testing.T) {
	src := readSessionMapSources(t)
	if !strings.Contains(src, "smEdgeKindProject") {
		t.Fatal("归属边类型常量缺失")
	}
	// SaveWorkspace 必须拒收客户端提交的 root_path（否则 PUT 即可把项目指向 C:\\Windows）。
	if !strings.Contains(src, "body.RootPath = existing.RootPath") {
		t.Fatal("SaveWorkspace 必须沿用库里的 root_path，不能接受客户端提交的值")
	}
	if !strings.Contains(src, "body.SortIndex = existing.SortIndex") {
		t.Fatal("SaveWorkspace 必须沿用库里的 sort_index，不能接受客户端提交的值")
	}
}

// readSessionMapSources 读出本包与 session_map_nodes 相关的源码，拼成一段文本。
//
// 只拼需要的文件而不是整个包：包的其它部分与本测试无关，拼进来只会让
// 字符串匹配意外命中无关代码。测试自身的文件也不能拼进来 —— 它的断言消息里
// 就写着这些字面量，拼进来会让断言恒真。
func readSessionMapSources(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	var b strings.Builder
	for _, name := range []string{"sessionmap.go", "sessionmap_projects.go"} {
		data, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		b.Write(data)
		b.WriteString("\n")
	}
	return b.String()
}

// extractStatements 从源码里挑出每个 `DELETE FROM session_map_nodes` 所在的**单条 SQL**。
//
// 必须精确截到收尾反引号：Go 里的 SQL 是原始字符串字面量，跨到下一语句去
// 会把旁边的 INSERT（含 session_id）也读进来，于是"按会话级联删除"的例外被
// 误触发，漏过滤的 DELETE 反而通过 —— 这正是本测试第一版被变异验证抓出来的缺陷。
func extractStatements(src, head string) []string {
	var out []string
	for i := 0; ; {
		idx := strings.Index(src[i:], head)
		if idx < 0 {
			return out
		}
		start := i + idx
		// SQL 以反引号开启；从 head 之后找下一个反引号作为结束。
		tail := src[start:]
		if end := strings.Index(tail, "`"); end >= 0 {
			out = append(out, tail[:end])
		}
		i = start + len(head)
	}
}
