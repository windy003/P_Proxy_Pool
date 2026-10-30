"""
通过 HTTP 代理访问目标站点，把「代理真实出口 IP」和「站点记录的来源 IP」逐一对照。

一次对照 = 同一个出口 IP 上依次打三个请求：
    1. IP_CHECK_URL       查代理真实出口 IP（真值，第三方视角）
    2. 站点 /api/ip       站点自己算出来的客户端 IP（visits 表里记的就是它）
    3. 站点目标页面        带浏览器 UA，会被计入访问统计

第 1 步和第 2 步相等，说明站点取 IP 的链路是通的，此时第 3 步写进 visits 的也是
这个 IP，去后台「24小时独立IP」按访问时间就能对上。

隧道代理默认每次新连接换一个出口 IP，所以这里用粘性会话（在密码后追加
:会话ID，30 秒内固定同一个出口 IP），否则三步会各走各的 IP，对照就没有意义。

用法：
    1. Copy-Item .env.example .env
    2. 在 .env 里填 PROXY_URL / TARGET_URL
    3. python proxy-test.py
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

IP_CHECK_URL = "https://myip.ipip.net"
DEFAULT_ENV = Path(__file__).resolve().with_name(".env")

# 目标站点会把 python-requests/curl/wget 这类 UA 当爬虫过滤掉（不计入访问统计），
# 所以要核对后台 IP 记录时，必须伪装成正常浏览器
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# myip.ipip.net 返回形如「当前 IP：223.123.34.132  来自于：巴基斯坦 旁遮普省   zong.com.pk」
IP_TEXT_PATTERN = re.compile(r'IP\s*[：:]\s*([0-9a-fA-F:.]+)')


def parse_ip(text: str) -> str:
    match = IP_TEXT_PATTERN.search(text or '')
    return match.group(1) if match else ''


def build_echo_url(target_url: str) -> str:
    """目标站点的 IP 回显接口：同源 + /api/ip（就是 app.py 里的 api_client_ip）。"""
    parts = urlsplit(target_url)
    return urlunsplit((parts.scheme, parts.netloc, '/api/ip', '', ''))


def with_session(proxy_url: str, session: str):
    """给隧道代理加粘性会话：把 :会话ID 追加到密码后面（host 不变）。

    快代理隧道代理的约定是「用户名:密码:会话ID」，同一个会话 ID 的请求在 30 秒内
    固定走同一个出口 IP。这里直接按最后一个 @ 切分，避免动到已经 URL 编码过的凭据。
    IP 白名单认证的代理没有用户名密码，返回 None 表示用不了粘性会话。
    """
    if '@' not in proxy_url:
        return None
    head, tail = proxy_url.rsplit('@', 1)
    return f'{head}:{session}@{tail}'


def make_proxies(url: str):
    return {'http': url, 'https': url}


def check_exit_ip(proxies) -> str:
    resp = requests.get(IP_CHECK_URL, proxies=proxies, timeout=30)
    return parse_ip(resp.text)


def fetch_site_ip(echo_url: str, proxies, ua: str) -> str:
    """读站点 /api/ip。站点没这个接口（或不是本项目）时返回空串，不让整体失败。"""
    resp = requests.get(echo_url, proxies=proxies, timeout=30,
                        headers={'User-Agent': ua})
    if resp.status_code != 200:
        return ''
    try:
        return (resp.json() or {}).get('ip', '') or ''
    except ValueError:
        return ''


def main() -> int:
    ap = argparse.ArgumentParser(description="代理出口 IP 与站点记录 IP 对照测试")
    ap.add_argument("--count", type=int, default=5, help="对照轮数（每轮访问一次目标站点）")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV), help=".env 文件路径")
    args = ap.parse_args()

    # utf-8-sig：兼容 Windows 上用 PowerShell/记事本存出来的带 BOM 的 .env
    load_dotenv(Path(args.env_file), override=False, encoding="utf-8-sig")

    for key in ("PROXY_URL", "TARGET_URL"):
        if not os.environ.get(key):
            sys.exit(f"请在 .env 里设置 {key}")

    proxy_url = os.environ["PROXY_URL"].strip()
    target_url = os.environ["TARGET_URL"].strip()
    echo_url = build_echo_url(target_url)

    # requests 的代理认证只支持 latin-1，中文占位符或未编码的特殊字符会在这里报错
    try:
        proxy_url.encode("latin-1")
    except UnicodeEncodeError:
        sys.exit("PROXY_URL 里有非 ASCII 字符（占位符还没替换？）。请填真实凭据，用户名密码含特殊字符时要做 URL 编码。")

    # ---- 预检：先确认代理本身可用，失败时给出能看懂的提示 ----
    print("=== 代理连通性预检 ===")
    sticky = with_session(proxy_url, "probe")
    sticky_ok = sticky is not None
    preflight_url = sticky or proxy_url
    try:
        print("  " + check_exit_ip(make_proxies(preflight_url)))
    except requests.exceptions.ProxyError as e:
        if sticky_ok:
            # 有些套餐不认粘性会话后缀，认证会被判失败：退回普通模式继续，只是没法严格对照
            sticky_ok = False
            print("  (粘性会话不可用，退回普通模式) " + str(e))
            try:
                print("  " + check_exit_ip(make_proxies(proxy_url)))
            except Exception as e2:
                sys.exit(f"  代理不可用：{e2}")
        else:
            sys.exit(
                f"  代理不可用：{e}\n"
                "  请检查 .env 里的 PROXY_URL，以及快代理后台的 IP 白名单是否包含本机公网 IP"
            )
    except Exception as e:
        sys.exit(f"  代理不可用：{e}")

    if not sticky_ok:
        print("  注意：没有可用的粘性会话，出口 IP 每次连接都会变，"
              "对照结果可能出现「代理出口 ≠ 站点记录」，属正常现象。")

    # ---- 逐轮对照 ----
    print(f"\n=== IP 对照：{target_url} ===")
    print(f"    回显接口：{echo_url}")
    matched = 0
    for i in range(1, args.count + 1):
        # 每轮换一个会话 ID，这样轮与轮之间还是拿到不同的出口 IP
        session = sticky_ok and with_session(proxy_url, f'jd{i}{int(time.time()) % 100000}')
        proxies = make_proxies(session or proxy_url)

        try:
            true_ip = check_exit_ip(proxies)
        except Exception as e:
            print(f"  [{i}] 取出口 IP 失败：{e}")
            continue

        try:
            site_ip = fetch_site_ip(echo_url, proxies, BROWSER_UA)
        except Exception as e:
            site_ip = ''
            note = f'(读 /api/ip 失败：{e})'
        else:
            note = ''

        try:
            resp = requests.get(target_url, proxies=proxies, timeout=45,
                                headers={'User-Agent': BROWSER_UA})
            status = str(resp.status_code)
        except Exception as e:
            status = f"ERR {e}"

        if site_ip:
            ok = site_ip == true_ip
            matched += 1 if ok else 0
            verdict = '✓ 一致' if ok else '✗ 不一致（站点取 IP 有问题）'
            print(f"  [{i}] 页面 {status} | 代理出口 {true_ip} | 站点记录 {site_ip}  {verdict}")
        else:
            print(f"  [{i}] 页面 {status} | 代理出口 {true_ip} | 站点记录 (无) {note}")

        # 兜底：粘性会话生命周期是 30 秒，太慢的话同一会话会过期换 IP，留点余量
        if i < args.count:
            time.sleep(0.8)

    if matched:
        print(f"\n  共 {matched} 轮通过对照。现在去后台「24小时独立IP · 全球」"
              "按最近访问时间看，最新的几条就是这些 IP。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
