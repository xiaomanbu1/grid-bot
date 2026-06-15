FROM python:3.12-slim

WORKDIR /app

# 先装依赖, 利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

# 数据目录 (SQLite + 日志) 挂出来持久化
RUN mkdir -p /app/data

EXPOSE 8787

# 配置文件通过 volume 挂载进来, 不打进镜像 (避免密钥进镜像层)
CMD ["python", "main.py", "live", "--config", "config.local.yaml"]
