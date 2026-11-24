package auth

import "strings"

// NormalizeEmail 返回邮箱的唯一规范化形态：去掉首尾空白并统一小写。
//
// 为什么必须有这个单一入口：登录自 C1 起就用 `lower(email) = lower($1)` 匹配，
// 而 users 的唯一索引建在**原始** email 上（区分大小写）。两者口径不一致会同时
// 造成两类问题 —— 大小写变体可以重复注册（唯一索引不拦），但登录时只能命中其中
// 一行，另一行永远登不进去。写入与查询都必须过这里，唯一索引才对得上登录谓词。
func NormalizeEmail(raw string) string {
	return strings.ToLower(strings.TrimSpace(raw))
}

// EmailsEquivalent 判断两个邮箱在登录意义上是否等价（大小写/空白不敏感）。
// 用于「本次写入是否会与既有行撞车」的等价性比较。
func EmailsEquivalent(a, b string) bool {
	return NormalizeEmail(a) == NormalizeEmail(b)
}
