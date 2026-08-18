#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析 hysteria2:// / hy2:// 节点链接，生成 hysteria client JSON 配置。
使用正则硬解析，避免 urllib.parse 对复杂 hy2 URL 拆分错误。
"""
import json
import os
import re
import urllib.parse


def parse_hy2_url(url: str) -> dict:
    if url.startswith("hy2://"):
        url = "hysteria2://" + url[6:]
    if not url.startswith("hysteria2://"):
        raise SystemExit(f"❌ 不是 hy2/hysteria2 链接: {url[:20]}...")

    body = url[12:]  # 去掉 hysteria2://

    # 先分离 query
    if "?" in body:
        body, query_str = body.split("?", 1)
    else:
        query_str = ""

    # 分离 auth 和 host
    # 格式: [auth@]host[:port]
    # auth 可能包含 :（如 uuid:password）也可能就是 uuid，这里兼容：取最后一个 @ 之前为 auth
    if "@" in body:
        auth, host_part = body.rsplit("@", 1)
    else:
        auth = ""
        host_part = body

    # 解析 host:port
    if host_part.startswith("[") and "]" in host_part:
        # IPv6
        host, _, port_str = host_part.rpartition(":")
        host = host[1:-1]
        port = int(port_str) if port_str else 443
    elif ":" in host_part:
        host, port_str = host_part.rsplit(":", 1)
        port = int(port_str) if port_str.isdigit() else 443
    else:
        host = host_part
        port = 443

    if not host:
        raise SystemExit("❌ 无法从 hy2 链接解析出服务器地址")

    qs = urllib.parse.parse_qs(query_str)

    insecure = qs.get("insecure", ["0"])[0].lower() in ("1", "true", "yes")
    sni = qs.get("sni", [host])[0] or host
    alpn = qs.get("alpn", ["h3"])[0]
    obfs = qs.get("obfs", [""])[0]
    obfs_password = qs.get("obfs-password", [""])[0]

    cfg = {
        "server": f"{host}:{port}",
        "auth": auth,
        "tls": {"sni": sni, "insecure": insecure},
        "quic": {
            "initStreamReceiveWindow": 8388608,
            "maxStreamReceiveWindow": 8388608,
            "initConnReceiveWindow": 20971520,
            "maxConnReceiveWindow": 20971520,
        },
        "socks5": {"listen": f"127.0.0.1:{os.environ.get('LOCAL_PROXY_PORT', '1080')}"},
    }
    if alpn:
        cfg["tls"]["alpn"] = alpn.split(",")
    if obfs:
        cfg["obfs"] = {"type": obfs}
        if obfs_password:
            cfg["obfs"]["salamander"] = {"password": obfs_password}

    return cfg


def main():
    url = os.environ.get("GOST_PROXY", "")
    if not url:
        raise SystemExit("❌ 环境变量 GOST_PROXY 未设置")

    cfg = parse_hy2_url(url)
    json.dump(cfg, open("hy2_config.json", "w"), indent=2)
    # 打印脱敏后的配置便于调试
    safe = {k: (v if k != "auth" else "***") for k, v in cfg.items()}
    print(json.dumps(safe, indent=2))


if __name__ == "__main__":
    main()
