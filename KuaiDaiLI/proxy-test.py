"""
通过 HTTP 代理访问目标站点，用于核对后台日志里能看到的来源 IP。

用法：
    1. Copy-Item .env.example .env
    2. 在 .env 里填 PROXY_URL / TARGET_URL
    3. python proxy-test.py
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

IP_CHECK_URL = "https://myip.ipip.net"
DEFAULT_ENV = Path(__file__).resolve().with_name(".env")


def main() -> int:
    ap = argparse.ArgumentParser(description="代理连通性与出口 IP 测试")
    ap.add_argument("--count", type=int, default=5, help="请求目标站点的次数")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV), help=".env 文件路径")
    args = ap.parse_args()

    # utf-8-sig：兼容 Windows 上用 PowerShell/记事本存出来的带 BOM 的 .env
    load_dotenv(Path(args.env_file), override=False, encoding="utf-8-sig")

    for key in ("PROXY_URL", "TARGET_URL"):
        if not os.environ.get(key):
            sys.exit(f"请在 .env 里设置 {key}")

    proxy_url = os.environ["PROXY_URL"].strip()
    target_url = os.environ["TARGET_URL"].strip()

    # requests 的代理认证只支持 latin-1，中文占位符或未编码的特殊字符会在这里报错
    try:
        proxy_url.encode("latin-1")
    except UnicodeEncodeError:
        sys.exit("PROXY_URL 里有非 ASCII 字符（占位符还没替换？）。请填真实凭据，用户名密码含特殊字符时要做 URL 编码。")

    proxies = {"http": proxy_url, "https": proxy_url}

    # 1. 出口 IP 检查：隧道代理每次新连接会自动换 IP
    print("=== 出口 IP 检查 ===")
    try:
        r = requests.get(IP_CHECK_URL, proxies=proxies, timeout=30)
    except Exception as e:
        sys.exit(
            f"  代理不可用：{e}\n"
            "  请检查 .env 里的 PROXY_URL，以及快代理后台的 IP 白名单是否包含本机公网 IP"
        )
    print("  " + r.text.strip())

    for _ in range(4):
        try:
            r = requests.get(IP_CHECK_URL, proxies=proxies, timeout=30)
            print("  " + r.text.strip())
        except Exception as e:
            print(f"  (本次失败) {e}")

    # 2. 用代理访问目标站点
    print(f"\n=== 请求目标站点：{target_url} ===")
    for i in range(1, args.count + 1):
        try:
            r = requests.get(target_url, proxies=proxies, timeout=45)
            print(f"  [{i}] {r.status_code}")
        except Exception as e:
            print(f"  [{i}] ERR {e}")
        time.sleep(0.8)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
