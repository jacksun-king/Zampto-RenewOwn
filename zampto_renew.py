#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import os
import json
import requests
import subprocess
from datetime import datetime
from seleniumbase import SB

# ============================================================
#   ⚙️ 配置（全部从环境变量读取，适配 akimify/Zampto-Renew 的 GitHub Actions 工作流）
# ============================================================
# ZAMPTO_ACCOUNT: 你的 Zampto 登录邮箱
ZAMPTO_ACCOUNT = os.environ.get("ZAMPTO_ACCOUNT", "")

# ZAMPTO_PASSWORD: 你的 Zampto 登录密码（工作流未明确该变量名，这里兼容两种命名）
ZAMPTO_PASSWORD = os.environ.get("ZAMPTO_PASSWORD", "") or os.environ.get("PASSWORD", "")

# TG_BOT: Telegram 通知，支持两种格式
#   1) "token:chatid"        （与工作流 secrets.TG_BOT 对应）
#   2) 分别设置 TG_TOKEN / TG_ID
_TG_BOT = os.environ.get("TG_BOT", "")
if _TG_BOT and ":" in _TG_BOT:
    TG_TOKEN, TG_ID = _TG_BOT.split(":", 1)
else:
    TG_TOKEN = os.environ.get("TG_TOKEN", "")
    TG_ID = os.environ.get("TG_ID", "")

# GOST_PROXY: 工作流启动 GOST 后本地 8080 代理地址；为空则不使用代理
GOST_PROXY = os.environ.get("GOST_PROXY", "")
LOCAL_PROXY = "http://127.0.0.1:8080" if GOST_PROXY else ""

# LOGIN_URL: Zampto 登录页地址；未设置或为空时使用默认地址
#   根据当前面板，默认登录页为 /auth/login
LOGIN_URL = os.environ.get("LOGIN_URL", "") or "https://dash.zampto.net/auth/login"

# ZAMPTO_APP_ID: 可选，登录 URL 上的 app_id 参数；如果设置且 LOGIN_URL 未手动指定，
#   会在默认地址后追加 ?app_id=...。当前 dash.zampto.net 用不到，保持兼容。
ZAMPTO_APP_ID = os.environ.get("ZAMPTO_APP_ID", "")
if ZAMPTO_APP_ID and LOGIN_URL == "https://dash.zampto.net/auth/login":
    LOGIN_URL = f"{LOGIN_URL}?app_id={ZAMPTO_APP_ID}"

DOMAIN = os.environ.get("ZAMPTO_DOMAIN", "") or "dash.zampto.net"

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
        return
    url = (f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
           f"?chat_id={TG_ID}&text={requests.utils.quote(msg)}")
    try:
        requests.get(url, timeout=10, proxies=_proxies())
        print("✅ Telegram 消息已发送")
    except Exception as e:
        print(f"⚠️ TG 通知失败: {e}")


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


def _ts_exists(sb):
    try:
        return bool(sb.execute_script(
            "return (function(){ return document.querySelector('input[name=\"cf-turnstile-response\"]') !== null; })();"))
    except:
        return False


def _ts_solved(sb):
    try:
        return bool(sb.execute_script(
            "return (function(){ var i=document.querySelector('input[name=\"cf-turnstile-response\"]');"
            "return !!(i && i.value && i.value.length > 20); })();"))
    except:
        return False


def _activate_win():
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
    _activate_win()
    try:
        subprocess.run(["xdotool", "mousemove", str(x), str(y)], timeout=2, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")


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
    对隐藏式 invisible Turnstile 不强行点击，而是用人类化行为等待 token 自动生成。"""
    _dump_turnstile_state(sb, "点击前")

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
                    if (src.includes('challenges.cloudflare.com') || src.includes('turnstile')) {
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
                    if (src.includes('challenges.cloudflare.com') || src.includes('turnstile')) {
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
                    if (src.includes('challenges.cloudflare.com') || src.includes('turnstile')) {
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
            "return (function(){ return {sx: window.screenX||0, sy: window.screenY||0,"
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


def handle_turnstile(sb, current_url: str = "") -> bool:
    print("🔍 处理 Turnstile 验证...")
    # 先给 Turnstile 充分初始化时间，invisible 版本经常需要 5~10 秒才能出 token
    time.sleep(4)
    if _ts_solved(sb):
        print("  ✅ 已静默通过")
        return True
    for _ in range(3):
        sb.execute_script(_EXPAND_JS)
        _humanize(sb)
        time.sleep(1.0)
    for attempt in range(6):
        if _ts_solved(sb):
            print(f"  ✅ Turnstile 通过（第{attempt+1}次）")
            return True
        print(f"\n  🔄 Turnstile 第 {attempt+1}/6 次尝试...")
        sb.execute_script(_EXPAND_JS)
        _humanize(sb)
        time.sleep(0.8)
        _click_turnstile(sb)
        # 每次尝试后等待更久
        for _ in range(12):
            time.sleep(1.0)
            if _ts_solved(sb):
                print(f"  ✅ Turnstile 通过（第{attempt+1}次）")
                return True
        print(f"  ⚠️ 第{attempt+1}次未通过")
        # 若还有 URL 且未解决，第3、5次刷新页面重新来
        if current_url and attempt in [2, 4]:
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
    print("  ❌ Turnstile 6次均失败")
    _dump_turnstile_state(sb, "最终")
    take_screenshot(sb, "turnstile_fail.png")
    return False


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
    except:
        pass
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
    """检测续期弹窗结果：success / cooldown / None"""
    try:
        return sb.execute_script("""
            (function() {
                var t = document.body.innerText || '';
                if (t.includes('cooldown') || t.includes('too soon') || t.includes('wait')) return 'cooldown';
                if (t.includes('success') || t.includes('Success') || t.includes('renewed')) return 'success';
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    if (btns[i].innerText.includes('Close') || btns[i].innerText.includes('OK'))
                        return 'success';
                }
                return null;
            })();
        """)
    except:
        return None


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
    print(f"🌐 访问: {server_url}")
    sb.uc_open_with_reconnect(server_url, reconnect_time=4)
    time.sleep(4)
    take_screenshot(sb, f"{prefix}_loaded.png")

    # ② 读取当前剩余时间
    time_left = get_time_left(sb)
    print(f"⏱️  当前剩余时间: {time_left or '未读取到'}")

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

    # ④ 等待 Turnstile
    print("⏳ 等待 Turnstile...")
    ts_found = False
    for _ in range(20):
        if _ts_exists(sb):
            print("✅ 检测到 Turnstile")
            ts_found = True
            break
        # 提前检查是否已出现结果弹窗
        r = _check_renew_result(sb)
        if r:
            print(f"ℹ️  点击后直接出现结果: {r}")
            break
        time.sleep(1)

    if not ts_found and not _ts_exists(sb):
        r = _check_renew_result(sb)
        if r == "success":
            print("🎉 续期成功（无需 Turnstile）！")
            time_left = get_time_left(sb)
            send_tg(f"🖥 {name}\n✅ 续期成功\n⏱️ 剩余: {time_left}\n时间: {now_str()}")
            return True
        print("❌ Turnstile 未出现")
        take_screenshot(sb, f"{prefix}_no_turnstile.png")
        send_tg(f"🖥 {name}\n❌ Turnstile 未出现\n时间: {now_str()}")
        return False

    # ⑤ 处理 Turnstile
    if not handle_turnstile(sb, server_url):
        take_screenshot(sb, f"{prefix}_ts_fail.png")
        send_tg(f"🖥 {name}\n❌ Turnstile 验证失败\n时间: {now_str()}")
        return False

    # ⑥ 等待提交结果
    print("⏳ 等待续期结果...")
    start = time.time()
    while time.time() - start < 30:
        r = _check_renew_result(sb)
        if r == "success":
            print("🎉 检测到成功结果！")
            break
        if r == "cooldown":
            print("⏳ 冷却期内")
            break
        time.sleep(1)

    time.sleep(2)
    take_screenshot(sb, f"{prefix}_result.png")

    # ⑦ 刷新页面读取新时间
    print("🔄 刷新页面确认剩余时间...")
    sb.uc_open_with_reconnect(server_url, reconnect_time=3)
    time.sleep(4)
    new_time = get_time_left(sb)
    print(f"⏱️  续期后剩余时间: {new_time or '未读取到'}")
    take_screenshot(sb, f"{prefix}_final.png")

    send_tg(f"🖥 {name}\n✅ 续期完成\n⏱️ 剩余: {new_time or '未知'}\n时间: {now_str()}")
    return True


# ============================================================
#   登录（对齐 wode808 的两步 identifier 流程）
# ============================================================
def do_login(sb) -> bool:
    print(f"🚀 访问登录页: {LOGIN_URL}")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=4)
    time.sleep(3)

    # 登录页可能有 Turnstile
    for _ in range(10):
        time.sleep(0.5)
        if _ts_exists(sb):
            break
    if _ts_exists(sb):
        print("🔍 登录页检测到 Turnstile，处理中...")
        if not handle_turnstile(sb, LOGIN_URL):
            take_screenshot(sb, "login_ts_fail.png")
            return False

    # 第一步：输入 identifier（邮箱），提交
    print("⌨️  输入账号...")
    try:
        sb.wait_for_element_visible('input[name="identifier"]', timeout=15)
        sb.type('input[name="identifier"]', ZAMPTO_ACCOUNT)
        sb.click('button[type="submit"]')
    except Exception as e:
        print(f"❌ 账号输入失败: {e}")
        take_screenshot(sb, "login_fail.png")
        return False

    # 等待密码页
    print("⏳ 等待密码页...")
    try:
        sb.wait_for_element_visible('input[name="password"]', timeout=15)
    except Exception:
        print("❌ 密码页未出现")
        take_screenshot(sb, "password_page_fail.png")
        return False

    # 密码页可能有 Turnstile
    for _ in range(10):
        time.sleep(0.5)
        if _ts_exists(sb):
            break
    if _ts_exists(sb):
        print("🔍 密码页检测到 Turnstile，处理中...")
        if not handle_turnstile(sb, LOGIN_URL):
            take_screenshot(sb, "password_ts_fail.png")
            return False

    # 第二步：输入密码，提交
    print("⌨️  输入密码...")
    sb.type('input[name="password"]', ZAMPTO_PASSWORD)
    sb.click('button[name="submit"]')

    print("⏳ 等待跳转登录成功...")
    for _ in range(60):
        try:
            url = sb.get_current_url()
            # wode808 实测：登录成功后跳转到 /homepage
            if "/homepage" in url:
                print(f"✅ 登录成功: {url}")
                return True
            # 兼容不带 /homepage 的情况：在 DOMAIN 内且已离开登录页
            if ZAMPTO_APP_ID and "/sign-in" not in url and DOMAIN in url:
                print(f"✅ 登录成功（已离开登录页）: {url}")
                return True
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

    # 代理可选：仅在设置了 GOST_PROXY 时才走本地 8080
    sb_proxy = LOCAL_PROXY if LOCAL_PROXY else None

    with SB(uc=True, test=True, proxy=sb_proxy) as sb:

        print("🌐 检测出口 IP...")
        try:
            sb.open("https://api.ipify.org/?format=json")
            print(f"✅ 出口 IP: {sb.get_text('body')}")
        except:
            print("⚠️ IP 检测超时")
        print("-" * 40)

        if not do_login(sb):
            send_tg(f"❌ Zampto 登录失败\n时间: {now_str()}")
            return

        time.sleep(3)
        print("-" * 40)

        results = {}
        for server in TARGET_SERVERS:
            results[server["id"]] = renew_server(sb, server)

        print("=" * 40)
        print("📊 续期结果汇总：")
        for s in TARGET_SERVERS:
            status = "🎉 成功" if results[s["id"]] else "❌ 失败"
            print(f"  {s['name']}: {status}")
        print("=" * 40)
        print("👋 完成")


if __name__ == "__main__":
    main()
