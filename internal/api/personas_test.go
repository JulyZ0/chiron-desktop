package api

import (
	"context"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/db"
)

func TestValidatePersona(t *testing.T) {
	cases := []struct {
		name       string
		persona    string
		prompt     string
		wantReject bool
	}{
		{"ok", "猫娘", "你是猫娘", false},
		{"missing name", "  ", "你是猫娘", true},
		{"missing prompt", "猫娘", "  ", true},
		{"name too long", strings.Repeat("猫", personaMaxName+1), "p", true},
		{"prompt too long", "猫娘", strings.Repeat("x", personaMaxPrompt+1), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rejected := validatePersona(strings.TrimSpace(tc.persona), strings.TrimSpace(tc.prompt)) != ""
			if rejected != tc.wantReject {
				t.Fatalf("validatePersona(%q, %q) rejected=%v, want %v", tc.persona, tc.prompt, rejected, tc.wantReject)
			}
		})
	}
}

// 没有数据库时必须 fail-soft：人格是增强，不该让提交直接失败。
func TestPersonaForContextWithoutDB(t *testing.T) {
	prev := db.GlobalDBManager
	db.GlobalDBManager = nil
	t.Cleanup(func() { db.GlobalDBManager = prev })

	if _, ok := personaForContext(context.Background(), 1, "p1"); ok {
		t.Fatal("数据库不可用时不应返回人格")
	}
}
