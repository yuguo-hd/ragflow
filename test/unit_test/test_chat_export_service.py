import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from aiohttp import web

_SERVICE = Path(__file__).resolve().parents[2] / "api/apps/services/chat_export_service.py"
_SPEC = importlib.util.spec_from_file_location("chat_export_service", _SERVICE)
export = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(export)

_MODEL = {
    "llm_factory": "DeepSeek",
    "llm_name": "deepseek-v4-flash",
    "api_key": "!literal$key",
}


@pytest.mark.parametrize(
    "body,status",
    [
        ({"format": "pdf", "markdown": "Answer"}, 400),
        ({"format": "docx", "markdown": "<think>Only thinking</think>"}, 400),
        ({"format": "docx", "markdown": "中文" * 200_000}, 413),
    ],
)
def test_invalid_export_body(body, status):
    with pytest.raises(export.ChatExportError) as error:
        export.validate_export_request(body)
    assert error.value.status == status


def test_openai_compatible_mapping_and_unsupported_provider():
    model = {
        "llm_factory": "OpenAI-API-Compatible",
        "llm_name": "chat-model",
        "api_base": "http://model:8080/v1/",
    }
    provider = export._pi_config(model)["providers"]["ragflow-openai-compatible"]
    assert provider["baseUrl"] == "http://model:8080/v1"
    assert provider["models"][0]["id"] == "chat-model"
    with pytest.raises(export.ChatExportError) as error:
        export._pi_config({**model, "llm_factory": "unsupported"})
    assert error.value.code == "unsupported_export_provider"


def test_keeps_literal_tags_in_markdown_code():
    markdown = "# Title\n\n`<think>`\n\n```xml\n<retrieving>example</retrieving>\n```\n"
    assert export.validate_export_request({"format": "docx", "markdown": "<think>private</think>" + markdown}) == (
        "docx",
        markdown,
    )


@pytest.mark.parametrize("upstream_status", [200, 502, 503, 504])
def test_multipart_proxy_and_errors(monkeypatch, upstream_status):
    async def run():
        async def upstream(request):
            parts = {}
            multipart = await request.multipart()
            async for part in multipart:
                assert part.filename is None
                parts[part.name] = await part.text()
            assert parts["prompt"] == "# Answer\n\n[Link](https://example.com) [ID:1]\n"
            provider = json.loads(parts["provider"])["providers"]["deepseek-api"]
            assert provider["baseUrl"] == "https://api.deepseek.com/v1"
            assert provider["apiKey"] == "$!literal$$key"
            assert [model["id"] for model in provider["models"]] == ["deepseek-v4-flash"]
            return web.Response(body=b"office-data", content_type="application/octet-stream", status=upstream_status)

        app = web.Application()
        app.router.add_post("/api/v1/skills/m/mkc_makexlsx", upstream)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            monkeypatch.setenv("CMD_PI_BASE_URL", f"http://127.0.0.1:{runner.addresses[0][1]}")
            file_format, markdown = export.validate_export_request({
                "format": "xlsx",
                "markdown": "<think>private</think><retrieving>status</retrieving># Answer\n\n[Link](https://example.com) [ID:1]\n",
            })
            if upstream_status == 200:
                response = await export.export_answer(file_format, markdown, _MODEL)
                assert response.headers["Content-Disposition"] == 'attachment; filename="report.xlsx"'
                assert response.content_length == len(b"office-data")
                assert await response.get_data() == b"office-data"
            else:
                with pytest.raises(export.ChatExportError) as error:
                    await export.export_answer(file_format, markdown, _MODEL)
                assert error.value.status == upstream_status
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_cancelled_download_closes_upstream(monkeypatch):
    async def run():
        request_finished = asyncio.Event()

        async def upstream(request):
            await request.read()
            response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
            await response.prepare(request)
            await response.write(b"first chunk")
            try:
                await asyncio.Future()
            finally:
                request_finished.set()

        app = web.Application()
        app.router.add_post("/api/v1/skills/m/mkc_makedocx", upstream)
        runner = web.AppRunner(app, handler_cancellation=True)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            monkeypatch.setenv("CMD_PI_BASE_URL", f"http://127.0.0.1:{runner.addresses[0][1]}")

            async def download():
                response = await export.export_answer("docx", "Answer", _MODEL)
                async with response.response as body:
                    assert await anext(body.__aiter__()) == b"first chunk"

            await asyncio.wait_for(download(), 3)
            await asyncio.wait_for(request_finished.wait(), 3)
        finally:
            await runner.cleanup()

    asyncio.run(run())
