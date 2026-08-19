#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析代理节点链接，生成 sing-box JSON 配置（本地 mixed 监听 127.0.0.1:1080）。

支持协议：
  vmess://  vless://  trojan://  hysteria2://  hy2://  tuic://  anytls://
  socks5://  socks://  http://  https://

用法：
  GOST_PROXY="vmess://..." python3 parse_proxy.py   # 生成 singbox_config.json
"""
import base64
import json
import os
import re
import sys
import urllib.parse

LOCAL_PORT = int(os.environ.get("LOCAL_PROXY_PORT", "1080"))


def b64decode(s: str) -> str:
    """兼容缺失 padding 的 base64 解码"""
    s = s.strip()
    if not s:
        return ""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    try:
        return base64.b64decode(s).decode("utf-8")
    except Exception:
        return ""


def _split_host_port(host_part: str, default_port: int = 443):
    """解析 host[:port]，兼容 IPv6。返回 (host, port)"""
    if host_part.startswith("[") and "]" in host_part:
        host, _, port_str = host_part.rpartition(":")
        host = host[1:-1]
        return host, int(port_str) if port_str.isdigit() else default_port
    if ":" in host_part:
        host, port_str = host_part.rsplit(":", 1)
        if port_str.isdigit():
            return host, int(port_str)
    return host_part, default_port


def _split_uri_body(url: str, proto: str):
    """把 proto:// 后面的部分拆成 (auth, host, port, query_dict, fragment)"""
    body = url[len(proto + "://"):]
    fragment = ""
    if "#" in body:
        body, fragment = body.split("#", 1)
    if "?" in body:
        body, qs_str = body.split("?", 1)
    else:
        qs_str = ""
    qs = urllib.parse.parse_qs(qs_str) if qs_str else {}
    if "@" in body:
        auth, host_part = body.rsplit("@", 1)
    else:
        auth, host_part = "", body
    host, port = _split_host_port(host_part)
    if not host:
        raise SystemExit(f"❌ 无法从 {proto} 链接解析出服务器地址")
    q = {k: v[0] for k, v in qs.items()}
    return auth, host, port, q, fragment


def _tls_block(q: dict, default_sni: str, transport_tls: bool = True) -> dict | None:
    """根据 query 构造 sing-box tls 块；security=none 且无 tls 时返回 None"""
    security = q.get("security", "tls" if transport_tls else "none")
    if security in ("none", "reality") and not transport_tls and not q.get("sni"):
        # reality 特殊处理在调用方，这里只处理普通 TLS
        pass
    if security in ("none",) and not q.get("sni"):
        return None
    if security == "reality":
        cfg = {"enabled": True, "server_name": q.get("sni", default_sni)}
        if q.get("fp"):
            cfg["utls"] = {"enabled": True, "fingerprint": q["fp"]}
        if q.get("pbk"):
            cfg["reality"] = {"enabled": True, "public_key": q["pbk"], "short_id": q.get("sid", "")}
        return cfg
    t = {"enabled": True}
    if q.get("sni"):
        t["server_name"] = q["sni"]
    elif default_sni:
        t["server_name"] = default_sni
    if q.get("insecure", q.get("allowInsecure", "0")) in ("1", "true", "yes"):
        t["insecure"] = True
    if q.get("alpn"):
        t["alpn"] = q["alpn"].split(",")
    fp = q.get("fp") or q.get("fingerprint")
    if fp and fp != "none":
        t["utls"] = {"enabled": True, "fingerprint": fp}
    return t


def _transport_block(q: dict, host_default: str = "") -> dict | None:
    """根据 query 的 type/net 构造运输层块（ws/http/grpc），tcp 返回 None"""
    net = (q.get("net") or q.get("type") or "tcp").lower()
    host = q.get("host", host_default)
    path = q.get("path", "")
    if net in ("ws", "websocket"):
        t = {"type": "ws"}
        # ⚠️ sing-box 会把 path 中的 '?' 编码成 %3F，导致服务端 404；
        #    因此剥离 query（如 ?ed=2560），只保留纯路径。
        if path and "?" in path:
            base_path = path.split("?", 1)[0]
            print(f"ℹ️  ws path 含 query，已剥离为 {base_path}（sing-box 不支持 URL query）")
            path = base_path
        if path:
            t["path"] = path
        if host:
            t["headers"] = {"Host": host}
        return t
    if net == "grpc":
        t = {"type": "grpc"}
        if path:
            t["service_name"] = path.lstrip("/")
        return t
    if net in ("h2", "http"):
        t = {"type": "http"}
        if host:
            t["host"] = host
        if path:
            t["path"] = path
        return t
    return None


# ---------------- vmess ----------------
def parse_vmess(url: str) -> dict:
    b = url[len("vmess://"):]
    if "#" in b:
        b = b.split("#", 1)[0]
    raw = b64decode(b)
    if not raw:
        raise SystemExit("❌ vmess:// base64 解码失败")

    if raw.startswith("{"):  # 老格式：base64(JSON)
        d = json.loads(raw)
        host = d.get("add", "")
        port = int(d.get("port", 443))
        uuid = d.get("id", "")
        net = d.get("net", "tcp")
        tls = d.get("tls", "none")
        sni = d.get("sni", "") or d.get("host", "")
        path = d.get("path", "")
        host_hdr = d.get("host", "")
        aid = int(d.get("aid", 0))
        security = d.get("scy", "auto") or "auto"
        fp = d.get("fp", "")
        q = {"type": net, "path": path, "host": host_hdr, "sni": sni, "security": security, "fp": fp}
    else:  # 新格式：base64(URI)
        m = re.match(r"^([^@]+)@(.+)$", raw)
        if not m:
            raise SystemExit("❌ vmess:// URI 格式解析失败")
        uuid, hp = m.group(1), m.group(2)
        host, port, frag_tmp = hp, 443, ""
        if "#" in host:
            host, frag_tmp = host.split("#", 1)
            host, port = _split_host_port(host)[0], _split_host_port(host)[1]
        if "?" in host:
            host_part, qs_str = host.split("?", 1)
            host, port = _split_host_port(host_part)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(qs_str).items()}
        else:
            host, port = _split_host_port(host)
            q = {}
        aid = int(q.get("alterId", q.get("aid", "0")))
        security = q.get("security", q.get("encryption", "")) or "auto"
        q.setdefault("sni", q.get("sni", host))
        fp = q.get("fp", "")
        q["fp"] = fp

    if not host or not uuid:
        raise SystemExit("❌ vmess 链接缺少 host 或 uuid")

    out = {
        "type": "vmess",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "security": security,
        "alter_id": aid,
    }
    tls = _tls_block(q, host)
    if tls:
        out["tls"] = tls
    trans = _transport_block(q, q.get("host", ""))
    if trans:
        out["transport"] = trans
    return out


# ---------------- vless / trojan / hysteria2 / tuic / anytls ----------------
def parse_vless(url: str) -> dict:
    auth, host, port, q, frag = _split_uri_body(url, "vless")
    out = {
        "type": "vless",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": auth,
    }
    if q.get("flow") and q["flow"] != "none":
        out["flow"] = q["flow"]
    tls = _tls_block(q, host)
    if tls:
        out["tls"] = tls
    trans = _transport_block(q, q.get("host", ""))
    if trans:
        out["transport"] = trans
    return out


def parse_trojan(url: str) -> dict:
    auth, host, port, q, frag = _split_uri_body(url, "trojan")
    out = {
        "type": "trojan",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": auth,
    }
    tls = _tls_block(q, host)
    if tls:
        out["tls"] = tls
    trans = _transport_block(q, q.get("host", ""))
    if trans:
        out["transport"] = trans
    return out


def parse_hysteria2(url: str) -> dict:
    if url.startswith("hy2://"):
        url = "hysteria2://" + url[6:]
    auth, host, port, q, frag = _split_uri_body(url, "hysteria2")
    out = {
        "type": "hysteria2",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": auth,
    }
    if q.get("obfs") and q["obfs"] != "none":
        out["obfs"] = {"type": q["obfs"]}
        if q.get("obfs-password"):
            out["obfs"]["password"] = q["obfs-password"]
    tls = _tls_block(q, host)
    if tls:
        out["tls"] = tls
    return out


def parse_tuic(url: str) -> dict:
    auth, host, port, q, frag = _split_uri_body(url, "tuic")
    uuid, _, password = auth.partition(":")
    out = {
        "type": "tuic",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "password": password,
    }
    if q.get("congestion_control"):
        out["congestion_control"] = q["congestion_control"]
    if q.get("udp_relay_mode"):
        out["udp_relay_mode"] = q["udp_relay_mode"]
    if q.get("zero_rtt_handshake") in ("1", "true", "yes"):
        out["zero_rtt_handshake"] = True
    tls = _tls_block(q, host)
    if tls:
        out["tls"] = tls
    return out


def parse_anytls(url: str) -> dict:
    auth, host, port, q, frag = _split_uri_body(url, "anytls")
    out = {
        "type": "anytls",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": auth,
    }
    if q.get("idle_session_check_interval"):
        out["idle_session_check_interval"] = int(q["idle_session_check_interval"])
    if q.get("idle_session_timeout"):
        out["idle_session_timeout"] = int(q["idle_session_timeout"])
    if q.get("min_idle_session"):
        out["min_idle_session"] = int(q["min_idle_session"])
    tls = _tls_block(q, host)
    if tls:
        out["tls"] = tls
    trans = _transport_block(q, q.get("host", ""))
    if trans:
        out["transport"] = trans
    return out


# ---------------- socks / http ----------------
def parse_socks(url: str) -> dict:
    # 兼容 socks:// 和 socks4:// 前缀，统一改写成 socks5:// 再解析
    if url.startswith("socks://"):
        url = "socks5://" + url[len("socks://"):]
    elif url.startswith("socks4://"):
        url = "socks5://" + url[len("socks4://"):]
    proto = "socks5" if url.startswith("socks5") else "socks"
    auth, host, port, q, frag = _split_uri_body(url, proto)
    out = {"type": "socks", "tag": "proxy", "server": host, "server_port": port}
    if auth:
        user, _, pwd = auth.partition(":")
        out["username"] = user
        out["password"] = pwd
    return out


def parse_http(url: str) -> dict:
    proto = "https" if url.startswith("https://") else "http"
    auth, host, port, q, frag = _split_uri_body(url, proto)
    out = {"type": "http", "tag": "proxy", "server": host, "server_port": port}
    if auth:
        user, _, pwd = auth.partition(":")
        out["username"] = user
        out["password"] = pwd
    if proto == "https" and q:
        tls = _tls_block(q, host)
        if tls:
            out["tls"] = tls
    return out


PARSERS = [
    ("vmess://", parse_vmess),
    ("vless://", parse_vless),
    ("trojan://", parse_trojan),
    ("hysteria2://", parse_hysteria2),
    ("hy2://", parse_hysteria2),
    ("tuic://", parse_tuic),
    ("anytls://", parse_anytls),
    ("socks5://", parse_socks),
    ("socks4://", parse_socks),
    ("socks://", parse_socks),
    ("https://", parse_http),
    ("http://", parse_http),
]


def build_config(proxy_url: str) -> dict:
    for prefix, parser in PARSERS:
        if proxy_url.startswith(prefix):
            outbound = parser(proxy_url)
            break
    else:
        raise SystemExit(f"❌ 不支持的代理链接: {proxy_url[:30]}...（支持 vmess/vless/trojan/hysteria2/tuic/anytls/socks/http）")

    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": LOCAL_PORT,
            }
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"final": "proxy"},
    }


def main():
    url = os.environ.get("GOST_PROXY", "")
    if not url:
        raise SystemExit("❌ 环境变量 GOST_PROXY 未设置")
    cfg = build_config(url)
    with open("singbox_config.json", "w") as f:
        json.dump(cfg, f, indent=2)
    # 脱敏打印
    safe = json.loads(json.dumps(cfg))
    ob = safe["outbounds"][0]
    for k in ("uuid", "password", "auth"):
        if k in ob:
            ob[k] = "***"
    if "username" in ob:
        ob["username"] = "***"
        ob["password"] = "***"
    tls = ob.get("tls")
    if tls:
        tls.pop("reality", None)
    print(json.dumps(safe, indent=2))


if __name__ == "__main__":
    main()