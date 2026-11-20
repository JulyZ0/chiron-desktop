# Multi-stage build — Python AI Engine v0.1.260825.01
FROM python:3.11-slim AS base

WORKDIR /app

# 系统依赖（ffmpeg：video_analyze 抽帧需要；**不引 Python 依赖**，见 app/tools/video.py）
#
# git：**运行时必需**，不是开发工具。`app/agent/git_checkpoint.py` 的还原点
# （`git init` / `read-tree` / `commit-tree` / `restore`）全部通过子进程调 git，
# 镜像里没有它时每个回合的检查点都会落成 `state='failed'`，而 fail-open 语义
# 让这件事只出现在日志里 —— 用户看到的是"还原点列表永远为空"。
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi-dev \
    libmagic1 \
    file \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# 依赖安装（缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# ⚠ 再单独升一次 setuptools/wheel：Trivy 扫的是镜像里的 dist-info，而 **setuptools 自带的
# `_vendor/`** 里躺着 jaraco.context 5.3.0（CVE-2026-23949）与 wheel 0.45.1（CVE-2026-24049）
# —— 这两个包**不在 requirements.txt 里**，只能靠升级 setuptools 本身把它们换掉。
RUN pip install --no-cache-dir --upgrade setuptools wheel

# 应用代码（排除 examples/ 目录，防止生产环境意外加载 mock 代码）
COPY app/ app/

# 非 root 用户
RUN useradd -m -u 1000 appuser
# 共享卷挂载点（项目根 / 检查点 / 知识库原文与索引 / 沙箱）。
# **必须在镜像里就建好并 chown 到运行用户**：named volume 首次挂载到不存在的路径时，
# Docker 会以 root 建空目录，容器内 uid=1000 写不进去（表现为检查点全 failed、
# 知识库上传 503）。目录已存在时卷会沿用镜像里的属主。
RUN mkdir -p /srv/chiron/projects /srv/chiron/checkpoints /srv/chiron/sandbox \
        /srv/chiron/knowledge /srv/chiron/knowledge_index \
    && chown -R appuser:appuser /srv/chiron
USER appuser

# 端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

# 启动
CMD ["python", "-m", "app.main"]
