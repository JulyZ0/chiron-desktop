package storage

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// S3Store implements FileStore backed by an S3-compatible object store (MinIO / AWS S3).
type S3Store struct {
	client   *minio.Client
	bucket   string
	prefix   string // optional subdirectory prefix within the bucket
	endpoint string // used to construct external URLs
}

// NewS3Store creates a new S3Store connected to the given endpoint.
// endpoint: S3-compatible endpoint (e.g. "play.min.io" or "s3.amazonaws.com").
// bucket:   the bucket name (created if it doesn't exist).
// prefix:   optional prefix (subdirectory) — all paths are relative to this.
// creds:    (accessKey, secretKey, token) — token may be empty for static creds.
func NewS3Store(endpoint, bucket, prefix, accessKey, secretKey, token string, useSSL bool) (*S3Store, error) {
	if bucket == "" {
		return nil, fmt.Errorf("bucket is required")
	}

	client, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, token),
		Secure: useSSL,
	})
	if err != nil {
		return nil, fmt.Errorf("minio client: %w", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	// Ensure bucket exists (idempotent)
	if err := client.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); err != nil {
		// Check if bucket already exists
		exists, errExists := client.BucketExists(ctx, bucket)
		if errExists != nil {
			return nil, fmt.Errorf("ensure bucket %q: %w", bucket, errExists)
		}
		if !exists {
			return nil, fmt.Errorf("ensure bucket %q: %w", bucket, err)
		}
	}

	return &S3Store{
		client:   client,
		bucket:   bucket,
		prefix:   prefix,
		endpoint: endpoint,
	}, nil
}

func (s *S3Store) Read(ctx context.Context, path string) ([]byte, error) {
	objPath := s.objectPath(path)

	obj, err := s.client.GetObject(ctx, s.bucket, objPath, minio.GetObjectOptions{})
	if err != nil {
		return nil, fmt.Errorf("s3 get object %q: %w", objPath, err)
	}
	defer obj.Close()

	data, err := io.ReadAll(obj)
	if err != nil {
		return nil, fmt.Errorf("s3 read object %q: %w", objPath, err)
	}

	return data, nil
}

func (s *S3Store) Write(ctx context.Context, path string, data []byte) error {
	objPath := s.objectPath(path)

	r := bytes.NewReader(data)
	_, err := s.client.PutObject(ctx, s.bucket, objPath, r, int64(len(data)), minio.PutObjectOptions{
		ContentType: detectContentType(path),
	})
	if err != nil {
		return fmt.Errorf("s3 put object %q: %w", objPath, err)
	}

	return nil
}

// WriteStream 流式上传对象；size 未知传 -1（minio 自动分块）。
// perm 在对象存储语义下无意义，忽略。
func (s *S3Store) WriteStream(ctx context.Context, path string, r io.Reader, size int64, perm os.FileMode) error {
	objPath := s.objectPath(path)
	if _, err := s.client.PutObject(ctx, s.bucket, objPath, r, size, minio.PutObjectOptions{
		ContentType: detectContentType(path),
	}); err != nil {
		return fmt.Errorf("s3 put object %q: %w", objPath, err)
	}
	return nil
}

// OpenStream 返回对象读取流；先 Stat 一次，让"对象不存在"在打开阶段即暴露。
func (s *S3Store) OpenStream(ctx context.Context, path string) (io.ReadCloser, error) {
	objPath := s.objectPath(path)
	obj, err := s.client.GetObject(ctx, s.bucket, objPath, minio.GetObjectOptions{})
	if err != nil {
		return nil, fmt.Errorf("s3 get object %q: %w", objPath, err)
	}
	if _, err := obj.Stat(); err != nil {
		_ = obj.Close()
		return nil, fmt.Errorf("s3 stat object %q: %w", objPath, err)
	}
	return obj, nil
}

func (s *S3Store) Delete(ctx context.Context, path string) error {
	objPath := s.objectPath(path)

	err := s.client.RemoveObject(ctx, s.bucket, objPath, minio.RemoveObjectOptions{})
	if err != nil {
		return fmt.Errorf("s3 remove object %q: %w", objPath, err)
	}

	return nil
}

func (s *S3Store) List(ctx context.Context, prefix string) ([]FileInfo, error) {
	objPrefix := s.objectPath(prefix)

	var files []FileInfo
	for obj := range s.client.ListObjects(ctx, s.bucket, minio.ListObjectsOptions{
		Prefix:    objPrefix,
		Recursive: false,
	}) {
		if obj.Err != nil {
			return nil, fmt.Errorf("s3 list objects: %w", obj.Err)
		}
		if len(obj.Key) == 0 {
			continue
		}
		files = append(files, FileInfo{
			Path:     s.stripPrefix(obj.Key),
			Size:     obj.Size,
			IsDir:    obj.Key[len(obj.Key)-1] == '/',
			Modified: obj.LastModified.Format("2006-01-02T15:04:05Z"),
		})
	}

	if files == nil {
		files = []FileInfo{}
	}
	return files, nil
}

// PresignedPutURL returns a presigned URL for uploading an object via PUT.
// The URL expires after ttl. This enables client-side direct upload to S3.
func (s *S3Store) PresignedPutURL(ctx context.Context, path string, ttl time.Duration) (string, error) {
	objPath := s.objectPath(path)
	u, err := s.client.PresignedPutObject(ctx, s.bucket, objPath, ttl)
	if err != nil {
		return "", fmt.Errorf("presigned put url: %w", err)
	}
	return u.String(), nil
}

// PresignedGetURL returns a presigned URL for downloading an object.
// The URL expires after ttl. Enables temporary share links.
func (s *S3Store) PresignedGetURL(ctx context.Context, path string, ttl time.Duration) (string, error) {
	objPath := s.objectPath(path)
	u, err := s.client.PresignedGetObject(ctx, s.bucket, objPath, ttl, nil)
	if err != nil {
		return "", fmt.Errorf("s3 presigned get object %q: %w", objPath, err)
	}
	return u.String(), nil
}

// ObjectURL returns the public HTTP URL for an object.
func (s *S3Store) ObjectURL(objKey string) string {
	objPath := s.objectPath(objKey)
	return fmt.Sprintf("%s://%s/%s/%s", s.scheme(), s.endpoint, s.bucket, objPath)
}

// scheme 与 ObjectURL 的协议推导保持一致（本地 MinIO 走 http）。
func (s *S3Store) scheme() string {
	return ObjectURLScheme(s.endpoint)
}

// ObjectURLScheme 返回给定 endpoint 对应的公开 URL 协议。
//
// 与 ObjectURL 同源：只有「就地部署的 MinIO」是 http，其余按 https。
func ObjectURLScheme(endpoint string) string {
	if endpoint == "localhost" || strings.HasPrefix(endpoint, "localhost:") ||
		endpoint == "127.0.0.1" || strings.HasPrefix(endpoint, "127.0.0.1:") {
		return "http"
	}
	return "https"
}

// ErrObjectKeyNotInBucket 表示 URL 指向的不是本 store 的 bucket/prefix。
var ErrObjectKeyNotInBucket = errors.New("object url does not address this bucket")

// ObjectKeyFromURL 把一个「对象公开 URL」反解为对象键，并严格校验它确实由本
// 存储后端签出：scheme、host、bucket、prefix 四段都必须匹配。
//
// 为什么不能只比较 scheme/host：同 host 上通常还有别的 bucket（备份桶、其它环境
// 的桶），只比 host 等于承认「同 host 即同一对象所有权」—— 攻击者只要拿到同 host
// 下任意对象的 URL（哪怕属于别人、别的桶），就能把它写进自己的资产行，再通过
// 下载端点代理读取。所有权证明必须到 bucket + prefix 这一级。
func (s *S3Store) ObjectKeyFromURL(raw string) (string, error) {
	return ObjectKeyFromURL(raw, s.endpoint, s.bucket, s.prefix)
}

// ObjectKeyFromURL 是 (*S3Store).ObjectKeyFromURL 的纯函数形态（便于单测）。
func ObjectKeyFromURL(raw, endpoint, bucket, prefix string) (string, error) {
	if strings.TrimSpace(raw) == "" {
		return "", ErrObjectKeyNotInBucket
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return "", ErrObjectKeyNotInBucket
	}
	if u.User != nil {
		return "", ErrObjectKeyNotInBucket
	}
	if u.Scheme != ObjectURLScheme(endpoint) || u.Host != endpoint {
		return "", ErrObjectKeyNotInBucket
	}
	// 用 EscapedPath 再 Unescape：避免 %2e%2e 之类的编码绕过 path.Clean。
	escaped := u.EscapedPath()
	unescaped, err := url.PathUnescape(escaped)
	if err != nil {
		return "", ErrObjectKeyNotInBucket
	}
	key := strings.TrimPrefix(unescaped, "/")
	wantBucket := bucket + "/"
	if !strings.HasPrefix(key, wantBucket) {
		return "", ErrObjectKeyNotInBucket
	}
	key = strings.TrimPrefix(key, wantBucket)
	if prefix != "" {
		if !strings.HasPrefix(key, prefix+"/") {
			return "", ErrObjectKeyNotInBucket
		}
		key = strings.TrimPrefix(key, prefix+"/")
	}
	if key == "" || path.Clean(key) != key || strings.Contains(key, "..") || strings.Contains(key, "\\") {
		return "", ErrObjectKeyNotInBucket
	}
	return key, nil
}

// PresignedGetURLWithResponse 生成带「响应头覆盖」的预签名下载 URL。
//
// 对象存储会把 response-content-type / response-content-disposition /
// response-cache-control 原样写进下载响应，于是「必须另存为」「不得缓存」这类
// 策略在 302 直达对象存储的路径上同样生效 —— 否则网关侧设的响应头会在重定向后
// 全部丢失（浏览器最终看到的是对象存储的默认头）。
func (s *S3Store) PresignedGetURLWithResponse(ctx context.Context, objKey string, ttl time.Duration, contentType, disposition, cacheControl string) (string, error) {
	params := url.Values{}
	if contentType != "" {
		params.Set("response-content-type", contentType)
	}
	if disposition != "" {
		params.Set("response-content-disposition", disposition)
	}
	if cacheControl != "" {
		params.Set("response-cache-control", cacheControl)
	}
	if len(params) == 0 {
		return s.PresignedGetURL(ctx, objKey, ttl)
	}
	objPath := s.objectPath(objKey)
	u, err := s.client.PresignedGetObject(ctx, s.bucket, objPath, ttl, params)
	if err != nil {
		return "", fmt.Errorf("s3 presigned get object %q: %w", objPath, err)
	}
	return u.String(), nil
}

// objectPath joins the store prefix with the user-provided path.
func (s *S3Store) objectPath(path string) string {
	if s.prefix == "" {
		return filepath.ToSlash(path)
	}
	return filepath.ToSlash(s.prefix + "/" + path)
}

// stripPrefix removes the store prefix from an S3 object key.
func (s *S3Store) stripPrefix(key string) string {
	if s.prefix == "" {
		return key
	}
	expected := s.prefix + "/"
	if strings.HasPrefix(key, expected) {
		return key[len(expected):]
	}
	return key
}

func detectContentType(path string) string {
	ext := filepath.Ext(path)
	switch ext {
	case ".html", ".htm":
		return "text/html"
	case ".css":
		return "text/css"
	case ".js":
		return "application/javascript"
	case ".json":
		return "application/json"
	case ".png":
		return "image/png"
	case ".jpg", ".jpeg":
		return "image/jpeg"
	case ".gif":
		return "image/gif"
	case ".svg":
		return "image/svg+xml"
	case ".pdf":
		return "application/pdf"
	case ".md":
		return "text/markdown"
	case ".txt":
		return "text/plain"
	case ".yaml", ".yml":
		return "application/x-yaml"
	case ".zip":
		return "application/zip"
	case ".gz", ".tar.gz":
		return "application/gzip"
	default:
		return "application/octet-stream"
	}
}
