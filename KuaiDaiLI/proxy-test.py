"""
通过 HTTP 代理访问目标站点，把「代理真实出口 IP」和「站点记录的来源 IP」逐一对照。

一次对照 = 同一个出口 IP 上依次打三个请求：
    1. IP 回显接口        查代理真实出口 IP（真值，第三方视角）
    2. 站点 /api/ip       站点自己算出来的客户端 IP（visits 表里记的就是它）
    3. 站点目标页面        带浏览器 UA，会被计入访问统计

第 1 步和第 2 步相等，说明站点取 IP 的链路是通的，此时第 3 步写进 visits 的也是
这个 IP，去后台「24小时独立IP」按访问时间就能对上。

隧道代理默认每次新连接换一个出口 IP，所以这里用粘性会话（把 -period-1-sid-xxxx
拼到用户名里，1 分钟内固定同一个出口 IP），否则三步会各走各的 IP，对照就没有意义。

用法：
    1. Copy-Item .env.example .env
    2. 在 .env 里填 PROXY_URL / TARGET_URL
    3. python proxy-test.py
"""

import argparse
import ipaddress
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

# 回显源按顺序尝试，第一个返回 IP 就用它：某个源慢或抽风时不会把整轮卡死。
# 前面两个返回纯文本 IP，最后一个返回中文句子（靠 IP_TEXT_PATTERN 提取）
IP_CHECK_URLS = (
    "https://api.ipify.org",
    "https://ipinfo.io/ip",
    "https://myip.ipip.net",
)
TIMEOUT = (10, 15)       # (连接, 读取) 秒
PAGE_TIMEOUT = (10, 30)  # 目标页面本来就慢的话，读取稍放宽

# 快代理海外动态代理：period 单位是「分钟」，不写 / 0 = 每个请求换一次 IP，
# 正整数 = 这段时间内固定同一个出口 IP（1 是最短的粘性窗口）
STICKY_PERIOD = 1

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
    text = (text or '').strip()
    try:  # 纯 IP 的回显接口（ipify / ipinfo）
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    match = IP_TEXT_PATTERN.search(text)
    return match.group(1) if match else ''


def build_echo_url(target_url: str) -> str:
    """目标站点的 IP 回显接口：同源 + /api/ip（就是 app.py 里的 api_client_ip）。"""
    parts = urlsplit(target_url)
    return urlunsplit((parts.scheme, parts.netloc, '/api/ip', '', ''))


def _with_params(proxy_url: str, suffix: str):
    """把参数拼到「用户名」后面，密码和 host 不动；凭据里没有用户名时返回 None。"""
    if '@' not in proxy_url:
        return None
    head, tail = proxy_url.rsplit('@', 1)
    scheme, sep, cred = head.rpartition('://')
    if not sep or ':' not in cred:
        return None
    user, _, password = cred.partition(':')
    return f'{scheme}://{user}{suffix}:{password}@{tail}'


def with_session(proxy_url: str, session: str, period: int = STICKY_PERIOD):
    """给快代理海外动态代理加粘性参数，格式是把参数拼进「用户名」里：

        http://用户名-period-1-sid-xxxx:密码@i104.kdlfps.com:18866

    period 是固定出口 IP 的分钟数（正整数；0 或不写 = 每个请求换一次）；
    sid 是通道号（≤6 位），不同 sid 在同一周期内各拿一个独立的出口 IP。
    之前把会话 ID 拼在密码后面（用户名:密码:会话ID）会直接被判成密码错误，
    代理返回 407 Proxy Authentication Required。

    IP 白名单认证（URL 里没有用户名密码）用不了粘性参数，返回 None。
    """
    return _with_params(proxy_url, f'-period-{period}-sid-{session}')


def with_period(proxy_url: str, period: int = STICKY_PERIOD):
    """只加 period 不加 sid：整条隧道共用一个出口 IP（多通道没开通时的退路）。"""
    return _with_params(proxy_url, f'-period-{period}')


def sticky_proxy(proxy_url: str, kind: str, session: str = 'probe'):
    """按预检选出的模式生成代理 URL：sid = 多通道，period = 单通道，plain = 原样。"""
    if kind == 'plain':
        return proxy_url
    if kind == 'sid':
        return with_session(proxy_url, session)
    return with_period(proxy_url)


def make_proxies(url: str):
    return {'http': url, 'https': url}


def check_exit_ip(proxies) -> str:
    """取代理真实出口 IP，回显源依次尝试，全部失败才抛异常。"""
    last = '没有可用的 IP 回显接口'
    for url in IP_CHECK_URLS:
        try:
            resp = requests.get(url, proxies=proxies, timeout=TIMEOUT,
                                headers={'User-Agent': BROWSER_UA})
        except requests.exceptions.ProxyError:
            raise  # 代理本身不通（407 / 连不上），换回显源也没用
        except requests.RequestException as e:
            last = f'{url}: {e}'
            continue
        ip = parse_ip(resp.text)
        if ip:
            return ip
        last = f'{url} 返回内容里没有 IP'
    raise RuntimeError(last)


def fetch_site_ip(echo_url: str, proxies, ua: str):
    """读站点 /api/ip，返回 (ip, 说明)。站点没这个接口（或不是本项目）时 ip 为空串。"""
    resp = requests.get(echo_url, proxies=proxies, timeout=TIMEOUT,
                        headers={'User-Agent': ua})
    if resp.status_code != 200:
        return '', f'(/api/ip 返回 {resp.status_code})'
    try:
        return (resp.json() or {}).get('ip', '') or '', ''
    except ValueError:
        return '', '(/api/ip 返回的不是 JSON)'


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
    # 依次试三种写法，能用哪种用哪种：
    #   sid   多通道，每轮一个独立出口 IP（对照效果最好）
    #   period 单通道，周期内固定一个 IP（多通道没开通时用）
    #   plain  不拼参数，每次连接换 IP（只能说是代理本身通不通）
    mode = None
    last_err = None
    for kind in ('sid', 'period', 'plain'):
        url = sticky_proxy(proxy_url, kind)
        if url is None:
            continue  # IP 白名单认证，用户名里没东西可拼
        try:
            exit_ip = check_exit_ip(make_proxies(url))
        except requests.exceptions.ProxyError as e:
            last_err = e
            if kind == 'sid':
                print("  (多通道参数不可用，改试单通道) " + str(e))
            elif kind == 'period':
                print("  (单通道参数也不可用，改试普通模式) " + str(e))
            continue
        except Exception as e:
            sys.exit(f"  代理不可用：{e}")
        print("  " + exit_ip)
        mode = kind
        break

    if mode is None:
        hint = '  请检查 .env 里的 PROXY_URL，以及快代理后台的 IP 白名单是否包含本机公网 IP'
        if last_err and '456' in str(last_err):
            hint = '  456 = 快代理拒绝来自中国大陆的客户端，要在境外机器（比如这台 VPS）上跑'
        sys.exit(f"  代理不可用：{last_err}\n{hint}")

    if mode == 'plain':
        print("  注意：没有可用的粘性会话，出口 IP 每次连接都会变，"
              "下面标「无法对照」的轮次不代表站点有问题。")
    elif mode == 'period':
        print("  注意：单通道模式，同一周期内所有轮次共用一个出口 IP，"
              "能验证站点取 IP，但拿不到多个不同 IP。")

    # ---- 逐轮对照 ----
    print(f"\n=== IP 对照：{target_url} ===")
    print(f"    回显接口：{echo_url}")
    matched = compared = 0
    for i in range(1, args.count + 1):
        # sid 模式下每轮换一个随机通道（≤6 位），轮与轮之间拿到不同的出口 IP
        proxies = make_proxies(sticky_proxy(proxy_url, mode, secrets.token_hex(3)) or proxy_url)

        try:
            true_ip = check_exit_ip(proxies)
        except Exception as e:
            print(f"  [{i}] 取出口 IP 失败：{e}")
            continue

        try:
            site_ip, note = fetch_site_ip(echo_url, proxies, BROWSER_UA)
        except Exception as e:
            site_ip, note = '', f'(读 /api/ip 失败：{e})'

        # 站点取完 IP 再取一次代理出口 IP：两次一样，才能确信「站点记录」
        # 和「代理出口」来自同一个 IP；中途变了说明粘性没撑住，这轮没得比
        try:
            later_ip = check_exit_ip(proxies)
        except Exception:
            later_ip = ''

        try:
            resp = requests.get(target_url, proxies=proxies, timeout=PAGE_TIMEOUT,
                                headers={'User-Agent': BROWSER_UA})
            status = str(resp.status_code)
        except Exception as e:
            status = f"ERR {e}"

        if not site_ip:
            print(f"  [{i}] 页面 {status} | 代理出口 {true_ip} | 站点记录 (无) {note}")
        elif later_ip and later_ip != true_ip:
            print(f"  [{i}] 页面 {status} | 代理出口 {true_ip}→{later_ip} | "
                  f"站点记录 {site_ip}  ? 出口 IP 中途变了，这轮无法对照")
        else:
            ok = site_ip == true_ip
            matched += 1 if ok else 0
            compared += 1
            verdict = '✓ 一致' if ok else '✗ 不一致（站点取 IP 有问题）'
            print(f"  [{i}] 页面 {status} | 代理出口 {true_ip} | 站点记录 {site_ip}  {verdict}")

        if i < args.count:
            time.sleep(0.8)

    if compared:
        print(f"\n  共 {compared} 轮完成对照，{matched} 轮一致。现在去后台「24小时独立IP · 全球」"
              "按最近访问时间看，最新的几条就是这些 IP。")
    elif args.count:
        print("\n  没有一轮能完成对照，原因见上面每一轮的说明。")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:  # Ctrl+C 时不要甩一屏 traceback
        print("\n已中断")
        raise SystemExit(130)
