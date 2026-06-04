from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from scrape_taobao_with_images import scrape_product


app = FastAPI(title="Taobao/Tmall Product Scraper API")


def is_valid_product_url(url: str) -> bool:
    if not url:
        return False

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return False

    if not parsed.netloc:
        return False

    return True


class ScrapeRequest(BaseModel):
    url: str = Field(..., description="淘宝/天猫商品链接")
    download_images: bool = Field(True, description="是否下载商品图片")
    headless: bool = Field(False, description="是否使用无头浏览器")
    auth_state_path: str | None = Field(None, description="登录状态文件路径")
    image_output_root: str | None = Field(None, description="图片保存根目录")


class ScrapeResponse(BaseModel):
    status: str
    result: dict[str, Any] | None = None
    error_type: str | None = None
    message: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(request: ScrapeRequest) -> ScrapeResponse:
    url = request.url.strip()

    if not is_valid_product_url(url):
        return ScrapeResponse(
            status="failed",
            result=None,
            error_type="invalid_url",
            message="商品链接格式无效",
        )

    auth_state_path =  "storage/auth/taobao_state.json"
    image_output_root = request.image_output_root or "storage/images"

    try:
        result = await run_in_threadpool(
            scrape_product,
            url,
            request.download_images,
            request.headless,
            auth_state_path,
            image_output_root,
        )

        if not isinstance(result, dict):
            return ScrapeResponse(
                status="failed",
                result=None,
                error_type="invalid_scraper_result",
                message="抓取函数未返回 dict 类型结果",
            )

        return ScrapeResponse(
            status=result.get("status", "unknown"),
            result=result,
        )

    except Exception as e:
        return ScrapeResponse(
            status="failed",
            result=None,
            error_type="scrape_internal_error",
            message=str(e),
        )