package auth

import "testing"

func TestNormalizeEmail(t *testing.T) {
	cases := []struct{ in, want string }{
		{"  User@Example.COM ", "user@example.com"},
		{"USER@EXAMPLE.COM", "user@example.com"},
		{"user@example.com", "user@example.com"},
		{"", ""},
		{"\tA@B.co\n", "a@b.co"},
	}
	for _, c := range cases {
		if got := NormalizeEmail(c.in); got != c.want {
			t.Fatalf("NormalizeEmail(%q) = %q，期望 %q", c.in, got, c.want)
		}
	}
}

func TestEmailsEquivalent(t *testing.T) {
	// 与 users 上 lower(email) 唯一索引口径一致：这些对子在库里只能是同一行。
	if !EmailsEquivalent("Bob@Example.com", "bob@example.COM ") {
		t.Fatal("大小写/空白差异应视为同一邮箱")
	}
	if EmailsEquivalent("bob@example.com", "alice@example.com") {
		t.Fatal("不同邮箱不应等价")
	}
}
