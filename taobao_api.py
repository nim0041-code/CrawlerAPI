from typing import Any

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, HttpUrl

from scrape_taobao_with_images import scrape_product


app = FastAPI(title="Taobao/Tmall Product Scraper API")


class ScrapeRequest(BaseModel):
    url: HttpUrl = Field(..., description="淘宝/天猫商品链接")
    download_images: bool = Field(True, description="是否下载商品图片")
    headless: bool = Field(False, description="是否使用无头浏览器")


class ScrapeResponse(BaseModel):
    status: str
    result: dict[str, Any]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(request: ScrapeRequest) -> ScrapeResponse:
    result = await run_in_threadpool(
        scrape_product,
        str(request.url),
        request.download_images,
        request.headless,
    )
    return ScrapeResponse(status=result.get("status", "unknown"), result=result)
