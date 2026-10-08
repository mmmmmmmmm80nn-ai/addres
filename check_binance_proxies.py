"""Tests every proxy in proxies.txt against Binance and prints the ones that work.
Run on the VPS:  python3 check_binance_proxies.py
"""
import time
from concurrent.futures import ThreadPoolExecutor

import requests

URL = "https://api.binance.com/api/v3/ping"


def norm(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    return line if "://" in line else "http://" + line


def test(px):
    px_use = px.replace("socks5://", "socks5h://").replace("socks4://", "socks4a://")
    t = time.time()
    try:
        r = requests.get(URL, proxies={"http": px_use, "https": px_use}, timeout=10)
        return px, r.status_code, time.time() - t
    except Exception as e:
        return px, type(e).__name__, time.time() - t


def main():
    proxies = [p for p in (norm(l) for l in open("proxies.txt", encoding="utf-8")) if p]
    print(f"testing {len(proxies)} proxies...")
    with ThreadPoolExecutor(40) as ex:
        results = list(ex.map(test, proxies))
    good = sorted([r for r in results if r[1] == 200], key=lambda r: r[2])
    blocked = sum(1 for r in results if r[1] == 451)
    print(f"working: {len(good)} | blocked(451): {blocked} | failed: {len(results) - len(good) - blocked}")
    for px, _, dt in good[:10]:
        print(f"{dt:.1f}s  {px}")
    if good:
        px = good[0][0].replace("socks5://", "socks5h://")
        print(f'\nexport BINANCE_PROXY="{px}"')
    else:
        print("\nلا يوجد بروكسي شغال. استخدم VPS في ألمانيا أو بروكسي مدفوع ثابت.")


if __name__ == "__main__":
    main()
