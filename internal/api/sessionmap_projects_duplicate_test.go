package api

import "testing"

// sameRootPath 是「禁止重复路径项目」的唯一判据。
//
// 它必须同时满足两件事：同一目录的不同写法要被识别为同一个（否则用户换个写法
// 就能建出第二个项目，两个项目对同一个 `.git` 各自建检查点，时间线互相污染）；
// 不同目录不能被误判为同一个（否则用户根本建不出第二个项目）。
func TestSameRootPath(t *testing.T) {
	cases := []struct {
		name string
		a, b string
		want bool
	}{
		{"完全相同", `D:\repo`, `D:\repo`, true},
		{"大小写不同（Windows/macOS 同一目录）", `D:\Repo`, `d:\repo`, true},
		{"尾分隔符", `D:\repo\`, `D:\repo`, true},
		{"含 . 片段", `D:\repo\.`, `D:\repo`, true},
		{"含 .. 片段", `D:\repo\sub\..`, `D:\repo`, true},
		{"不同目录", `D:\repo`, `D:\other`, false},
		{"前缀相同但不是同级", `D:\repo`, `D:\repo2`, false},
		{"空串不参与匹配", "", `D:\repo`, false},
		{"两边都空", "", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := sameRootPath(tc.a, tc.b); got != tc.want {
				t.Fatalf("sameRootPath(%q, %q) = %v, want %v", tc.a, tc.b, got, tc.want)
			}
		})
	}
}
