"""Playwright driver for Flow Omni 1.2 Flash video-to-video edit (source video + reference images)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gflow_cli.api.client import FlowApiClient
from gflow_cli.api.transports._mc_common import (
    ASPECT_LIGATURE,
    MIGRATED_ROOT_URL,
    NEW_PROJECT_BUTTON,
    PICKER_OPTION,
    SUBMIT_RPCS,
    TOOLBAR_ADD,
    UPLOAD_MENU_ITEM,
    _unique_display_name,
)
from gflow_cli.api.transports.migrated_composer import MigratedComposer
from gflow_cli.api.video import Mode, VideoModel
from gflow_cli.config import get_settings
from gflow_cli.profile_store import resolve_profile

VIDEO_EDIT_SUBMIT_RPCS = ("jIps6", *SUBMIT_RPCS)


async def _ensure_project_id(page) -> str:
    await page.goto(MIGRATED_ROOT_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(3000)
    link = page.locator('a[href*="/project/"]').first
    if await link.count():
        await link.click()
    else:
        await page.locator(NEW_PROJECT_BUTTON).first.click()
    await page.wait_for_url("**/project/**", timeout=20000)
    return page.url.split("/project/")[1].split("?")[0].split("/")[0]


async def _upload_video(page, video_path: Path) -> tuple[str, str]:
    display_name = _unique_display_name(video_path)
    stem = Path(display_name).stem
    loop = asyncio.get_running_loop()
    vid_id_fut: asyncio.Future[str] = loop.create_future()

    async def on_resp(response):
        if "/flow/upload/video/" not in response.url or response.status != 200 or vid_id_fut.done():
            return
        try:
            text = await response.text()
            if text.strip().startswith("{"):
                data = json.loads(text)
                mid = data.get("mediaId")
                if isinstance(mid, str) and mid:
                    vid_id_fut.set_result(mid)
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        await page.locator(TOOLBAR_ADD).first.click(timeout=5000)
        async with page.expect_file_chooser(timeout=8000) as fc_info:
            await page.locator(UPLOAD_MENU_ITEM).first.click(timeout=5000)
        chooser = await fc_info.value
        raw = await asyncio.to_thread(video_path.read_bytes)
        await chooser.set_files({"name": display_name, "mimeType": "video/mp4", "buffer": raw})
        await page.wait_for_timeout(1500)
        # Accept one-time video rights dialog if Flow displays it on a fresh profile.
        dlg = page.locator('[role="dialog"]')
        if await dlg.count():
            for label in ("我同意，不再显示", "I agree, don't show again", "我同意", "I agree"):
                btn = dlg.get_by_role("button", name=label)
                if await btn.count():
                    await btn.first.click(timeout=4000)
                    break
        media_id = await asyncio.wait_for(vid_id_fut, timeout=120.0)
        return media_id, stem
    finally:
        page.remove_listener("response", on_resp)


async def _attach_video_chip(page, composer: MigratedComposer, query_stem: str) -> None:
    for _ in range(24):
        await composer.clear_composer(page)
        await page.keyboard.type("@" + query_stem, delay=40)
        await page.wait_for_timeout(2000)
        opts = await page.locator(PICKER_OPTION).all_inner_texts()
        if opts and "正在上传" not in opts[0] and "Uploading" not in opts[0]:
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(1500)
            box_text = await page.locator("flow-prompt-box").first.inner_text()
            if "videocam" in box_text:
                return
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(2500)
    raise RuntimeError("video_reference_attach_timeout")


async def generate_video_edit(
    *,
    prompt: str,
    video_path: Path,
    reference_images: list[Path],
    aspect: str,
    duration: int | None,
    output_dir: Path,
) -> dict:
    settings = get_settings()
    profile_dir = settings.profile_subdir(resolve_profile(None))
    aspect_keys = list(ASPECT_LIGATURE)
    selected_aspect = aspect_keys[1] if aspect == "9:16" and len(aspect_keys) > 1 else aspect_keys[0]

    class _EditReq:
        mode = Mode.R2V
        model = VideoModel.OMNI_1_2_FLASH
        count = 1
        resolution = None
        start_image = None
        end_image = None
        reference_images = ()
        reference_entities = ()

    req = _EditReq()
    req.aspect = selected_aspect
    req.duration = duration

    async with FlowApiClient(profile_dir=profile_dir, headless=True) as client:
        page = client._page
        proj_id = await _ensure_project_id(page)
        composer = MigratedComposer()
        await composer.ensure_editor(page, proj_id)
        await composer.apply_video_settings(page, req)

        vid_id, vid_stem = await _upload_video(page, video_path)
        img_ids: list[str] = []
        img_names: list[str] = []
        for img_path in reference_images:
            iid, iname = await composer._upload_via_toolbar(page, proj_id, img_path)
            img_ids.append(iid)
            img_names.append(iname)

        await _attach_video_chip(page, composer, vid_stem)
        for idx, iname in enumerate(img_names, start=1):
            await composer._mention_by_name(page, iname, expect_chips=idx)
        await composer.send_prompt(page, prompt, append=True)

        record = await composer.submit_and_observe(
            page,
            poll_timeout_s=480.0,
            on_started=None,
            project_id=proj_id,
            submit_rpcs=VIDEO_EDIT_SUBMIT_RPCS,
            expect_reference_ids=tuple(img_ids),
            adopt_ids=(vid_id, *img_ids),
        )
        if not record.is_done:
            return {"status": "failed", "flow_media_id": record.media_id,
                    "error": {"code": "generation_failed", "detail": str(record.status)}}

        output_dir.mkdir(parents=True, exist_ok=True)

        async def _save(media_id: str, data: bytes) -> Path:
            dest = output_dir / f"{media_id}.mp4"
            await asyncio.to_thread(dest.write_bytes, data)
            return dest

        saved = await composer.download(page, record, _save)
        return {
            "status": "ok",
            "flow_media_id": record.media_id,
            "files": [str(saved)] if saved else [],
        }
