# 数据分析报告智能体 - 服务器部署镜像（Python 出图，无 MATLAB）
FROM python:3.11-slim

WORKDIR /app

# 中文字体（matplotlib 出图需要）+ 时区
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-zenhei tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV TZ=Asia/Shanghai
ENV REPORT_AGENT_CONFIG=/app/config.json

# 默认以定时调度服务运行；一次性生成可改为:
# CMD ["python", "run_agent.py", "--mode", "weekly"]
CMD ["python", "serve_scheduler.py"]
