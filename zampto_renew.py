#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import os
import json
import shutil
import requests
import subprocess
from datetime import datetime
from typing import Callable, Optional
from seleniumbase import SB

# ============================================================
#   ⚙️ 配置（全部从环境变量读取，适配 akimify/Zampto-Renew 的 GitHub Actions 工作流）
# ============================================================
# ZAMPTO_ACCOUNT: 你的 Zampto 登录邮箱
ZAMPTO_ACCOUNT = os.environ.get("ZAMPTO_ACCOUNT", "")

# ZAMPTO_PASSWORD: 你的 Zampto 登录密码（工作流未明确该变量名，这里兼容两种命名）
ZAMPTO_PASSWORD = os.environ.get("ZAMPTO_PASSWORD", "") or os.environ.get("PASSWORD", "")

# TG 通知：token 与 channel 必须分开存储（不兼容 "token:chatid" 合并写法）
#   TG_BOT_TOKEN: bot token，形如 123456:ABC-DEF...（本身含冒号，故不可与 chat_id 合并）
#   TG_CHAT_ID:   接收者，形如 123456789（用户 ID）或 @channel_name（频道/群组）
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "") or os.environ.get("TG_TOKEN", "")
TG_ID = os.environ.get("TG_CHAT_ID", "") or os.environ.get("TG_ID", "")

# GOST_PROXY: 上游代理链接（支持 hy2://、hysteria2://、socks5://、http:// 等）。
#   工作流会把它转成统一的本地 SOCKS5 端口 127.0.0.1:1080，脚本直接走本地端口。
GOST_PROXY = os.environ.get("GOST_PROXY", "")
LOCAL_PROXY = "socks5://127.0.0.1:1080" if GOST_PROXY else ""

# LOGIN_URL: Zampto 登录页地址；未设置或为空时使用默认地址
#   根据当前面板，默认登录页为 /auth/login
LOGIN_URL = os.environ.get("LOGIN_URL", "") or "https://dash.zampto.net/auth/login"

# ZAMPTO_APP_ID: 可选，登录 URL 上的 app_id 参数；如果设置且 LOGIN_URL 未手动指定，
#   会在默认地址后追加 ?app_id=...。当前 dash.zampto.net 用不到，保持兼容。
ZAMPTO_APP_ID = os.environ.get("ZAMPTO_APP_ID", "")
if ZAMPTO_APP_ID and LOGIN_URL == "https://dash.zampto.net/auth/login":
    LOGIN_URL = f"{LOGIN_URL}?app_id={ZAMPTO_APP_ID}"

DOMAIN = os.environ.get("ZAMPTO_DOMAIN", "") or "dash.zampto.net"

# TARGET_SERVERS: JSON 字符串，例如 '[{"id":"4480","name":"java"},{"id":"4481","name":"python"}]'
#   也可用 TARGET_IDS / TARGET_NAMES 逗号分隔形式
_raw_servers = os.environ.get("TARGET_SERVERS", "")
if _raw_servers:
    try:
        TARGET_SERVERS = json.loads(_raw_servers)
    except Exception:
        TARGET_SERVERS = []
else:
    _ids = os.environ.get("TARGET_IDS", "").split(",")
    _names = os.environ.get("TARGET_NAMES", "").split(",")
    TARGET_SERVERS = []
    for i, sid in enumerate([x.strip() for x in _ids if x.strip()]):
        name = _names[i].strip() if i < len(_names) else sid
        TARGET_SERVERS.append({"id": sid, "name": name})

if not ZAMPTO_ACCOUNT or not ZAMPTO_PASSWORD:
    raise SystemExit("❌ 缺少必填环境变量 ZAMPTO_ACCOUNT / ZAMPTO_PASSWORD")

if not TARGET_SERVERS:
    raise SystemExit("❌ 缺少服务器列表，请设置 TARGET_SERVERS 或 TARGET_IDS")


# ============================================================
#   工具函数
# ============================================================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _proxies():
    if LOCAL_PROXY:
        return {"http": LOCAL_PROXY, "https": LOCAL_PROXY}
    return {}


def send_tg(msg: str):
    if not TG_TOKEN or not TG_ID:
        print("⚠️ 未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, timeout=15, proxies=_proxies(),
                          json={"chat_id": TG_ID, "text": msg, "parse_mode": "HTML"})
        if r.status_code == 200 and r.json().get("ok"):
            print("✅ Telegram 消息已发送")
        else:
            print(f"⚠️ TG 通知被拒绝: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ TG 通知发送异常: {e}")


def take_screenshot(sb, filename: str):
    sb.save_screenshot(filename)
    print(f"📸 截图 → {filename}")


# ============================================================
#   Turnstile（完全照搬 weirdhost 逻辑）
# ============================================================
_EXPAND_JS = """
(function() {
    var ts = document.querySelector('input[name="cf-turnstile-response"]');
    if (!ts) return 'no-turnstile';
    var el = ts;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
        el.style.minWidth = 'max-content';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})();
"""


def _ts_exists(sb, label: str = "", detect_loading: bool = False):
    """检测页面或弹框内是否存在 Turnstile 相关元素。
    只认 Cloudflare Turnstile 的 input 或 challenges.cloudflare.com iframe，
    避免把 googlesyndication 广告 iframe 误判为 Turnstile。
    label 用于在日志中标记当前检测阶段。
    detect_loading: True 时，若弹框仍在 'Loading security verification...' 也视为即将出现。"""
    try:
        info = sb.execute_script("""
            (function(){
                var input = document.querySelector('input[name="cf-turnstile-response"]');
                var inputFound = !!input;
                var inputVisible = !!(input && input.offsetParent !== null);
                var cfFrames = [];
                function checkFrames(list) {
                    for (var i = 0; i < list.length; i++) {
                        var src = list[i].src || '';
                        if (src.indexOf('challenges.cloudflare.com') !== -1) {
                            cfFrames.push(src.substring(0, 120));
                        }
                    }
                }
                checkFrames(document.querySelectorAll('iframe'));
                var boxes = document.querySelectorAll('div[role="dialog"], div[role="alertdialog"], .modal, [class*="modal"], [class*="dialog"], [class*="Dialog"]');
                for (var b = 0; b < boxes.length; b++) {
                    checkFrames(boxes[b].querySelectorAll('iframe'));
                }
                var loading = false;
                var bodyText = (document.body.innerText || '').toLowerCase();
                if (bodyText.indexOf('loading security verification') !== -1 ||
                    bodyText.indexOf('please complete the security verification') !== -1) {
                    loading = true;
                }
                return {inputFound: inputFound, inputVisible: inputVisible, cfFrames: cfFrames, loading: loading};
            })();
        """)
        exists = bool(info.get("inputFound") or info.get("cfFrames"))
        if exists:
            print(f"  [TS检测/{label or 'default'}] ✅ 发现 Turnstile: inputFound={info.get('inputFound')} inputVisible={info.get('inputVisible')} cfFrames={len(info.get('cfFrames', []))}")
        elif detect_loading and info.get("loading"):
            print(f"  [TS检测/{label or 'default'}] ⏳ Turnstile 仍在 Loading 中，继续等待...")
            return True  # 视为存在，让上层继续等待
        return exists
    except Exception as e:
        print(f"  [TS检测/{label or 'default'}] ⚠️ JS 执行异常: {e}")
        return False


def _ts_real_present(sb, label: str = ""):
    """只认真正的 Turnstile 元素（input 或 cloudflare iframe），不含 Loading 文本。
    用于重触发后判断是否已真正出现、可以开始处理；见到 Loading 不算。"""
    try:
        present = bool(sb.execute_script("""
            (function(){
                var input = document.querySelector('input[name="cf-turnstile-response"]');
                if (input) return true;
                var frames = document.querySelectorAll('iframe');
                for (var i = 0; i < frames.length; i++) {
                    var src = frames[i].src || '';
                    if (src.indexOf('challenges.cloudflare.com') !== -1) return true;
                }
                return false;
            })();
        """))
        if present:
            print(f"  [TS真实检测/{label or 'default'}] ✅ Turnstile 已真正出现")
        return present
    except Exception as e:
        print(f"  [TS真实检测/{label or 'default'}] ⚠️ JS 异常: {e}")
        return False

def _ts_solved(sb):
    """检测 Turnstile token 是否已生成"""
    try:
        return bool(sb.execute_script("""
            (function(){
                var i=document.querySelector('input[name="cf-turnstile-response"]');
                return !!(i && i.value && i.value.length > 20);
            })();
        """))
    except:
        return False


def _have_xdotool():
    return bool(shutil.which("xdotool"))


def _py_xclick(x, y) -> bool:
    """python-xlib XTEST 模拟真实点击（无 xdotool 时的兜底，Xvfb 单屏环境足够）。"""
    try:
        from Xlib import display, X
        from Xlib.ext import xtest
        d = display.Display()
        root = d.screen().root
        root.warp_pointer(x, y)
        d.sync()
        xtest.fake_input(d, X.ButtonPress, 1)
        d.sync()
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync()
        d.close()
        return True
    except Exception as e:
        print(f"  ⚠️ python-xlib 点击失败: {e}")
        return False


def _activate_win():
    if not _have_xdotool():
        # 无 xdotool：Xvfb 无窗口管理器，无需激活窗口，直接点击坐标即可
        return
    try:
        r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "chrome"],
                           capture_output=True, text=True, timeout=3)
        wids = r.stdout.strip().split("\n")
        if wids and wids[0]:
            subprocess.run(["xdotool", "windowactivate", wids[0]],
                           timeout=2, stderr=subprocess.DEVNULL)
            time.sleep(0.2)
    except:
        pass


def _xclick(x, y):
    if _have_xdotool():
        _activate_win()
        try:
            subprocess.run(["xdotool", "mousemove", str(x), str(y)], timeout=2, stderr=subprocess.DEVNULL)
            time.sleep(0.15)
            subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
            return
        except:
            os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")
            return
    # 无 xdotool 时用 python-xlib 模拟
    _py_xclick(x, y)


def _rand_sleep(a=0.3, b=0.8):
    time.sleep(a + (b - a) * (hash(str(time.time())) % 1000 / 1000.0))


def _humanize(sb):
    """模拟人类行为：随机滚动、鼠标移动，降低被风控概率。"""
    try:
        sb.execute_script("""
            (function() {
                var h = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
                var y = Math.floor(Math.random() * Math.min(h, 600));
                window.scrollTo({top: y, behavior: 'smooth'});
            })();
        """)
        _rand_sleep(0.3, 0.8)
        # 在页面中心附近随机移动鼠标
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            body = sb.driver.find_element("tag name", "body")
            chain = ActionChains(sb.driver)
            chain.move_to_element_with_offset(body, -100 + int(200 * (hash(str(time.time())) % 1000 / 1000.0)),
                                              -150 + int(300 * (hash(str(time.time() + 1)) % 1000 / 1000.0)))
            chain.perform()
        except Exception:
            pass
    except Exception:
        pass


def _dump_turnstile_state(sb, label=""):
    """输出当前 Turnstile 状态，方便排查。"""
    try:
        info = sb.execute_script("""
            (function() {
                var iframes = Array.from(document.querySelectorAll('iframe')).map(function(f, i){
                    var r = f.getBoundingClientRect();
                    return {
                        idx: i, src: (f.src || '').substring(0, 120),
                        w: r.width, h: r.height, x: Math.round(r.x), y: Math.round(r.y),
                        display: f.style.display, vis: f.style.visibility, op: f.style.opacity
                    };
                }).filter(function(x){ return x.src.includes('cloudflare') || x.src.includes('turnstile') || x.w>0; });
                var inp = document.querySelector('input[name="cf-turnstile-response"]');
                return {
                    iframe_count: document.querySelectorAll('iframe').length,
                    cf_iframes: iframes,
                    has_input: !!inp,
                    input_value_len: inp ? (inp.value || '').length : 0,
                    input_display: inp ? (inp.style.display || '') : ''
                };
            })();
        """)
        print(f"  [TS诊断{label}] {json.dumps(info, ensure_ascii=False)}")
    except Exception as e:
        print(f"  [TS诊断{label}] 获取失败: {e}")


def _click_turnstile(sb):
    """多策略处理 Turnstile：SeleniumBase 原生 > iframe 内真实点击 > JS 事件 > xdotool。
    对隐藏式 invisible Turnstile 不强行点击，而是用人类化行为等待 token 自动生成。
    支持弹框/模态框内嵌的 Turnstile。"""
    _dump_turnstile_state(sb, "点击前")

    # 优先策略：直接定位 input[name="cf-turnstile-response"] 的可见父容器并点击。
    # 弹框内的 Turnstile 经常 iframe 没渲染或隐藏，但 input 一定存在。
    try:
        ok = sb.execute_script("""
            (function() {
                var inp = document.querySelector('input[name="cf-turnstile-response"]');
                if (!inp) return null;
                var el = inp;
                for (var i = 0; i < 16; i++) {
                    el = el.parentElement;
                    if (!el) break;
                    var rect = el.getBoundingClientRect();
                    if (rect.width >= 50 && rect.height >= 50 && rect.x >= 0 && rect.y >= 0) {
                        ['mousedown','mouseup','click'].forEach(function(t){
                            el.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true,
                                clientX: rect.x + rect.width/2, clientY: rect.y + rect.height/2, view: window}));
                        });
                        return {w: rect.width, h: rect.height, x: rect.x, y: rect.y};
                    }
                }
                return null;
            })();
        """)
        if ok:
            print(f"  🖱️ 已用 JS 点击 cf-turnstile-response 容器: {ok}")
            _rand_sleep(1.0, 2.0)
            if _ts_solved(sb): return
    except Exception as e:
        print(f"  ⚠️ JS 点击容器失败: {e}")

    # 次优先：等 iframe 实际渲染后再定位点击（弹框内常需加载完才出现）
    for _ in range(10):
        ready = sb.execute_script("""
            (function(){
                var iframes = document.querySelectorAll('iframe');
                for (var i = 0; i < iframes.length; i++) {
                    var src = iframes[i].src || '';
                    if (src.includes('challenges.cloudflare.com')) {
                        var r = iframes[i].getBoundingClientRect();
                        if (r.width > 30 && r.height > 30) return true;
                    }
                }
                return false;
            })();
        """)
        if ready:
            break
        print("  ⏳ 等待 Turnstile iframe 渲染...")
        time.sleep(0.8)

    # 策略1：SeleniumBase 自带的 undetected 能力
    try:
        sb.execute_script(_EXPAND_JS)
        _rand_sleep(0.2, 0.5)
        if hasattr(sb, "handle_turnstile"):
            sb.handle_turnstile()
            print("  🖱️ 已调用 SeleniumBase handle_turnstile")
            _rand_sleep(1.0, 2.0)
            if _ts_solved(sb):
                return
    except Exception as e:
        print(f"  ⚠️ SeleniumBase handle_turnstile 未成功: {e}")

    # 策略2：判断是否为 invisible Turnstile：iframe 隐藏/极小则不要点，做人类化行为等 token
    iframe_info = None
    try:
        iframe_info = sb.execute_script("""
            (function() {
                var list = document.querySelectorAll('iframe');
                for (var i = 0; i < list.length; i++) {
                    var src = list[i].src || '';
                    if (src.includes('challenges.cloudflare.com')) {
                        var r = list[i].getBoundingClientRect();
                        var cs = window.getComputedStyle(list[i]);
                        return {index: i, x: r.x, y: r.y, w: r.width, h: r.height,
                                hidden: r.width < 10 || r.height < 10 || cs.visibility === 'hidden'};
                    }
                }
                return null;
            })();
        """)
        if iframe_info and iframe_info.get("hidden"):
            print("  🫥 检测到隐藏式 invisible Turnstile，不点击，模拟人类行为等待 token...")
            _humanize(sb)
            _rand_sleep(2.0, 4.0)
            if _ts_solved(sb):
                return
            # 再等一轮
            _humanize(sb)
            _rand_sleep(2.0, 4.0)
            return
    except Exception as e:
        print(f"  ⚠️ invisible Turnstile 处理失败: {e}")

    # 策略3：切进可见的 Turnstile iframe，点击内部真实 checkbox
    if iframe_info and not iframe_info.get("hidden"):
        try:
            print(f"  📍 定位到可见 Turnstile iframe #{iframe_info['index']} ({iframe_info['w']}x{iframe_info['h']}) @({iframe_info['x']},{iframe_info['y']})")
            idx = iframe_info["index"]
            iframes = []
            try:
                iframes = sb.find_elements("iframe")
            except Exception:
                try:
                    from selenium.webdriver.common.by import By
                    iframes = sb.driver.find_elements(By.TAG_NAME, "iframe")
                except Exception:
                    pass
            if idx < len(iframes):
                sb.switch_to_frame(iframes[idx])
                _rand_sleep(0.4, 0.8)
                clicked_inside = False
                for cb_sel in ['input[type="checkbox"]', '#challenge-stage', '.rc-checkbox', 'body']:
                    try:
                        sb.wait_for_element_visible(cb_sel, timeout=3)
                        sb.uc_click(cb_sel)
                        print(f"  🖱️ 已点击 iframe 内元素: {cb_sel}")
                        clicked_inside = True
                        break
                    except Exception as ee:
                        print(f"    iframe 内 {cb_sel} 不可点: {ee}")
                        continue
                sb.switch_to_default_content()
                _rand_sleep(1.0, 2.0)
                if _ts_solved(sb):
                    return
                if clicked_inside:
                    return
            else:
                print(f"  ⚠️ iframe 索引 {idx} 越界(共{len(iframes)}个)")
        except Exception as e:
            print(f"  ⚠️ iframe 内点击失败: {e}")
        finally:
            try:
                sb.switch_to_default_content()
            except Exception:
                pass

    # 策略4：JS 点击可见 iframe 或父容器（仅对可见元素）
    try:
        js_ok = sb.execute_script("""
            (function() {
                var iframes = document.querySelectorAll('iframe');
                for (var i = 0; i < iframes.length; i++) {
                    var src = iframes[i].src || '';
                    if (src.includes('challenges.cloudflare.com')) {
                        var r = iframes[i].getBoundingClientRect();
                        if (r.width > 30 && r.height > 30) {
                            ['mousedown','mouseup','click'].forEach(function(t){
                                iframes[i].dispatchEvent(new MouseEvent(t, {
                                    bubbles: true, cancelable: true,
                                    clientX: r.x + r.width/2, clientY: r.y + r.height/2,
                                    view: window
                                }));
                            });
                            return true;
                        }
                    }
                }
                var inp = document.querySelector('input[name="cf-turnstile-response"]');
                if (inp) {
                    var p = inp.parentElement;
                    for (var j = 0; j < 15; j++) {
                        if (!p) break;
                        var r = p.getBoundingClientRect();
                        if (r.width > 80 && r.height > 25) {
                            ['mousedown','mouseup','click'].forEach(function(t){
                                p.dispatchEvent(new MouseEvent(t, {
                                    bubbles: true, cancelable: true,
                                    clientX: r.x + 30, clientY: r.y + r.height/2,
                                    view: window
                                }));
                            });
                            return true;
                        }
                        p = p.parentElement;
                    }
                }
                return false;
            })();
        """)
        if js_ok:
            print("  🖱️ 已用 JS 点击 Turnstile")
            _rand_sleep(1.0, 2.0)
            if _ts_solved(sb):
                return
    except Exception as e:
        print(f"  ⚠️ JS 点击 Turnstile 失败: {e}")

    # 策略5：回退到 xdotool 屏幕坐标（仅对可见元素）
    print("  🖱️ 回退到 xdotool 点击...")
    try:
        coords = sb.execute_script("""
            (function() {
                var iframes = document.querySelectorAll('iframe');
                for (var i = 0; i < iframes.length; i++) {
                    var src = iframes[i].src || '';
                    if (src.includes('challenges.cloudflare.com')) {
                        var r = iframes[i].getBoundingClientRect();
                        if (r.width > 30 && r.height > 30)
                            return {cx: Math.round(r.x + r.width/2), cy: Math.round(r.y + r.height/2)};
                    }
                }
                var inp = document.querySelector('input[name="cf-turnstile-response"]');
                if (inp) {
                    var p = inp.parentElement;
                    for (var j = 0; j < 15; j++) {
                        if (!p) break;
                        var r = p.getBoundingClientRect();
                        if (r.width > 80 && r.height > 25)
                            return {cx: Math.round(r.x + 30), cy: Math.round(r.y + r.height/2)};
                        p = p.parentElement;
                    }
                }
                return null;
            })();
        """)
        if not coords:
            print("  ⚠️ 无法定位可见 Turnstile 坐标")
            _dump_turnstile_state(sb, "无坐标")
            return
        wi = sb.execute_script(
            "(function(){ return {sx: window.screenX||0, sy: window.screenY||0,"
            "oh: window.outerHeight, ih: window.innerHeight, py: window.screenTop||window.screenY||0}; })();")
        bar = wi["oh"] - wi["ih"]
        ax = coords["cx"] + (wi.get("sx", 0) or 0)
        ay = coords["cy"] + (wi.get("sy", 0) or 0) + bar
        print(f"  🖱️ xdotool 点击 Turnstile ({ax}, {ay}) bar={bar} wi={wi}")
        _activate_win()
        _xclick(ax, ay)
        _rand_sleep(1.0, 2.0)
    except Exception as e:
        print(f"  ⚠️ xdotool 点击也失败: {e}")


def handle_turnstile(sb, current_url: str = "", no_refresh: bool = False,
                     on_retry: Optional[Callable[[], bool]] = None,
                     max_retry: int = 2, skip_wait: bool = False) -> bool:
    """处理 Turnstile 验证。
    current_url: 需要刷新重置时跳转的 URL（弹框模式请传空并设 no_refresh=True）
    no_refresh:  True 表示不刷新页面（用于弹框内，刷新会关闭弹框）
    on_retry:    当某次尝试失败需要重新触发时的回调函数，返回 True 表示已重新触发
    max_retry:   弹框模式下最多重触发弹框的次数（默认 2，避免短时高频点击被风控/封节点）

    注意：弹框模式下绝对不会刷新页面，也不会无限重触发弹框。达到 max_retry 上限
    后直接返回失败，交由上层决定是否放弃该服务器续期，绝不会要求重启工作流。
    如果 Turnstile 没有显示复选框（invisible/无需点击），本函数会等待其自动通过，
    不会强行点击空气导致流程卡死。"""
    print("🔍 处理 Turnstile 验证...")
    print(f"  [配置] no_refresh={no_refresh} max_retry={max_retry} 当前时间={now_str()}")
    t0 = time.time()
    # 先给 Turnstile 充分初始化时间，invisible 版本经常需要 5~10 秒才能出 token
    time.sleep(4)
    if _ts_solved(sb):
        print(f"  ✅ 已静默通过（耗时 {time.time()-t0:.1f}s）")
        return True
    for _ in range(3):
        sb.execute_script(_EXPAND_JS)
        _humanize(sb)
        time.sleep(1.0)

    # 关键：判断当前是否真的有可交互的 Turnstile 元素。
    # 有些情况下 Turnstile 不会显示复选框（token 已通过 invisible 方式生成），
    # 此时应等待自动通过，而不是反复点击空气。
    ts_interactive = _ts_exists(sb, label="handle可交互检测")
    if not ts_interactive:
        if skip_wait:
            # 上层已等满 3 分钟仍无 Turnstile：不再静默等待，直接进入重触发弹框流程
            print("  ⚠️ 已等满 3 分钟仍无 Turnstile 元素，跳过静默等待，进入重触发流程")
        else:
            print("  ℹ️ 未检测到可交互 Turnstile 元素，等待自动通过（不点击空气）...")
        # 最多等 ~150s（2.5 分钟），期间一旦通过立即返回
        for i in range(150):
            time.sleep(1.0)
            if _ts_solved(sb):
                print(f"  ✅ Turnstile 自动通过（耗时 {time.time()-t0:.1f}s）")
                return True
        # 仍然未通过：可能是 invisible 已出但还没写入，再轻点一次兜底
        print("  ⏳ 仍未通过，尝试一次兜底点击...")
        _click_turnstile(sb)
        for i in range(30):
            time.sleep(1.0)
            if _ts_solved(sb):
                print(f"  ✅ Turnstile 通过（兜底点击后，耗时 {time.time()-t0:.1f}s）")
                return True
        print(f"  ❌ Turnstile 未出现可交互元素且未自动通过（耗时 {time.time()-t0:.1f}s）")
        _dump_turnstile_state(sb, "最终")
        take_screenshot(sb, "turnstile_fail.png")
        return False

    retry_count = 0
    for attempt in range(6):
        if _ts_solved(sb):
            print(f"  ✅ Turnstile 通过（第{attempt+1}次，耗时 {time.time()-t0:.1f}s）")
            return True
        print(f"\n  🔄 Turnstile 第 {attempt+1}/6 次尝试... [{now_str()}]")
        sb.execute_script(_EXPAND_JS)
        _humanize(sb)
        time.sleep(0.8)
        _click_turnstile(sb)
        # 每次尝试后等待更久
        for _ in range(12):
            time.sleep(1.0)
            if _ts_solved(sb):
                print(f"  ✅ Turnstile 通过（第{attempt+1}次，耗时 {time.time()-t0:.1f}s）")
                return True
        print(f"  ⚠️ 第{attempt+1}次未通过（已等待）")

        # 如果指定了 no_refresh（弹框模式），通过回调重新触发弹框，而不是刷新页面
        if no_refresh and on_retry:
            if retry_count >= max_retry:
                print(f"  🛑 已达重触发弹框上限 max_retry={max_retry}，停止重试以避免触发风控/封节点")
                break
            retry_count += 1
            print(f"  🔄 第 {retry_count}/{max_retry} 次重新触发 Renew Server 弹框... [{now_str()}]")
            try:
                if on_retry():
                    print(f"    ✅ 弹框已重新触发（第{retry_count}次）")
                    # 等待弹框重新加载：最多 3 分钟。
                    # 注意：必须等真正的 Turnstile 元素/iframe 出现，或已静默通过(token)才停止等待；
                    # 不能因为看到 "Loading security verification" 就提前停止——那只是加载中，并非可处理。
                    waited = 0
                    print(f"    ⏳ 等待弹框 Turnstile 真正出现（最多 180s；Loading 中不算，出现才停）...")
                    for _ in range(180):
                        if (_ts_exists(sb, label=f"重触发第{retry_count}次") and
                                _ts_real_present(sb)) or _ts_solved(sb):
                            break
                        time.sleep(1.0)
                        waited += 1
                        # 每 30s 打印一次进度
                        if waited % 30 == 0:
                            print(f"      ...已等 {waited}s，Turnstile 尚未真正出现（仍在 Loading）")
                    print(f"    ⏳ 等待弹框 Turnstile 加载完成（{waited}s）")
                    time.sleep(2)
                else:
                    print(f"    ⚠️ 第{retry_count}次重新触发弹框失败")
            except Exception as e:
                print(f"    ⚠️ 重新触发弹框异常: {e}")
            continue

        # 普通页面模式：第3、5次刷新页面重新来
        if not no_refresh and current_url and attempt in [2, 4]:
            print("  🔄 刷新页面重置 Turnstile...")
            try:
                sb.uc_open_with_reconnect(current_url, reconnect_time=3)
                time.sleep(5)
                for _ in range(3):
                    sb.execute_script(_EXPAND_JS)
                    _humanize(sb)
                    time.sleep(1.0)
            except Exception as e:
                print(f"  ⚠️ 刷新失败: {e}")
    print(f"  ❌ Turnstile 验证失败（共尝试 6 次 + 重触发 {retry_count} 次，耗时 {time.time()-t0:.1f}s）")
    _dump_turnstile_state(sb, "最终")
    take_screenshot(sb, "turnstile_fail.png")
    return False


# ============================================================
#   关闭页面上的各类非必要弹窗/overlay/广告，避免干扰 Turnstile
# ============================================================
def close_all_popups(sb):
    """在关键步骤前清理：cookie consent、普通 dialog、广告 iframe、overlay/backdrop"""
    try:
        sb.execute_script("""
            (function() {
                var removed = 0;
                // 1) Google / OneTrust / CookieYes 等常见 CMP 弹窗
                var cmpSelectors = [
                    '[class*="cookie-consent"]', '[class*="cookieConsent"]', '[id*="cookie-consent"]',
                    '[id*="cookieConsent"]', '[class*="cmp-"]', '[id*="cmp-"]',
                    '[class*="fides-"]', '[class*="onetrust-"]', '[id*="onetrust-"]',
                    '[class*="truste_"]', '[class*="CybotCookiebotDialog"]', '[id*="CybotCookiebotDialog"]',
                    '[class*="privacy-settings"]', '[class*="privacySettings"]',
                    '[aria-label*="cookie" i]', '[aria-label*="privacy" i]'
                ];
                cmpSelectors.forEach(function(sel) {
                    document.querySelectorAll(sel).forEach(function(el) {
                        el.style.display = 'none'; el.remove(); removed++;
                    });
                });
                // 2) 通用 dialog / modal（带关闭按钮的先点关闭，再移除）
                var dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"], .modal, [class*="modal"], [class*="dialog"], [class*="Dialog"]');
                dialogs.forEach(function(d) {
                    // 如果这是 Renew Server 弹框本身，保留它
                    var title = (d.innerText || '').toLowerCase();
                    if (title.indexOf('renew server') !== -1 || title.indexOf('security verification') !== -1) {
                        return;
                    }
                    var closeBtn = d.querySelector('button[class*="close"], svg[class*="close"], [aria-label*="close" i], [class*="CloseButton"], button svg');
                    if (closeBtn) {
                        try { closeBtn.click(); } catch (e) {}
                    }
                    d.style.display = 'none'; d.remove(); removed++;
                });
                // 3) 非 Turnstile iframe 广告（如 Discover more 等）
                //    重要：若 iframe 位于 Renew Server 弹框内部（父级含 'renew server'/'security verification'），
                //    一律保留——即使此刻 src 还没变成 cloudflare/turnstile（Turnstile 正在 Loading 阶段）。
                document.querySelectorAll('iframe').forEach(function(f) {
                    // 向上找祖先，判断是否在续期弹框内
                    var inRenew = false;
                    var p = f.parentElement;
                    for (var k = 0; k < 6 && p; k++) {
                        var pt = (p.innerText || p.getAttribute('aria-label') || p.className || '').toLowerCase();
                        if (pt.indexOf('renew server') !== -1 || pt.indexOf('security verification') !== -1) {
                            inRenew = true; break;
                        }
                        p = p.parentElement;
                    }
                    if (inRenew) return;  // 续期弹框内的 iframe 全部保留，绝不误删
                    var src = (f.src || '').toLowerCase();
                    if (src.indexOf('cloudflare') === -1 && src.indexOf('turnstile') === -1) {
                        f.style.display = 'none'; f.remove(); removed++;
                    }
                });
                // 4) 固定定位的高 z-index overlay/backdrop（仅移除与续期弹框无关的）
                document.querySelectorAll('div').forEach(function(el) {
                    var style = window.getComputedStyle(el);
                    if (style.position === 'fixed' && parseInt(style.zIndex || 0) > 1000) {
                        var w = el.offsetWidth, h = el.offsetHeight;
                        // 如果它覆盖了大半个屏幕但不是 Renew Server 弹框，移除
                        if (w > window.innerWidth * 0.5 && h > window.innerHeight * 0.5) {
                            var t = (el.innerText || '').toLowerCase();
                            if (t.indexOf('renew server') !== -1 || t.indexOf('security verification') !== -1) {
                                return;  // 续期弹框本身，保留
                            }
                            // 向上检查祖先，避免误删续期弹框的遮罩层
                            var inRenew = false, ap = el.parentElement;
                            for (var k = 0; k < 6 && ap; k++) {
                                var apt = (ap.innerText || ap.className || '').toLowerCase();
                                if (apt.indexOf('renew server') !== -1 || apt.indexOf('security verification') !== -1) {
                                    inRenew = true; break;
                                }
                                ap = ap.parentElement;
                            }
                            if (!inRenew) {
                                el.style.display = 'none'; el.remove(); removed++;
                            }
                        }
                    }
                });
                // 5) 页面底部/侧边常见的固定广告条
                var adSelectors = [
                    '[class*="advertisement"]', '[class*="ad-"]', '[id*="ad-"]',
                    '[class*="banner"]', '[class*="toast"]', '[class*="snackbar"]'
                ];
                adSelectors.forEach(function(sel) {
                    document.querySelectorAll(sel).forEach(function(el) {
                        el.style.display = 'none'; el.remove(); removed++;
                    });
                });
                return removed;
            })();
        """)
    except Exception as e:
        print(f"  [close_all_popups] ⚠️ 清理异常: {e}")


# ============================================================
#   页面解析
# ============================================================
def get_time_left(sb) -> str:
    """读取服务器详情页的剩余时间"""
    try:
        sb.wait_for_element_visible("#nextRenewalTime", timeout=8)
        for _ in range(10):
            t = sb.get_text("#nextRenewalTime").strip()
            if t:
                return t
            time.sleep(0.5)
        print("  ⚠️ #nextRenewalTime 已加载但内容为空")
    except Exception as e:
        print(f"  ⚠️ 读取 #nextRenewalTime 失败: {e}")
    try:
        t = sb.execute_script("""
            (function() {
                var els = Array.from(document.querySelectorAll('*'));
                for (var i = 0; i < els.length; i++) {
                    var txt = els[i].innerText || '';
                    if (/\\d+\\s*day|\\d+h\\s*\\d+m|\\d+\\s*hour/.test(txt) && els[i].children.length === 0)
                        return txt.trim();
                }
                return '';
            })();
        """)
        if t:
            return t
    except:
        pass
    return ""


def click_renew_button(sb) -> bool:
    """尝试多种方式点击续期按钮"""
    # 先滚动到底部确保按钮可见
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)

    clicked = sb.execute_script("""
        (function() {
            // 方式1: span 文本含 Renew Server
            var spans = document.querySelectorAll('span');
            for (var i = 0; i < spans.length; i++) {
                if (spans[i].innerText && spans[i].innerText.includes('Renew Server')) {
                    var btn = spans[i].closest('button') || spans[i];
                    btn.scrollIntoView({block:'center'});
                    btn.click();
                    return 'span:Renew Server';
                }
            }
            // 方式2: button 文本含 Renew
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                var t = btns[i].innerText || '';
                if (t.includes('Renew')) {
                    btns[i].scrollIntoView({block:'center'});
                    btns[i].click();
                    return 'button:' + t.trim().substring(0,30);
                }
            }
            // 方式3: 任意元素文本含 Renew
            var all = document.querySelectorAll('[class*="renew"],[id*="renew"],[class*="Renew"],[id*="Renew"]');
            if (all.length > 0) {
                all[0].scrollIntoView({block:'center'});
                all[0].click();
                return 'attr:renew';
            }
            return null;
        })();
    """)

    if clicked:
        print(f"✅ 已点击续期按钮（方式: {clicked}）")
        return True

    # 方式4: SeleniumBase XPath 备用
    for xpath in [
        "//span[contains(text(),'Renew Server')]",
        "//button[contains(text(),'Renew')]",
        "//span[contains(text(),'Renew')]",
        "//*[contains(text(),'Renew Server')]",
    ]:
        try:
            sb.wait_for_element_visible(xpath, timeout=3)
            sb.click(xpath)
            print(f"✅ 已点击续期按钮（XPath: {xpath}）")
            return True
        except:
            continue

    return False


def _check_renew_result(sb):
    """检测续期弹窗/弹框结果：success / cooldown / None
    只认弹框（modal）内的结果文案：优先匹配"续期完成/成功"（中英文），
    其次冷却文案；弹框内存在 Close/OK 按钮作为结果已出的兜底信号。"""
    try:
        result = sb.execute_script("""
            (function() {
                // 成功文案（Zampto 实际弹框的中英文都可能出现，已做双语言覆盖）
                var OK_RE = /server has been renewed successfully|has been renewed|renewed successfully|server renewed|successfully renewed|renewal completed|续期完成|已成功续期|续期成功|延期成功|成功延长|服务器.*(?:已经|已).*续期|renew success/i;
                // 冷却/未到可续期时间文案
                var CD_RE = /cooldown|too soon|try again later|still on cooldown|not available yet|冷却|太早|未到.*时间|请.*再试/i;
                var modals = document.querySelectorAll('div[role="dialog"], div[role="alertdialog"], .modal, [class*="modal"], [class*="dialog"], [class*="Dialog"]');
                var modalText = '';
                var hasModal = false;
                for (var m = 0; m < modals.length; m++) {
                    var mt = (modals[m].innerText || '').trim();
                    if (!mt) continue;
                    hasModal = true;
                    modalText += mt + '\\n';
                }
                // 1) 弹框内成功文案（最高优先级：用户要求等"续期完成"出现）
                if (hasModal && OK_RE.test(modalText)) return 'success';
                // 2) 弹框内冷却文案
                if (hasModal && CD_RE.test(modalText)) return 'cooldown';
                // 3) 弹框内存在关闭/确定按钮 → 结果弹窗已加载完成
                if (hasModal) {
                    for (var m = 0; m < modals.length; m++) {
                        var mbtns = modals[m].querySelectorAll('button');
                        for (var i = 0; i < mbtns.length; i++) {
                            var txt = (mbtns[i].innerText || '').trim();
                            if (/close|ok|done|confirm|关闭|确定|完成/i.test(txt)) return 'success';
                        }
                    }
                }
                // 4) 兜底：整页文本匹配
                var allText = document.body.innerText || '';
                if (OK_RE.test(allText)) return 'success';
                if (CD_RE.test(allText)) return 'cooldown';
                return null;
            })();
        """)
        return result
    except:
        return None


def close_result_modal(sb):
    """关闭续期结果弹框：优先点击弹框内的 Close/OK/× 等按钮，兜底直接移除弹框。"""
    try:
        ok = sb.execute_script("""
            (function() {
                var modals = document.querySelectorAll('div[role="dialog"], div[role="alertdialog"], .modal, [class*="modal"], [class*="dialog"], [class*="Dialog"]');
                // 1) 优先点按钮
                for (var m = 0; m < modals.length; m++) {
                    var btns = modals[m].querySelectorAll('button, [role="button"], svg[class*="close"], [aria-label*="close" i], [class*="close" i]');
                    for (var i = 0; i < btns.length; i++) {
                        var el = btns[i];
                        var t = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '') + ' ' + (el.className || '')).toLowerCase();
                        if (/close|ok|done|confirm|cancel|关闭|确定|完成|退出|×|✕|✖/i.test(t)) {
                            try { el.click(); return 'click:' + t.trim().substring(0, 30); } catch(e) {}
                        }
                    }
                }
                // 2) 兜底：移除与续期结果相关的弹框
                for (var m = 0; m < modals.length; m++) {
                    var t = (modals[m].innerText || '').toLowerCase();
                    if (/renew|续期|成功|success|renewed|completed|cooldown|冷却/i.test(t)) {
                        modals[m].remove();
                        return 'removed:' + t.trim().substring(0, 30);
                    }
                }
                // 3) 再兜底：移除所有 dialog（仅当存在时）
                for (var m = 0; m < modals.length; m++) {
                    modals[m].remove();
                }
                return modals.length ? 'removed-all' : 'no-modal';
            })();
        """)
        print(f"  🧹 关闭结果弹框: {ok}")
        _rand_sleep(0.5, 1.0)
    except Exception as e:
        print(f"  ⚠️ 关闭弹框异常（可忽略）: {e}")


def _modal_has_turnstile(sb) -> bool:
    """检测续期弹框是否出现 Turnstile（含 Loading security verification 阶段）"""
    try:
        info = sb.execute_script("""
            (function() {
                var input = document.querySelector('input[name="cf-turnstile-response"]');
                var frames = document.querySelectorAll('iframe');
                var cfFrame = false;
                for (var i = 0; i < frames.length; i++) {
                    if ((frames[i].src || '').indexOf('challenges.cloudflare.com') !== -1) cfFrame = true;
                }
                var bodyText = (document.body.innerText || '').toLowerCase();
                var loading = bodyText.indexOf('loading security verification') !== -1 ||
                              bodyText.indexOf('complete the security verification') !== -1 ||
                              bodyText.indexOf('security verification') !== -1;
                return {hasInput: !!input, cfFrame: cfFrame, loading: loading,
                        tokenOk: !!(input && input.value && input.value.length > 20)};
            })();
        """)
        if info.get("tokenOk"):
            return False  # token 已生成，无需处理
        return bool(info.get("hasInput") or info.get("cfFrame") or info.get("loading"))
    except Exception:
        return False


def _handle_modal_turnstile(sb):
    """轻量处理续期弹框内的 Turnstile：检测到就点击一次并等待 token 生成。
    不做复杂重试/重触发弹框；token 未生成也不阻塞主流程。"""
    print("  🖱️ 检测到弹框 Turnstile（Loading security verification），轻量点击处理...")
    _click_turnstile(sb)
    for i in range(60):  # 最多等 60s token
        if _ts_solved(sb):
            print(f"  ✅ 弹框 Turnstile token 已生成（{i+1}s）")
            return
        time.sleep(1)
    print("  ⚠️ 弹框 Turnstile token 未在 60s 内生成，继续等待结果弹框")


# ============================================================
#   单个服务器续期
# ============================================================
def renew_server(sb, server: dict) -> bool:
    sid = server["id"]
    name = server["name"]
    server_url = f"https://{DOMAIN}/server?id={sid}"
    prefix = f"server_{sid}"

    print("-" * 40)
    print(f"🖥️  续期: {name}  (id={sid})")
    print("-" * 40)

    # ① 直接打开服务器详情页
    print(f"🌐 访问: {server_url}  [{now_str()}]")
    sb.uc_open_with_reconnect(server_url, reconnect_time=4)
    time.sleep(4)
    take_screenshot(sb, f"{prefix}_loaded.png")

    # ② 读取当前剩余时间
    time_left = get_time_left(sb)
    print(f"⏱️  当前剩余时间: {time_left or '未读取到'}")

    # ②B 清理页面上的非必要弹窗/广告，避免遮挡续期按钮或结果弹框
    print("🧹 清理页面弹窗、cookie consent、广告条...")
    close_all_popups(sb)

    # ③ 点击续期按钮
    print("🔍 查找续期按钮...")
    if not click_renew_button(sb):
        # 打印页面所有按钮帮助调试
        btns = sb.execute_script("""
            (function() {
                return Array.from(document.querySelectorAll('button,span')).map(function(b){
                    return b.innerText.trim();
                }).filter(function(t){ return t.length > 0 && t.length < 60; });
            })();
        """)
        print(f"  页面按钮/span: {btns}")
        take_screenshot(sb, f"{prefix}_no_btn.png")
        send_tg(f"🖥 {name}\n❌ 未找到续期按钮\n时间: {now_str()}")
        return False

    time.sleep(3)
    take_screenshot(sb, f"{prefix}_after_click.png")

    # ④ 等待弹出续期结果（弹框内若出现 Turnstile / Loading security verification，
    #    做一次轻量点击处理等 token；不做复杂重试/重触发）。
    #    直到弹框内出现"续期完成/成功"或"冷却中"文案为止；最多等 180s。
    print(f"⏳ 等待续期结果弹框（最多 180s，弹框 Turnstile 轻量处理）... [{now_str()}]")
    start = time.time()
    final_status = "unknown"
    ts_handled = False
    while time.time() - start < 180:
        # 弹框 Turnstile 只轻量处理一次（点击 + 等 token）
        if not ts_handled and _modal_has_turnstile(sb):
            _handle_modal_turnstile(sb)
            ts_handled = True
        r = _check_renew_result(sb)
        if r == "success":
            print(f"🎉 弹框已出现续期完成文案（耗时 {time.time()-start:.1f}s）")
            final_status = "success"
            break
        if r == "cooldown":
            print(f"ℹ️ 弹框显示冷却中（耗时 {time.time()-start:.1f}s）")
            final_status = "cooldown"
            break
        elapsed = int(time.time() - start)
        if elapsed > 0 and elapsed % 15 == 0:
            print(f"    ...已等 {elapsed}s，弹框仍在加载/未出现结果")
        time.sleep(1)

    if final_status == "unknown":
        print("⚠️ 180s 内未检测到明确结果弹框，按未知处理，仍会读取剩余时长")
    time.sleep(1)
    take_screenshot(sb, f"{prefix}_modal_result.png")

    # ⑤ 关闭结果弹框，再刷新页面读取新的剩余续期时长
    print("🧹 关闭结果弹框...")
    close_result_modal(sb)

    print("🔄 刷新页面确认剩余时间...")
    sb.uc_open_with_reconnect(server_url, reconnect_time=3)
    time.sleep(4)
    new_time = get_time_left(sb)
    print(f"⏱️  续期后剩余时间: {new_time or '未读取到'}")
    take_screenshot(sb, f"{prefix}_final.png")

    if final_status == "success":
        send_tg(f"🖥 {name}\n✅ 续期完成\n⏱️ 剩余: {new_time or '未知'}\n时间: {now_str()}")
    elif final_status == "cooldown":
        send_tg(f"🖥 {name}\nℹ️ 冷却中，无需续期\n⏱️ 剩余: {new_time or '未知'}\n时间: {now_str()}")
    else:
        send_tg(f"🖥 {name}\n⚠️ 未检测到明确续期结果，请查看截图\n⏱️ 剩余: {new_time or '未知'}\n时间: {now_str()}")
    return True


# ============================================================
#   登录（邮箱 + 密码同一页，单页提交）
# ============================================================
def _is_logged_in_url(url: str) -> bool:
    lower_url = url.lower()
    logged_in_paths = ["/homepage", "/dashboard", "/home", "/console", "/servers", "/overview", "/main"]
    auth_paths = ["/auth/login", "/auth/signin", "/sign-in", "/login", "/register"]
    if any(p in lower_url for p in logged_in_paths):
        return True
    if DOMAIN in lower_url and not any(p in lower_url for p in auth_paths):
        return True
    return False


def _wait_login_turnstile(sb, label: str, timeout: int = 8) -> bool:
    """等待并处理登录页出现的 Turnstile，最多处理一次。"""
    print(f"🔍 {label} Turnstile 检测中（最多等 {timeout}s）...")
    for i in range(timeout * 2):
        if _ts_exists(sb, label):
            print(f"🔍 {label} 检测到 Turnstile，处理中...")
            if not handle_turnstile(sb, current_url="", no_refresh=True, max_retry=0):
                take_screenshot(sb, f"login_{label.replace(' ', '_')}_ts_fail.png")
                send_tg(f"❌ {label} Turnstile 验证失败\n时间: {now_str()}")
                return False
            print(f"✅ {label} Turnstile 处理完成")
            return True
        time.sleep(0.5)
    print(f"  ℹ️ {label} 未出现 Turnstile")
    return True


def do_login(sb) -> bool:
    print(f"🚀 访问登录页: {LOGIN_URL}")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=4)
    time.sleep(3)

    # 登录页 Turnstile（邮箱+密码同一页，只检测这一次）
    if not _wait_login_turnstile(sb, "登录页", timeout=10):
        return False

    # 输入账号（邮箱）
    print("⌨️  输入账号...")
    account_selector = None
    for sel in ['input[name="identifier"]', 'input[name="email"]', 'input[type="email"]', 'input#email', 'input[inputmode="email"]']:
        if sb.is_element_present(sel):
            account_selector = sel
            print(f"   使用账号输入框: {sel}")
            break
    if not account_selector:
        print("❌ 未找到邮箱/账号输入框 (identifier/email)")
        take_screenshot(sb, "login_fail.png")
        return False
    try:
        sb.wait_for_element_visible(account_selector, timeout=15)
        sb.type(account_selector, ZAMPTO_ACCOUNT)
    except Exception as e:
        print(f"❌ 账号输入失败: {e}")
        take_screenshot(sb, "login_fail.png")
        return False

    # 同一页输入密码
    print("⌨️  输入密码...")
    pw_selector = None
    for sel in ['input[name="password"]', 'input[type="password"]', 'input#password']:
        if sb.is_element_present(sel):
            pw_selector = sel
            print(f"   使用密码输入框: {sel}")
            break
    if not pw_selector:
        print("❌ 未找到密码输入框")
        take_screenshot(sb, "login_fail.png")
        return False
    try:
        sb.wait_for_element_visible(pw_selector, timeout=15)
        sb.type(pw_selector, ZAMPTO_PASSWORD)
    except Exception as e:
        print(f"❌ 密码输入失败: {e}")
        take_screenshot(sb, "login_fail.png")
        return False

    # 提交按钮多选择器兼容
    submit_ok = False
    for submit_sel in ['button[name="submit"]', 'button[type="submit"]', 'input[type="submit"]', 'button:contains("Sign in")', 'button:contains("Log in")', 'button:contains("登录")']:
        if sb.is_element_present(submit_sel):
            sb.click(submit_sel)
            submit_ok = True
            print(f"   点击提交按钮: {submit_sel}")
            break
    if not submit_ok:
        # 兜底：直接回车
        sb.type(pw_selector, "\n")
        print("   未找到提交按钮，使用回车提交")

    # 提交后等待跳转：先给 5s 正常跳转，若仍在登录页再处理一次 Turnstile
    print("⏳ 等待跳转登录成功...")
    ts_handled_after_submit = False
    for i in range(60):
        try:
            url = sb.get_current_url()
            if _is_logged_in_url(url):
                print(f"✅ 登录成功: {url}")
                return True

            # 5s 后仍卡在登录页，且检测到 Turnstile，处理一次（不刷新）
            if i >= 10 and not ts_handled_after_submit and _ts_exists(sb, "提交后"):
                print(f"🔍 提交后仍停留在登录页，检测到 Turnstile，处理中...")
                if handle_turnstile(sb, current_url="", no_refresh=True, max_retry=0):
                    ts_handled_after_submit = True
                    print("✅ 提交后 Turnstile 处理完成，继续等待跳转...")
                else:
                    print("❌ 提交后 Turnstile 处理失败")
                    take_screenshot(sb, "login_after_submit_ts_fail.png")
                    send_tg(f"❌ 登录提交后 Turnstile 验证失败\n时间: {now_str()}")
                    return False
        except Exception:
            pass
        time.sleep(0.5)

    print("❌ 登录超时")
    take_screenshot(sb, "login_timeout.png")
    return False


# ============================================================
#   主流程
# ============================================================
def _preflight():
    """运行前先探测登录页是否可达，避免卡在浏览器里浪费时间"""
    print("🔎 预检：探测登录页连通性...")
    test_url = LOGIN_URL if LOGIN_URL else f"https://{DOMAIN}/auth/login"
    try:
        r = requests.get(test_url, timeout=20, proxies=_proxies(),
                         headers={"User-Agent": "Mozilla/5.0"})
        print(f"  HTTP {r.status_code}  {test_url}")
        if r.status_code >= 500:
            print("  ❌ 服务端/网关错误（如 522），登录页不可达")
            return False
        if r.status_code >= 400:
            print("  ⚠️ 返回 4xx，可能需带正确参数，但服务可达")
        return True
    except Exception as e:
        print(f"  ❌ 登录页无法访问: {e}")
        return False


def main():
    print("=" * 40)
    print("   Zampto Auto Renew")
    print("=" * 40)

    # 预检登录页连通性
    if not _preflight():
        send_tg(f"❌ Zampto 登录页不可达（{LOGIN_URL}）\n时间: {now_str()}")
        raise SystemExit("❌ 登录页不可达，请检查 LOGIN_URL / 代理 / 网络")

    # 代理可选：SeleniumBase 的 proxy= 在 uc=True 下不生效，必须改用 --proxy-server 启动参数
    sb_proxy = LOCAL_PROXY if LOCAL_PROXY else None

    with SB(uc=True, test=True,
            chromium_arg=f"--proxy-server={sb_proxy}" if sb_proxy else None) as sb:

        print("🌐 检测出口 IP...")
        try:
            sb.open("https://api.ipify.org/?format=json")
            print(f"✅ 出口 IP: {sb.get_text('body')}")
        except Exception:
            print("⚠️ IP 检测超时，代理可能未生效")
        print("-" * 40)
        if not do_login(sb):
            send_tg(f"❌ Zampto 登录失败\n时间: {now_str()}")
            return

        time.sleep(3)
        print("-" * 40)

        results = {}
        for idx, server in enumerate(TARGET_SERVERS):
            print(f"\n### 进度 {idx+1}/{len(TARGET_SERVERS)}: {server['name']} ### [{now_str()}]")
            try:
                results[server["id"]] = renew_server(sb, server)
            except Exception as e:
                # 单个服务器异常不中断整体，避免连锁失败 + 无谓重启
                print(f"❌ 续期 {server['name']} 抛出异常: {e}")
                results[server["id"]] = False
                send_tg(f"🖥 {server['name']}\n❌ 续期过程异常: {str(e)[:200]}\n时间: {now_str()}")
                try:
                    take_screenshot(sb, f"server_{server['id']}_exception.png")
                except Exception:
                    pass
            # 服务器之间有间隔，避免短时高频请求触发风控
            _rand_sleep(1.5, 3.0)

        print("=" * 40)
        print("📊 续期结果汇总：")
        ok_count = 0
        for s in TARGET_SERVERS:
            status = "🎉 成功" if results[s["id"]] else "❌ 失败"
            if results[s["id"]]:
                ok_count += 1
            print(f"  {s['name']}: {status}")
        print(f"  ── 共 {len(TARGET_SERVERS)} 台，成功 {ok_count} 台")
        print("=" * 40)
        print("👋 完成（本次运行已尽量自动重试，无需重启工作流）")
        if ok_count < len(TARGET_SERVERS):
            send_tg(f"📊 续期汇总：{ok_count}/{len(TARGET_SERVERS)} 成功\n"
                    f"失败的服务器请查看日志与截图，无需重启工作流，下次定时运行会自动再试\n时间: {now_str()}")


if __name__ == "__main__":
    main()
