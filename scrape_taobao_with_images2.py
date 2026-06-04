import hashlib
import json
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright


AUTH_STATE = "storage/auth/taobao_state.json"
IMAGE_OUTPUT_ROOT = "storage/images"
MAX_PRODUCT_IMAGES = 20
MIN_IMAGE_WIDTH = 300
MIN_IMAGE_HEIGHT = 300
DOWNLOAD_TIMEOUT = 20


@dataclass
class ImageCandidate:
    url: str
    source: str
    score: int


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
    host = urlparse(product_url).netloc.lower()

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


def clean_product_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()

    remove_suffixes = [
        "-tmall.com天猫",
        "-tmall.com",
        "-淘宝网",
        "- Taobao",
        "- Tmall",
    ]

    for suffix in remove_suffixes:
        if suffix in title:
            title = title.split(suffix)[0].strip()

    bad_titles = [
        "用户评价",
        "用户评价·",
        "商品详情",
        "宝贝详情",
        "累计评价",
    ]

    if any(title.startswith(bad) for bad in bad_titles):
        return ""

    return title


def extract_title(page) -> str:
    title_candidates = []

    meta_selectors = [
        "meta[property='og:title']",
        "meta[name='title']",
        "meta[name='keywords']",
    ]

    for selector in meta_selectors:
        title = get_meta_content(page, selector)
        title = clean_product_title(title)
        if title:
            title_candidates.append(title)

    try:
        page_title = clean_product_title(page.title())
        if page_title:
            title_candidates.append(page_title)
    except Exception:
        pass

    dom_selectors = [
        "h1",
        "[class*=ItemTitle]",
        "[class*=item-title]",
        "[class*=main-title]",
        "[class*=MainTitle]",
        "[data-spm*=title]",
    ]

    dom_title = first_text(page, dom_selectors)
    dom_title = clean_product_title(dom_title)
    if dom_title:
        title_candidates.append(dom_title)

    if title_candidates:
        return title_candidates[0]

    return ""


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
            count = min(locators.count(), 30)
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
        r"(?:\u00a5|\uffe5)\s*(\d+(?:\.\d{1,2})?)",
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
        try:
            matches = re.findall(pattern, combined)
        except Exception:
            continue

        for match in matches:
            try:
                value = float(match)

                if value > 100000:
                    value = value / 100

                if 0.1 <= value <= 999999:
                    candidates.append(value)
            except Exception:
                pass

    if not candidates:
        return ""

    candidates = sorted(set(candidates))
    return str(candidates[0]).rstrip("0").rstrip(".")


def is_login_page(page) -> bool:
    parsed = urlparse(page.url.lower())
    host = parsed.netloc
    path = parsed.path

    try:
        page_title = page.title().strip().lower()
    except Exception:
        page_title = ""

    login_hosts = [
        "login.taobao.com",
        "login.tmall.com",
        "passport.taobao.com",
        "passport.tmall.com",
        "member1.taobao.com",
    ]

    if any(login_host in host for login_host in login_hosts):
        return True

    if "login" in path or "passport" in path:
        return True

    if page_title in ["登录", "login"]:
        return True

    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        login_signals = [
            "扫码登录",
            "密码登录",
            "账户登录",
            "login.taobao.com",
            "passport.taobao.com",
        ]

        matched = sum(1 for signal in login_signals if signal in body_text)
        if matched >= 2:
            return True
    except Exception:
        pass

    return False


def save_raw_html(page, platform: str, product_id: str) -> str:
    Path("storage/raw").mkdir(parents=True, exist_ok=True)

    safe_product_id = product_id or "unknown"
    raw_html_path = f"storage/raw/{platform}_{safe_product_id}.html"

    html = page.content()
    Path(raw_html_path).write_text(html, encoding="utf-8")

    return raw_html_path


def normalize_image_url(url: str, base_url: str = "") -> str:
    if not url:
        return ""

    url = unquote(str(url)).strip().strip("'\"")
    url = url.replace("\\/", "/")

    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        parsed_base = urlparse(base_url)
        if parsed_base.scheme and parsed_base.netloc:
            url = f"{parsed_base.scheme}://{parsed_base.netloc}{url}"

    if not url.startswith(("http://", "https://")):
        return ""

    parsed = urlparse(url)
    lower_url = url.lower()
    lower_path = parsed.path.lower()

    image_domains = ["alicdn.com", "taobaocdn.com", "tbcdn.cn"]
    if not any(domain in parsed.netloc.lower() for domain in image_domains):
        return ""

    has_image_extension = re.search(r"\.(jpg|jpeg|png|webp)(?:$|[?_])", lower_path)
    has_taobao_image_path = any(part in lower_url for part in ["/imgextra/", "/bao/uploaded/", "-tps-"])
    if not has_image_extension and not has_taobao_image_path:
        return ""

    # 淘宝图片常带 430x430q90.jpg、sum.jpg 等缩略图后缀，下载原图更利于通过尺寸过滤。
    url = re.sub(r"_(?:\d+x\d+|sum|q\d+|webp|\.webp).*?$", "", url)
    return url


def image_url_score(url: str, source: str) -> int:
    lower = url.lower()
    score = 50

    if source == "main":
        score += 40
    elif source == "detail":
        score += 25

    if any(domain in lower for domain in ["alicdn.com", "taobaocdn.com", "tbcdn.cn"]):
        score += 10

    if re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)", lower):
        score += 8

    if any(word in lower for word in ["avatar", "icon", "logo", "sprite", "gif", "face"]):
        score -= 45

    if any(word in lower for word in ["sku", "thumb", "thumbnail", "small"]):
        score -= 10

    return score


def add_candidate(candidates: dict, url: str, source: str, base_url: str = "") -> None:
    normalized = normalize_image_url(url, base_url)
    if not normalized:
        return

    score = image_url_score(normalized, source)
    previous = candidates.get(normalized)
    if previous is None or score > previous.score:
        candidates[normalized] = ImageCandidate(normalized, source, score)


def extract_image_candidates(page) -> list[ImageCandidate]:
    candidates = {}
    base_url = page.url

    image_attrs = ["src", "data-src", "data-ks-lazyload", "data-lazyload", "data-img", "data-original"]
    main_selectors = [
        "img[class*=main]",
        "img[class*=Main]",
        "img[class*=pic]",
        "img[class*=Pic]",
        "img[class*=thumb]",
        "[class*=main] img",
        "[class*=gallery] img",
        "[class*=Gallery] img",
    ]
    detail_selectors = [
        "[id*=detail] img",
        "[class*=detail] img",
        "[class*=Detail] img",
        "[class*=desc] img",
        "[class*=Desc] img",
        "[data-spm*=detail] img",
        "img",
    ]

    for selector in main_selectors:
        try:
            locators = page.locator(selector)
            for i in range(min(locators.count(), 80)):
                img = locators.nth(i)
                for attr in image_attrs:
                    add_candidate(candidates, img.get_attribute(attr), "main", base_url)
        except Exception:
            pass

    for selector in detail_selectors:
        try:
            locators = page.locator(selector)
            for i in range(min(locators.count(), 200)):
                img = locators.nth(i)
                for attr in image_attrs:
                    add_candidate(candidates, img.get_attribute(attr), "detail", base_url)
        except Exception:
            pass

    try:
        style_urls = page.locator("[style*='url']").evaluate_all("""
        nodes => nodes.map(node => node.getAttribute('style') || '').join('\\n')
        """)
        for match in re.findall(r"url\\(([^)]+)\\)", style_urls):
            add_candidate(candidates, match, "detail", base_url)
    except Exception:
        pass

    try:
        html = page.content()
        url_pattern = r"(?:https?:)?//[^'\"\\s<>]+?\\.(?:jpg|jpeg|png|webp)(?:_[^'\"\\s<>]+)?"
        for match in re.findall(url_pattern, html, flags=re.IGNORECASE):
            add_candidate(candidates, match, "detail", base_url)
    except Exception:
        pass

    return sorted(candidates.values(), key=lambda item: item.score, reverse=True)


def get_image_size(image_bytes: bytes) -> tuple[Optional[int], Optional[int]]:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
        return int.from_bytes(image_bytes[16:20], "big"), int.from_bytes(image_bytes[20:24], "big")

    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        if image_bytes[12:16] == b"VP8X" and len(image_bytes) >= 30:
            width = 1 + int.from_bytes(image_bytes[24:27], "little")
            height = 1 + int.from_bytes(image_bytes[27:30], "little")
            return width, height
        if image_bytes[12:16] == b"VP8 " and len(image_bytes) >= 30:
            width = int.from_bytes(image_bytes[26:28], "little") & 0x3FFF
            height = int.from_bytes(image_bytes[28:30], "little") & 0x3FFF
            return width, height
        if image_bytes[12:16] == b"VP8L" and len(image_bytes) >= 25:
            bits = int.from_bytes(image_bytes[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height

    if image_bytes.startswith(b"\xff\xd8"):
        index = 2
        while index < len(image_bytes) - 9:
            if image_bytes[index] != 0xFF:
                index += 1
                continue
            marker = image_bytes[index + 1]
            index += 2
            if marker in [0xD8, 0xD9, 0x01]:
                continue
            block_length = int.from_bytes(image_bytes[index:index + 2], "big")
            if marker in range(0xC0, 0xC4) or marker in range(0xC5, 0xC8) or marker in range(0xC9, 0xCC) or marker in range(0xCD, 0xD0):
                height = int.from_bytes(image_bytes[index + 3:index + 5], "big")
                width = int.from_bytes(image_bytes[index + 5:index + 7], "big")
                return width, height
            index += block_length

    return None, None


def detect_image_extension(image_bytes: bytes, content_type: str = "") -> str:
    if image_bytes.startswith(b"\xff\xd8"):
        return ".jpg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return ".webp"

    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed in [".jpg", ".jpeg", ".png", ".webp"]:
        return ".jpg" if guessed == ".jpeg" else guessed

    return ".jpg"


def download_url_bytes(url: str) -> tuple[bytes, str]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://detail.tmall.com/",
            "Accept": "image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5",
        },
    )

    with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
        content_type = response.headers.get("Content-Type", "")
        data = response.read(20 * 1024 * 1024)
        return data, content_type


def is_supported_image_response(image_bytes: bytes, content_type: str = "") -> bool:
    lower_content_type = (content_type or "").lower()
    if any(kind in lower_content_type for kind in ["image/jpeg", "image/jpg", "image/png", "image/webp"]):
        return True

    if image_bytes.startswith((b"\xff\xd8", b"\x89PNG\r\n\x1a\n")):
        return True

    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return True

    return False


def save_image_locally(
    image_bytes: bytes,
    content_type: str,
    output_dir: Path,
    index: int,
    image_hash: str,
) -> str:
    extension = detect_image_extension(image_bytes, content_type)
    filename = f"{index:02d}_{image_hash[:16]}{extension}"
    output_path = output_dir / filename
    output_path.write_bytes(image_bytes)
    return str(output_path)


def upload_image_to_oss_or_s3(local_path: str) -> str:
    # 接 OSS/S3 时在这里替换：上传 local_path，然后 return 云端 URL。
    return local_path


def download_product_images(
    candidates: list[ImageCandidate],
    platform: str,
    product_id: str,
    max_images: int = MAX_PRODUCT_IMAGES,
) -> dict:
    safe_product_id = product_id or str(int(time.time()))
    output_dir = Path(IMAGE_OUTPUT_ROOT) / platform / safe_product_id
    output_dir.mkdir(parents=True, exist_ok=True)

    seen_hashes = set()
    images = []
    skipped = []

    for candidate in candidates:
        if len(images) >= max_images:
            break

        try:
            image_bytes, content_type = download_url_bytes(candidate.url)
            if not is_supported_image_response(image_bytes, content_type):
                skipped.append({
                    "url": candidate.url,
                    "reason": "non_image_response",
                    "content_type": content_type,
                    "bytes": len(image_bytes),
                })
                continue

            image_hash = hashlib.sha256(image_bytes).hexdigest()

            if image_hash in seen_hashes:
                skipped.append({"url": candidate.url, "reason": "duplicate_hash"})
                continue

            width, height = get_image_size(image_bytes)
            if not width or not height:
                skipped.append({
                    "url": candidate.url,
                    "reason": "unknown_size",
                    "content_type": content_type,
                    "bytes": len(image_bytes),
                    "head_hex": image_bytes[:16].hex(),
                })
                continue

            if width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT:
                skipped.append({
                    "url": candidate.url,
                    "reason": "too_small",
                    "width": width,
                    "height": height,
                })
                continue

            local_path = save_image_locally(image_bytes, content_type, output_dir, len(images) + 1, image_hash)
            final_path = upload_image_to_oss_or_s3(local_path)
            seen_hashes.add(image_hash)

            images.append({
                "path": final_path,
                "local_path": local_path,
                "url": candidate.url,
                "source": candidate.source,
                "score": candidate.score,
                "width": width,
                "height": height,
                "sha256": image_hash,
            })
        except Exception as exc:
            skipped.append({"url": candidate.url, "reason": type(exc).__name__})

    return {
        "image_paths": [image["path"] for image in images],
        "images": images,
        "skipped_images": skipped[:50],
        "image_count": len(images),
        "candidate_count": len(candidates),
    }


def scrape_product(product_url: str, download_images: bool = True) -> dict:
    product_id = extract_product_id(product_url)
    platform = detect_platform(product_url)

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

            raw_html_path = save_raw_html(page, platform, product_id)

            if not auth_state_exists:
                return {
                    "status": "failed",
                    "error_type": "auth_state_missing",
                    "message": "未找到登录状态文件，请先运行 save_taobao_auth.py 保存淘宝登录状态",
                    "auth_state_path": AUTH_STATE,
                    "product_id": product_id,
                    "platform": platform,
                    "current_url": page.url,
                    "raw_html_path": raw_html_path,
                }

            if is_login_page(page):
                return {
                    "status": "failed",
                    "error_type": "login_required",
                    "message": "当前页面跳转到了淘宝/天猫登录页，请重新运行 save_taobao_auth.py，确认登录成功后再抓取",
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

            result = {
                "status": "success",
                "product_id": product_id,
                "platform": platform,
                "title": title,
                "page_title": page.title(),
                "price": price,
                "current_url": page.url,
                "raw_html_path": raw_html_path,
            }

            if download_images:
                image_candidates = extract_image_candidates(page)
                image_result = download_product_images(image_candidates, platform, product_id)
                result.update(image_result)

            return result

        finally:
            browser.close()


if __name__ == "__main__":
    url = input("请输入淘宝/天猫商品链接：").strip()
    result = scrape_product(url, download_images=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
