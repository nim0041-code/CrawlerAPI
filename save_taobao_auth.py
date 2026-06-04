from pathlib import Path
from playwright.sync_api import sync_playwright

Path("storage/auth").mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        channel="msedge"
    )

    context = browser.new_context()
    page = context.new_page()

    page.goto("https://www.taobao.com", wait_until="domcontentloaded", timeout=60000)

    input("请在弹出的 Edge 浏览器里扫码登录淘宝，登录完成后按 Enter...")

    context.storage_state(path="storage/auth/taobao_state.json")

    browser.close()

print("淘宝登录态已保存到 storage/auth/taobao_state.json")