# 假 LLM 镜像：标准库 http.server，无第三方依赖。
FROM python:3.11-slim

WORKDIR /app
COPY fake_llm.py /app/fake_llm.py

EXPOSE 8081
CMD ["python", "/app/fake_llm.py"]
