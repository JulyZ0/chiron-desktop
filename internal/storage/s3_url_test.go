package storage

import "testing"

// H29：只比较 scheme/host 不足以证明对象所有权 —— 同 host 上可能还有别的 bucket。
// 这个纯函数是「客户端 file_url 是否指向本后端某个对象」的唯一判据。
func TestObjectKeyFromURL(t *testing.T) {
	const (
		endpoint = "minio.example:9000"
		bucket   = "chiron"
	)

	ok := []struct{ raw, want string }{
		{"https://minio.example:9000/chiron/media/1/asset/a.png", "media/1/asset/a.png"},
		{"https://minio.example:9000/chiron/media/1/asset/a%20b.png", "media/1/asset/a b.png"},
	}
	for _, c := range ok {
		got, err := ObjectKeyFromURL(c.raw, endpoint, bucket, "")
		if err != nil {
			t.Fatalf("%s：应通过，得到错误 %v", c.raw, err)
		}
		if got != c.want {
			t.Fatalf("%s：键 = %q，期望 %q", c.raw, got, c.want)
		}
	}

	// prefix（子目录）也必须匹配：否则「同桶但不同前缀」的对象同样被当成自己的。
	got, err := ObjectKeyFromURL("https://minio.example:9000/chiron/media-root/media/1/a.png", endpoint, bucket, "media-root")
	if err != nil || got != "media/1/a.png" {
		t.Fatalf("带 prefix 时应得到 media/1/a.png，得到 %q / %v", got, err)
	}

	bad := []struct{ name, raw string }{
		{"other host", "https://evil.example/chiron/media/1/a.png"},
		{"other bucket", "https://minio.example:9000/other-bucket/media/1/a.png"},
		{"wrong scheme", "http://minio.example:9000/chiron/media/1/a.png"},
		{"userinfo", "https://u:p@minio.example:9000/chiron/media/1/a.png"},
		{"traversal", "https://minio.example:9000/chiron/media/1/../../2/a.png"},
		{"encoded traversal", "https://minio.example:9000/chiron/media/1/%2e%2e/2/a.png"},
		{"empty", ""},
		{"bare path", "/media/1/a.png"},
	}
	for _, c := range bad {
		if _, err := ObjectKeyFromURL(c.raw, endpoint, bucket, ""); err == nil {
			t.Fatalf("%s：应被拒绝（%s）", c.name, c.raw)
		}
	}

	// 配置了 prefix 时，缺 prefix 的 URL 必须被拒（同桶不同前缀 ≠ 本后端对象）。
	if _, err := ObjectKeyFromURL("https://minio.example:9000/chiron/media/1/a.png", endpoint, bucket, "media-root"); err == nil {
		t.Fatal("缺 prefix 时应被拒绝")
	}
}

func TestObjectURLScheme(t *testing.T) {
	if got := ObjectURLScheme("localhost:9000"); got != "http" {
		t.Fatalf("localhost 应 http，得到 %q", got)
	}
	if got := ObjectURLScheme("s3.amazonaws.com"); got != "https" {
		t.Fatalf("公网 endpoint 应 https，得到 %q", got)
	}
}
