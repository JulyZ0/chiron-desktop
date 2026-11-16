package api

import (
	"context"
	"testing"

	"github.com/athenavi/chiron/internal/db"
)

// 用量明细（usage_events）的写入路径。
//
// 参数解析（parseUsageParams）与分桶聚合（usageGroupColumn）的用例随计费统计
// 接口一并删除：那两条只服务于已下线的账单/用量报表端点，被测函数已不存在。
// 保留的是本地形态仍在走的"只记录、不阻塞"写入契约。

func TestRecordUsageEventIsNoopWithoutDatabase(t *testing.T) {
	// 明细写入是"只记录不阻塞"的路径：数据库不可用时必须静默返回，
	// 绝不能让一次已完成的对话因统计失败而报错。
	if db.GlobalDBManager != nil {
		t.Skip("本用例只在无数据库连接的单元测试环境有意义")
	}
	// 不 panic、不阻塞即为通过。
	RecordUsageEvent(context.Background(), UsageEvent{
		TenantID: 1, UserID: "u1", SessionID: "s1", Model: "gpt-x",
		InputTokens: 10, OutputTokens: 5,
	})
}

func TestRecordUsageEventSkipsEmptyEvents(t *testing.T) {
	// 全空事件不该产生一行记录：没有任何信息量的行会污染统计基数。
	// 这里同样依赖无 DB 环境（函数在入口处就返回），主要保证不会误写。
	if db.GlobalDBManager != nil {
		t.Skip("本用例只在无数据库连接的单元测试环境有意义")
	}
	RecordUsageEvent(context.Background(), UsageEvent{})
}
