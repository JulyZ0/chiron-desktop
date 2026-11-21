package config

import "testing"

// 团队模式只关注册；本地回环免口令必须保留 —— owner 就是运行程序的第一个
// Windows 用户，关掉它会让本机管理员也进不来。
func TestTeamModeDisablesRegistrationButKeepsLocalBootstrap(t *testing.T) {
	t.Setenv("CHIRON_DEPLOY_MODE", "team")
	t.Setenv("DISABLE_REGISTRATION", "")
	t.Setenv("LOCAL_BOOTSTRAP", "1")

	cfg := loadConfig()
	if !cfg.IsTeamMode() {
		t.Fatal("team 模式下 IsTeamMode 应为 true")
	}
	if !cfg.DisableRegistration {
		t.Fatal("team 模式必须关闭注册")
	}
	if !cfg.LocalBootstrap {
		t.Fatal("team 模式不应关闭本地回环免口令（owner 是本机首个 Windows 用户）")
	}
}

// 服务模式开关（后台保存）与部署级 team 同义：任一为真都算团队模式。
func TestServiceModeCountsAsTeamMode(t *testing.T) {
	t.Setenv("CHIRON_DEPLOY_MODE", "")
	t.Setenv("CHIRON_SERVICE_MODE", "")
	cfg := loadConfig()
	if cfg.IsTeamMode() {
		t.Fatal("默认应为 desktop 模式")
	}
	cfg.ServiceMode = true
	if !cfg.IsTeamMode() {
		t.Fatal("后台服务模式开关为真时应视为团队模式")
	}
}

func TestInvalidDeployModeRejected(t *testing.T) {
	t.Setenv("CHIRON_DEPLOY_MODE", "clod")
	cfg := loadConfig()
	if cfg.DeployMode == DeployModeDesktop || cfg.DeployMode == DeployModeTeam {
		t.Fatalf("非法值不应被静默归一，得到 %q", cfg.DeployMode)
	}
}
