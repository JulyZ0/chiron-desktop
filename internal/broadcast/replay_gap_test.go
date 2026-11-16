package broadcast

import "testing"

// replayIsComplete 是「断线缺口有没有补全」这一对外承诺的**全部判据**。
//
// 语义精确定义：完整 ⇔ 流里仍留着基线 `after` **之后**的全部事件。
//   * after > oldest ⇒ 成立（缓冲起点比基线还老，中间没被裁）
//   * after == oldest ⇒ 成立（客户端最后收到的那条自己还在流里）
//   * after < oldest ⇒ 不成立（after 到 oldest 之间那段已被 MaxLen 驱逐）
//
// 为什么单独钉它：它依赖的 Redis 边界（MaxLen 驱逐、TTL 整条过期）在单测里造不出来，
// 而判据写错的方向只有一个 —— 把"补不齐"误判成"补齐了"。那正是让界面停在断线前
// 那一刻的原因：用户回来时以为任务还在跑，或者以为它从没跑过。
func TestReplayIsCompleteNoBaselineMeansNoGap(t *testing.T) {
	// 新连接没有基线：谈不上缺口，不该给客户端发 `stream_gap`
	if !replayIsComplete("", 0, false, "") {
		t.Fatal("空基线必须视为无缺口（新连接不该收到 stream_gap）")
	}
}

func TestReplayIsCompleteWhenBaselineIsNewerThanEvictionPoint(t *testing.T) {
	// 基线晚于缓冲起点：它之后的事件全在缓冲里，补齐是完整的
	if !replayIsComplete("200-0", 3, true, "100-0") {
		t.Fatal("基线晚于缓冲起点时，应判定为已补齐")
	}
}

func TestReplayIsCompleteWhenBaselineEqualsOldest(t *testing.T) {
	// 基线恰好是缓冲里最老的一条：它自己还在流里，之后的事件也都在
	if !replayIsComplete("100-0", 3, true, "100-0") {
		t.Fatal("基线等于缓冲起点时，缺口未发生，应判定为已补齐")
	}
}

func TestReplayIsCompleteWhenBaselineEvicted(t *testing.T) {
	// 基线早于缓冲起点：after→oldest 之间那段已被 MaxLen 裁掉，无从追回
	if replayIsComplete("100-0", 3, true, "200-0") {
		t.Fatal("基线早于缓冲起点时，缺口已被裁掉，必须报不完整")
	}
}

func TestReplayIsCompleteWhenStreamGone(t *testing.T) {
	// 流已过 TTL 被整条删除：断线期间的事件无从追回，绝不能谎报"补齐了"
	if replayIsComplete("100-1", 0, false, "") {
		t.Fatal("会话流不存在时必须报不完整")
	}
}

func TestReplayIsCompleteWhenTruncatedAtCap(t *testing.T) {
	// 取回条数触顶 ⇒ 后面很可能还有更多。
	// 注意这里 hasStream/oldestID 给的是"看起来正常"的值：触顶本身就该否决。
	if replayIsComplete("100-1", sseEventsMaxLen+1, true, "100-0") {
		t.Fatal("取回条数触顶时必须报不完整（不能因为基线还在就判补齐）")
	}
	// 恰好等于上限不算触顶：多取一条的查询没取到第 N+1 条，说明确实到头了
	if !replayIsComplete("100-1", sseEventsMaxLen, true, "100-0") {
		t.Fatal("恰好取满上限且基线仍在时，应判定为已补齐")
	}
}

func TestReplayIsCompleteComparesNumericNotLexical(t *testing.T) {
	// 流 ID 是两段十进制数：字符串比较会认为 "...-9" > "...-10"，于是把
	// "基线已被驱逐"误判成"还在缓冲里"，缺口就被静默吞掉。
	if replayIsComplete("100-9", 1, true, "100-10") {
		t.Fatal("100-9 早于 100-10（数值序），基线已被驱逐，必须报不完整")
	}
	if !replayIsComplete("100-10", 1, true, "100-9") {
		t.Fatal("100-10 晚于 100-9（数值序），基线仍在缓冲内，应判定为已补齐")
	}
}
