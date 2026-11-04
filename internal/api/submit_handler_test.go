package api

import "testing"

// 回归保护：历史上下文回传前必须剥离 [thinking] 思考块（引擎按 80 字分段下发），
// 而落库内容保留标签供前端 splitThinking(loose) 还原思考块。
func TestStripThinkingBlocks(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"无思考块原样返回", "普通正文", "普通正文"},
		{"单段思考块+正文", "[thinking]想一下[/thinking]正文", "正文"},
		{"多段思考块+正文", "[thinking]a[/thinking][thinking]b[/thinking]正文", "正文"},
		{"纯思考轮剥离后为空", "[thinking]a[/thinking]", ""},
		{"残留孤立闭合标签一并剥离", "[thinking]a[/thinking]正文[/thinking]", "正文"},
		{"正文中的方括号不受影响", "结果是 [1] 与 [2]", "结果是 [1] 与 [2]"},
	}
	for _, c := range cases {
		if got := stripThinkingBlocks(c.in); got != c.want {
			t.Errorf("%s: stripThinkingBlocks(%q) = %q, want %q", c.name, c.in, got, c.want)
		}
	}
}
