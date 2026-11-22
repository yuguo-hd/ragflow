import asyncio
import json
import os
import re
from urllib.parse import urlsplit

import aiohttp
from quart import Response

_SKILLS = {
    "docx": "mkc_makedocx",
    "xlsx": "mkc_makexlsx",
    "pptx": "mkc_makepptx",
}
_MAX_MARKDOWN_BYTES = 1024 * 1024
_TIMEOUT = 630
_FILE_TYPE = "application/octet-stream"
_INTERNAL_PREFIX = re.compile(r"\s*<(think|retrieving)>")


class ChatExportError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def validate_export_request(body: object) -> tuple[str, str]:
    if not isinstance(body, dict) or set(body) != {"format", "markdown"}:
        raise ChatExportError(400, "invalid_export_request")
    file_format, markdown = body["format"], body["markdown"]
    if not isinstance(file_format, str) or file_format not in _SKILLS or not isinstance(markdown, str):
        raise ChatExportError(400, "invalid_export_request")
    try:
        too_large = len(markdown.encode("utf-8")) > _MAX_MARKDOWN_BYTES
    except UnicodeEncodeError:
        raise ChatExportError(400, "invalid_export_request") from None
    if too_large:
        raise ChatExportError(413, "export_markdown_too_large")
    # Chat completion emits status and reasoning before the answer. Preserve
    # Markdown within the answer, including literal tags in code examples.
    start = 0
    while block := _INTERNAL_PREFIX.match(markdown, start):
        closing = f"</{block[1]}>"
        end = markdown.find(closing, block.end())
        if end == -1:
            raise ChatExportError(400, "empty_export_markdown")
        start = end + len(closing)
    markdown = markdown[start:]
    if not markdown.strip():
        raise ChatExportError(400, "empty_export_markdown")
    return file_format, markdown


def _valid_http_url(value):
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and not (url.username is not None or url.password is not None or url.fragment or url.query)
        )
    except ValueError:
        return False


def _pi_config(model):
    factory = model.get("llm_factory")
    model_id = model.get("llm_name")
    if not isinstance(model_id, str) or not model_id or model_id.startswith("-"):
        raise ChatExportError(422, "export_model_unavailable")
    base_url = model.get("api_base")
    key = model.get("api_key")
    compat = {"maxTokensField": "max_tokens", "supportsDeveloperRole": False}
    if factory == "DeepSeek":
        provider_id = "deepseek-api"
        base_url = base_url or "https://api.deepseek.com/v1"
        context = 1_000_000
        max_tokens = 32_768
        reasoning = True
        compat["thinkingFormat"] = "deepseek"
    elif factory == "OpenAI-API-Compatible":
        provider_id = "ragflow-openai-compatible"
        key = key or "local"
        context = model.get("max_tokens") or 8192
        if not isinstance(context, int) or context < 2:
            raise ChatExportError(422, "export_model_unavailable")
        max_tokens = min(32_768, context // 2)
        reasoning = False
    else:
        raise ChatExportError(422, "unsupported_export_provider")
    if not _valid_http_url(base_url) or not isinstance(key, str) or not key.strip() or any(char in key for char in "\r\n"):
        raise ChatExportError(422, "export_model_unavailable")
    # Pi interprets a leading ! and dollar references in models.json.
    # The upstream API key is a literal from RAGFlow's model configuration.
    key = key.replace("$", "$$")
    if key.startswith("!"):
        key = "$" + key
    return {
        "providers": {
            provider_id: {
                "baseUrl": base_url.rstrip("/"),
                "api": "openai-completions",
                "apiKey": key,
                "models": [{
                    "id": model_id,
                    "input": ["text"],
                    "contextWindow": context,
                    "maxTokens": max_tokens,
                    "reasoning": reasoning,
                    "compat": compat,
                }],
            },
        },
    }


async def export_answer(file_format: str, markdown: str, model: dict) -> Response:
    base_url = os.environ.get("CMD_PI_BASE_URL", "").strip()
    if not _valid_http_url(base_url):
        raise ChatExportError(503, "export_service_unconfigured")
    provider = json.dumps(_pi_config(model))
    url = f"{base_url.rstrip('/')}/api/v1/skills/m/{_SKILLS[file_format]}"
    with aiohttp.MultipartWriter("form-data") as form:
        for name, value in (("provider", provider), ("prompt", markdown)):
            part = form.append(value)
            part.set_content_disposition("form-data", name=name)
        client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_TIMEOUT, connect=10), auto_decompress=False)
        try:
            upstream = await client.post(url, data=form, allow_redirects=False)
        except TimeoutError:
            await client.close()
            raise ChatExportError(504, "export_timeout") from None
        except aiohttp.ClientError:
            await client.close()
            raise ChatExportError(502, "export_service_unreachable") from None
        except BaseException:
            await client.close()
            raise
    if upstream.status != 200 or upstream.content_type != _FILE_TYPE:
        upstream.close()
        await client.close()
        if upstream.status == 503:
            raise ChatExportError(503, "export_service_busy")
        if upstream.status == 504:
            raise ChatExportError(504, "export_timeout")
        raise ChatExportError(502, "export_generation_failed")

    async def chunks():
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            upstream.close()
            await client.close()

    def close_if_body_never_started(_):
        # Quart can cancel the request while sending headers, before entering chunks().
        if not client.closed:
            upstream.close()
            asyncio.create_task(client.close())

    request_task = asyncio.current_task()
    if request_task:
        request_task.add_done_callback(close_if_body_never_started)
    response = Response(
        chunks(),
        content_type=_FILE_TYPE,
        headers={"Content-Disposition": f'attachment; filename="report.{file_format}"', "Cache-Control": "no-store"},
    )
    if upstream.content_length is not None:
        response.content_length = upstream.content_length
    response.timeout = _TIMEOUT
    return response
