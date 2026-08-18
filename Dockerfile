FROM python:3.11-slim

# 系统依赖：Chrome + Xvfb + xdotool + 截图工具
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    google-chrome-stable \
    xvfb \
    x11-utils \
    xdotool \
    scrot \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY zampto_renew.py /app/zampto_renew.py

RUN pip install --no-cache-dir seleniumbase \
    && seleniumbase install chromedriver

# 运行入口：用 xvfb-run 包裹脚本
ENTRYPOINT ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24", "python", "zampto_renew.py"]
