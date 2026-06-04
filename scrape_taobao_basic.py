import re
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright


AUTH_STATE = "storage/auth/taobao_state.json"


def extract_product_id(product_url: str) -> str:
    parsed = urlparse(product_url)
    query = parse_qs(parsed.query)

    if "id" in query:
        return query["id"][0]

    match = re.search(r"id=(\d+)", product_url)
    if match:
        return match.group(1)

    return ""


def detect_platform(product_url: str) -> str:
    host = urlparse(product_url).netloc

    if "tmall.com" in host:
        return "tmall"

    if "taobao.com" in host:
        return "taobao"

    return "unknown"


def first_text(page, selectors):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                text = locator.inner_text(timeout=3000).strip()
                if text:
                    return text
        except Exception:
            pass

    return ""


def get_meta_content(page, selector: str) -> str:
    try:
        locator = page.locator(selector).first
        if locator.count() > 0:
            content = locator.get_attribute("content", timeout=3000)
            if content:
                return content.strip()
    except Exception:
        pass

    return ""


def extract_title(page) -> str:
    selectors = [
        "h1",
        "[class*=ItemTitle]",
        "[class*=item-title]",
        "[class*=title]",
        "[class*=Title]",
    ]

    title = first_text(page, selectors)

    if not title:
        title = get_meta_content(page, "meta[property='og:title']")

    if not title:
        try:
            title = page.title().strip()
        except Exception:
            title = ""

    title = re.sub(r"\s+", " ", title)
    return title


def extract_price(page) -> str:
    selectors = [
        "[class*=Price]",
        "[class*=price]",
        "[class*=promotion]",
        "[class*=Promotion]",
        "[class*=sale-price]",
        "[class*=SalePrice]",
        "[class*=value]",
        "[class*=Value]",
        "[class*=money]",
        "[class*=Money]",
        "[class*=yen]",
        "[class*=Yen]",
        "[class*=realPrice]",
        "[class*=RealPrice]",
    ]

    price_texts = []

    for selector in selectors:
        try:
            locators = page.locator(selector)
            count = min(locators.count(), 20)
            for i in range(count):
                text = locators.nth(i).inner_text(timeout=1000).strip()
                if text:
                    price_texts.append(text)
        except Exception:
            pass

    try:
        body_text = page.locator("body").inner_text(timeout=5000)
        price_texts.append(body_text)
    except Exception:
        pass

    try:
        scripts_text = page.locator("script").evaluate_all("""
        scripts => scripts.map(s => s.textContent || '').join('\\n')
        """)
        price_texts.append(scripts_text)
    except Exception:
        pass

    combined = "\n".join(price_texts)

    patterns = [
        r"(?:¥|￥)\s*(\d+(?:\.\d{1,2})?)",
        r'"price"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?',
        r'"priceText"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?',
        r'"salePrice"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?',
        r'"promotionPrice"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?',
        r'"reservePrice"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?',
        r'"priceCent"\s*:\s*"?(\d+)"?',
        r"价格\s*[:：]?\s*(\d+(?:\.\d{1,2})?)",
        r"到手价\s*[:：]?\s*(\d+(?:\.\d{1,2})?)",
        r"券后\s*[:：]?\s*(\d+(?:\.\d{1,2})?)",
    ]

    candidates = []

    for pattern in patterns:
        for match in re.findall(pattern, combined):
            try:
                value = float(match)

                # priceCent 这种是分，需要除以 100
                if value > 100000:
                    value = value / 100

                if 0.1 <= value <= 999999:
                    candidates.append(value)
            except Exception:
                pass

    if not candidates:
        return ""

    # 通常商品价格不会是页面里最大数字，先取比较合理的最小价格
    candidates = sorted(set(candidates))
    return str(candidates[0]).rstrip("0").rstrip(".")

def is_login_page(page) -> bool:
    current_url = page.url.lower()

    try:
        page_title = page.title().strip()
    except Exception:
        page_title = ""

    login_url_keywords = [
        "login",
        "passport",
        "member1.taobao.com",
        "login.taobao.com",
    ]

    if any(keyword in current_url for keyword in login_url_keywords):
        return True

    if page_title in ["登录", "淘宝网 - 淘！我喜欢"]:
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
            if "登录" in body_text and ("密码" in body_text or "扫码" in body_text):
                return True
        except Exception:
            return True

    return False


def scrape_product(product_url: str) -> dict:
    product_id = extract_product_id(product_url)
    platform = detect_platform(product_url)

    Path("storage/raw").mkdir(parents=True, exist_ok=True)

    auth_state_exists = Path(AUTH_STATE).exists()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="msedge"
        )

        if auth_state_exists:
            context = browser.new_context(storage_state=AUTH_STATE)
        else:
            context = browser.new_context()

        page = context.new_page()

        try:
            page.goto(product_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            html = page.content()
            raw_html_path = f"storage/raw/{platform}_{product_id or 'unknown'}.html"
            Path(raw_html_path).write_text(html, encoding="utf-8")

            if not auth_state_exists:
                return {
                    "status": "failed",
                    "error_type": "auth_state_missing",
                    "message": "未找到登录态文件，请先运行 save_taobao_auth.py 保存淘宝登录态",
                    "auth_state_path": AUTH_STATE,
                    "current_url": page.url,
                    "raw_html_path": raw_html_path,
                }

            if is_login_page(page):
                return {
                    "status": "failed",
                    "error_type": "login_required",
                    "message": "当前页面跳转到淘宝/天猫登录页，请重新运行 save_taobao_auth.py，并确认登录成功后再抓取",
                    "product_id": product_id,
                    "platform": platform,
                    "current_url": page.url,
                    "page_title": page.title(),
                    "raw_html_path": raw_html_path,
                }

            title = extract_title(page)
            price = extract_price(page)

            if title in ["登录", ""] and not price:
                return {
                    "status": "failed",
                    "error_type": "parse_failed_or_login_required",
                    "message": "未能提取到有效商品标题和价格，可能仍在登录页、验证页或页面结构变化",
                    "product_id": product_id,
                    "platform": platform,
                    "current_url": page.url,
                    "page_title": page.title(),
                    "raw_html_path": raw_html_path,
                }

            return {
                "status": "success",
                "product_id": product_id,
                "platform": platform,
                "title": title,
                "price": price,
                "current_url": page.url,
                "raw_html_path": raw_html_path,
            }

        finally:
            browser.close()


if __name__ == "__main__":
    url = input("请输入淘宝/天猫商品链接：").strip()
    result = scrape_product(url)
    print(json.dumps(result, ensure_ascii=False, indent=2))