"""Photo-short projects: upload photos → (AI) script → edit → narrate → render → publish."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from highlight_engine.photos import AUDIO_SR, HEIC_SUPPORTED, IMAGE_EXT, Slide, estimate_duration, normalize_photo, render_slideshow
from highlight_engine.render.captions import PRESETS, with_brand

from . import tts as tts_mod
from .auth import QuotaError
from .photo_script import TEMPLATES, ai_script, card_for, fallback_script, template_catalog, with_durations
from .youtube import AccountRegistry, VideoMeta, YouTubeError

MAX_PHOTOS = 20
MUSIC_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg"}
PHOTO_MINUTES = 1.0  # charged per photo project


class SlideIn(BaseModel):
    photo: str
    text: str = Field("", max_length=80)
    duration: float = Field(3.0, ge=1.0, le=10.0)


class ScriptIn(BaseModel):
    title: str = Field("", max_length=80)
    slides: list[SlideIn] = Field(min_length=1, max_length=MAX_PHOTOS)
    keywords: list[str] = []
    hashtags: list[str] = []


class SettingsIn(BaseModel):
    fields: dict | None = None
    notes: str | None = Field(None, max_length=500)
    preset: str | None = None
    voice: bool | None = None
    voice_id: str | None = None
    music_volume: float | None = Field(None, ge=0, le=1)
    show_title: bool | None = None
    end_card: bool | None = None


class PhotoPatch(BaseModel):
    name: str | None = Field(None, max_length=80)
    script: ScriptIn | None = None
    settings: SettingsIn | None = None


class PhotoPublish(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=4500)
    privacy: str = "public"
    publish_at: str | None = None


def register(app: FastAPI, registry: AccountRegistry) -> None:
    store, jobs = app.state.store, app.state.jobs
    app.state.tts = tts_mod.from_env()

    def uid(request: Request) -> str:
        return request.state.user["id"]

    def project(pid: str) -> dict:
        p = store.get(pid)
        if p.get("kind") != "photo":
            raise HTTPException(404, "사진 프로젝트가 아니에요")
        return p

    def view(p: dict) -> dict:
        base = f"/api/photo-projects/{p['id']}"
        photos = [{**ph, "url": f"{base}/photos/{ph['file']}", "thumb_url": f"{base}/photos/{ph['thumb']}"} for ph in p["photos"]]
        job = jobs.get(p["job_id"]) if p.get("job_id") else None
        return {**p, "photos": photos, "job": job, "templates": None}

    async def save_photos(pid: str, files: list[UploadFile]) -> list[dict]:
        d = store.dir(pid) / "photos"
        start = len(store.get(pid)["photos"])
        out = []
        for i, f in enumerate(files):
            ext = Path(f.filename or "").suffix.lower()
            if ext not in IMAGE_EXT:
                raise HTTPException(400, f"사진 형식이 아니에요: {f.filename}")
            if ext in (".heic", ".heif") and not HEIC_SUPPORTED:
                raise HTTPException(400, "이 컴퓨터에서는 HEIC(아이폰) 사진을 읽을 수 없어요. JPG로 바꿔 올리거나 pillow-heif를 설치하세요")
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                shutil.copyfileobj(f.file, tmp)
            n = start + i + 1
            name, thumb = f"p{n:02d}.jpg", f"t{n:02d}.jpg"
            try:
                w, h = normalize_photo(tmp.name, str(d / name), str(d / thumb))
            except Exception:
                raise HTTPException(400, f"사진을 읽을 수 없어요: {f.filename}")
            finally:
                Path(tmp.name).unlink(missing_ok=True)
            out.append({"file": name, "thumb": thumb, "w": w, "h": h, "orig": (f.filename or "")[:80]})
        return out

    def script_job(pid: str, use_ai: bool, *, progress) -> dict:
        store.update(pid, lambda p: p.update(status="scripting", error=None))
        p = store.get(pid)
        s = p["settings"]
        paths = [str(store.dir(pid) / "photos" / ph["file"]) for ph in p["photos"]]
        progress(0.1, "사진을 보고 대본을 쓰는 중" if use_ai else "템플릿으로 대본을 만드는 중")
        script = ai_script(s["template"], s["fields"], paths, s.get("notes", "")) if use_ai else \
            fallback_script(s["template"], s["fields"], len(paths))
        files = [ph["file"] for ph in p["photos"]]
        script["slides"] = [{"photo": files[sl["photo"]], "text": sl["text"]} for sl in script["slides"]]
        used = {sl["photo"] for sl in script["slides"]}
        if not use_ai:  # keep every photo when there's no AI deciding what to skip
            script["slides"] += [{"photo": f, "text": ""} for f in files if f not in used]
        with_durations(script)

        def fn(pr: dict) -> None:
            pr["script"] = script
            pr["status"] = "ready"
            if script.get("title") and pr["name"].startswith("사진 숏츠"):
                pr["name"] = script["title"]

        store.update(pid, fn)
        return {"slides": len(script["slides"]), "source": script.get("source")}

    def start_script(pid: str, use_ai: bool) -> str:
        owner = store.owner_of(pid)

        def fail(err: str) -> None:
            store.update(pid, lambda p: p.update(status="error", error=err))

        jid = jobs.submit("photo_script", lambda *, progress: script_job(pid, use_ai, progress=progress),
                          on_error=fail, project=pid, owner=owner)
        store.update(pid, lambda p: p.update(job_id=jid))
        return jid

    # ------------------------------------------------------------------ catalog
    @app.get("/api/photo/templates")
    def templates():
        tts = app.state.tts
        return {"templates": template_catalog(), "heic": HEIC_SUPPORTED, "max_photos": MAX_PHOTOS,
                "tts": {"available": tts is not None, "provider": tts.name if tts else None,
                        "voices": [{"id": v, "label": l} for v, l in (tts.voices if tts else [])]}}

    # ------------------------------------------------------------------ create / read / edit
    @app.post("/api/photo-projects")
    async def create(request: Request, photos: list[UploadFile] = File(...), template: str = Form("restaurant"),
                     name: str = Form(""), fields: str = Form("{}"), notes: str = Form(""), ai: bool = Form(True),
                     voice: bool = Form(False)):
        if template not in TEMPLATES:
            raise HTTPException(400, "템플릿을 골라 주세요")
        if not 1 <= len(photos) <= MAX_PHOTOS:
            raise HTTPException(400, f"사진은 1~{MAX_PHOTOS}장 올릴 수 있어요")
        try:
            field_values = {k: str(v)[:120] for k, v in json.loads(fields or "{}").items()}
        except (ValueError, AttributeError):
            raise HTTPException(400, "입력 정보를 읽을 수 없어요")
        owner = uid(request)
        p = store.create_photo(name.strip()[:80] or f"사진 숏츠 · {TEMPLATES[template]['label']}", owner, template)
        try:
            app.state.accounts.charge(owner, PHOTO_MINUTES, p["id"])
        except QuotaError:
            store.delete(p["id"])
            raise
        try:
            saved = await save_photos(p["id"], photos)
        except HTTPException:
            app.state.accounts.refund(owner, p["id"])
            store.delete(p["id"])
            raise

        def fn(pr: dict) -> None:
            pr["photos"] = saved
            pr["settings"].update(fields=field_values, notes=notes[:500], preset=TEMPLATES[template]["preset"],
                                  voice=bool(voice and app.state.tts))

        store.update(p["id"], fn)
        start_script(p["id"], ai)
        return view(store.get(p["id"]))

    @app.get("/api/photo-projects/{pid}")
    def get_one(pid: str):
        return view(project(pid))

    @app.get("/api/photo-projects/{pid}/photos/{name}")
    def photo_file(pid: str, name: str):
        path = store.dir(pid) / "photos" / Path(name).name
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/photo-projects/{pid}/photos")
    async def add_photos(pid: str, photos: list[UploadFile] = File(...)):
        p = project(pid)
        if len(p["photos"]) + len(photos) > MAX_PHOTOS:
            raise HTTPException(400, f"사진은 최대 {MAX_PHOTOS}장이에요")
        saved = await save_photos(pid, photos)

        def fn(pr: dict) -> None:
            pr["photos"] += saved
            if pr.get("script"):
                pr["script"]["slides"] += [{"photo": s["file"], "text": "", "duration": 3.0} for s in saved]

        store.update(pid, fn)
        return view(store.get(pid))

    @app.patch("/api/photo-projects/{pid}")
    def patch(pid: str, body: PhotoPatch):
        p = project(pid)
        files = {ph["file"] for ph in p["photos"]}
        if body.script is not None and any(s.photo not in files for s in body.script.slides):
            raise HTTPException(400, "없는 사진이 포함되어 있어요")
        if body.settings is not None and body.settings.preset is not None and body.settings.preset not in PRESETS:
            raise HTTPException(400, "자막 스타일을 확인하세요")
        if body.settings is not None and body.settings.voice and not app.state.tts:
            raise HTTPException(400, "AI 목소리가 설정되지 않았어요")

        def fn(pr: dict) -> None:
            if body.name is not None:
                pr["name"] = body.name.strip() or pr["name"]
            if body.script is not None:
                pr["script"] = {**(pr.get("script") or {}), **body.script.model_dump()}
            if body.settings is not None:
                changes = body.settings.model_dump(exclude_none=True)
                if "fields" in changes:
                    changes["fields"] = {k: str(v)[:120] for k, v in changes["fields"].items()}
                pr["settings"].update(changes)

        return view(store.update(pid, fn))

    @app.post("/api/photo-projects/{pid}/script")
    def regenerate(pid: str, ai: bool = True):
        project(pid)
        return {"job_id": start_script(pid, ai)}

    @app.post("/api/photo-projects/{pid}/music")
    def upload_music(pid: str, music: UploadFile = File(...)):
        project(pid)
        ext = Path(music.filename or "").suffix.lower()
        if ext not in MUSIC_EXT:
            raise HTTPException(400, "음악은 mp3, m4a, wav, ogg 파일을 올려 주세요")
        dest = store.dir(pid) / "audio" / f"music{ext}"
        with dest.open("wb") as f:
            shutil.copyfileobj(music.file, f)
        try:
            from highlight_engine.photos import decode_audio

            if len(decode_audio(str(dest))) < AUDIO_SR:
                raise ValueError
        except Exception:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "음악 파일을 읽을 수 없어요")
        return view(store.update(pid, lambda pr: pr["settings"].update(music=dest.name, music_name=(music.filename or "")[:80])))

    @app.delete("/api/photo-projects/{pid}/music")
    def delete_music(pid: str):
        p = project(pid)
        if p["settings"].get("music"):
            (store.dir(pid) / "audio" / p["settings"]["music"]).unlink(missing_ok=True)
        return view(store.update(pid, lambda pr: pr["settings"].update(music=None, music_name=None)))

    # ------------------------------------------------------------------ render
    def narration(pid: str, text: str, voice: str | None) -> np.ndarray | None:
        tts = app.state.tts
        if not tts or not text.strip():
            return None
        key = hashlib.sha1(f"{tts.name}|{voice}|{text}".encode()).hexdigest()[:16]
        cache = store.dir(pid) / "audio" / f"tts_{key}.f32"
        if cache.exists():
            return np.fromfile(cache, dtype=np.float32)
        samples = tts_mod.narrate(tts, text, voice)
        samples.tofile(cache)
        return samples

    def render_job(pid: str, *, progress) -> dict:
        p = store.get(pid)
        s, script = p["settings"], p["script"]
        style = with_brand(s.get("preset") or "legend")
        slides = []
        use_voice = bool(s.get("voice") and app.state.tts)
        for i, sl in enumerate(script["slides"]):
            voice = None
            if use_voice:
                progress(0.02 + 0.2 * i / len(script["slides"]), "AI 목소리 만드는 중")
                try:
                    voice = narration(pid, sl["text"], s.get("voice_id"))
                except tts_mod.TTSError as e:
                    raise RuntimeError(str(e))
            dur = float(sl.get("duration") or estimate_duration(sl["text"]))
            if voice is not None:
                dur = max(dur, len(voice) / AUDIO_SR + 0.7)  # the slide waits for the voice
            slides.append(Slide(str(store.dir(pid) / "photos" / sl["photo"]), sl["text"], dur, voice))
        card = card_for(s["template"], s.get("fields", {})) if s.get("end_card", True) else None
        music = str(store.dir(pid) / "audio" / s["music"]) if s.get("music") else None
        name = f"photo_{int(time.time() * 1000)}.mp4"
        out = store.dir(pid) / "renders" / name
        progress(0.25, "영상 만드는 중")
        meta = render_slideshow(slides, str(out), style=style, title=script.get("title") if s.get("show_title") else None,
                                keywords=script.get("keywords", []), end_card=card, music=music,
                                music_volume=float(s.get("music_volume", 0.3)),
                                progress=lambda f: progress(0.25 + 0.73 * f, "영상 만드는 중"))

        def done(pr: dict) -> None:
            old = (pr.get("render") or {}).get("file")
            pr["render"] = {"file": name, "rendered_at": time.time(), **meta}
            pr["duration"] = meta["duration"]
            if old and old != name:
                (store.dir(pid) / "renders" / old).unlink(missing_ok=True)

        store.update(pid, done)
        return {"file": name, "url": f"/api/photo-projects/{pid}/files/{name}", **meta}

    @app.post("/api/photo-projects/{pid}/render")
    def render(pid: str):
        p = project(pid)
        if not p.get("script") or not p["script"].get("slides"):
            raise HTTPException(400, "대본이 아직 없어요")
        return {"job_id": jobs.submit("photo_render", lambda *, progress: render_job(pid, progress=progress),
                                      project=pid, owner=p["owner"])}

    @app.get("/api/photo-projects/{pid}/files/{name}")
    def render_file(pid: str, name: str, download: bool = False):
        path = store.dir(pid) / "renders" / Path(name).name
        if not path.exists() or path.suffix != ".mp4":
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4", filename=name if download else None)

    # ------------------------------------------------------------------ publish
    @app.post("/api/photo-projects/{pid}/publish")
    def publish(pid: str, body: PhotoPublish):
        p = project(pid)
        if not p.get("render"):
            raise HTTPException(400, "먼저 영상을 만들어 주세요")
        acc = registry.for_user(p["owner"])
        if not acc.load():
            raise HTTPException(400, "먼저 YouTube 채널을 연결하세요")
        tags = [h.lstrip("#") for h in (p.get("script") or {}).get("hashtags", []) if h.strip("#")]
        meta = VideoMeta(title=body.title, description=body.description, tags=tags, privacy=body.privacy,
                         publish_at=body.publish_at or None)
        try:
            meta.validate()
        except YouTubeError as e:
            raise HTTPException(400, str(e))
        path = store.dir(pid) / "renders" / p["render"]["file"]

        def work(*, progress) -> dict:
            video = acc.upload(path, meta, lambda f: progress(f, "YouTube에 업로드 중"))
            url = f"https://youtube.com/shorts/{video['id']}"
            store.update(pid, lambda pr: pr.update(publish={"url": url, "youtube_id": video["id"], "at": time.time(),
                                                            "publish_at": meta.body()["status"].get("publishAt")}))
            return {"url": url}

        return {"job_id": jobs.submit("photo_publish", work, project=pid, owner=p["owner"])}
