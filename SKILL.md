---
name: douyin-subtitle-skill
description: Extract subtitles, public metadata, public images, OCR text, ASR transcripts, visual summaries, and timeline exports from Douyin or Bilibili links, share text, local videos, audio, screen recordings, screenshots, or image galleries. Use when the user asks to process Douyin/Bilibili video subtitles, generate TXT/SRT/Markdown/JSON outputs, summarize PPT-like video visuals, analyze burned-in subtitles, or fall back to legal media acquisition without bypassing login, CAPTCHA, paywalls, private content, or platform restrictions.
---

# Douyin / Bilibili Subtitle and Visual Understanding Skill

## Quick Start

Use `scripts/douyin_subtitle.py` for the concrete implementation. The script enforces the processing order in this file: normalize input, inspect public metadata/native subtitles/public images, then use legal media acquisition only as a fallback, and finally process user-provided or acquired media with OCR/ASR when available.

Preview/public-source pass for a link:

```powershell
python "D:\skill\douyin-subtitle-skill\scripts\douyin_subtitle.py" "<Douyin or Bilibili share text or URL>" -o "D:\skill\douyin-subtitle-skill\outputs\run-001"
```

Allow legal media acquisition only after public sources fail:

```powershell
python "D:\skill\douyin-subtitle-skill\scripts\douyin_subtitle.py" "<URL>" -o "D:\skill\douyin-subtitle-skill\outputs\run-001" --allow-media
```

Process a user-uploaded/local video, audio, screenshot, or image directory:

```powershell
python "D:\skill\douyin-subtitle-skill\scripts\douyin_subtitle.py" "<local file or directory>" -o "D:\skill\douyin-subtitle-skill\outputs\local-001" --visual-mode auto
```

Useful options:

- `--visual-mode off|subtitle_ocr|slide_summary|slide_diff_summary|auto`
- `--allow-media`: permit legal video/audio acquisition fallback after public sources fail.
- `--allow-external-api`: permit configured non-localhost parsing APIs after public sources fail. Do not use this unless the user accepts sending the URL to that API.
- `--offline`: dry-run local normalization/export behavior without network access.
- `--no-asr`: skip ASR on local or acquired media.

The script exports `transcript.txt`, `subtitle.srt`, `visual_report.md`, and `timeline.json`. `subtitle.srt` only contains `native_subtitle`, `asr_audio`, or `ocr_burned_subtitle` segments; visual summaries stay in `visual_report.md` and `timeline.json`.

## Privacy Configuration

Do not commit secrets, cookies, tokens, downloaded media, generated outputs, or local overrides. Keep private values in environment variables or ignored files:

- Set `DOUYIN_SUBTITLE_VIDEO_ANALYSE_TOKEN` for the optional `video_analyse` API token.
- Put machine-local overrides in `config.local.yaml`; this file is ignored by `.gitignore`.
- Keep `outputs/`, `tmp/`, media files, `.env`, and logs out of GitHub.

External parsing APIs are disabled by default in `config.yaml` and skipped unless `--allow-external-api` is passed. The script redacts token-like fields before writing `timeline.json`.

## Purpose

This skill extracts subtitles, public page information, image content, and visual summaries from Douyin or Bilibili links.

The skill should not treat video downloading as the first step. It should first try low-intrusion information sources such as native subtitles, public page metadata, public images, and visible page text.

Only when these sources are insufficient should the skill attempt legal media acquisition using configured tools.

The skill must not bypass login, CAPTCHA, anti-bot checks, paywalls, private content protection, or download restrictions.

---

## Core Principle

Process in this order:

```text
1. Native subtitles
2. Public page metadata
3. Public images / gallery / visible page information
4. Visual OCR / PPT / slide-diff analysis when visual data is available
5. Legal media acquisition as fallback
6. User-uploaded video / recording / audio / screenshots as final fallback
7. ASR / OCR / visual analysis
8. Export TXT / SRT / Markdown / JSON
```

Video or audio acquisition is not the default first step.

---

## Supported Inputs

The skill accepts:

- Douyin video URL
- Bilibili video URL
- Share text containing a URL
- Local video file
- Local audio file
- Screen recording
- Screenshot sequence
- Image gallery

---

## Supported Outputs

The skill can output:

- `transcript.txt`
- `subtitle.srt`
- `visual_report.md`
- `timeline.json`

`subtitle.srt` should only contain actual spoken content or clearly visible subtitle text.

PPT summaries, image summaries, chart explanations, and slide-diff analysis should go into `visual_report.md` and `timeline.json`, not into SRT.

---

# Core Processing Order

## Step 1: Normalize Input

When the user provides a link or share text:

1. Extract the actual URL.
2. Detect the platform:
   - Douyin
   - Bilibili
   - Unknown
3. Save the original input and normalized URL.
4. Do not download media at this stage.

Example state:

```json
{
  "status": "url_normalized",
  "platform": "douyin",
  "normalized_url": "https://www.douyin.com/video/xxx"
}
```

---

## Step 2: Extract Public Page Metadata

Before trying to download video or audio, attempt to read publicly available page information.

Possible metadata includes:

- title
- description
- author
- upload time
- cover image
- page text
- video duration
- public image gallery
- public embedded subtitle references

If useful page information is found, save it into `timeline.json`.

Example source label:

```json
{
  "source": "public_page_metadata"
}
```

---

## Step 3: Try Native Subtitle Extraction

After page metadata extraction, try to find native subtitles or caption tracks.

Possible subtitle sources:

- Bilibili subtitle track
- Bilibili AI subtitle
- Douyin public caption data if available
- subtitle metadata embedded in public page data
- VTT / SRT / JSON subtitle resources

If native subtitles are found:

1. Parse the subtitle segments.
2. Export:
   - `subtitle.srt`
   - `transcript.txt`
   - `timeline.json`
   - `visual_report.md` if visual content is also requested
3. Mark the source as:

```json
{
  "source": "native_subtitle"
}
```

4. Do not download the video unless the user explicitly asks for visual analysis that requires frames.

---

## Step 4: Analyze Public Images and Page Visual Information

If no native subtitle is available, inspect whether the page contains publicly accessible images or visual content.

This includes:

- video cover
- public image gallery
- article images
- screenshot-style content
- PPT-style image posts
- visible text in images

If public images are available, run:

- OCR
- image text extraction
- image summary
- PPT / slide summary
- visual report generation

Example source label:

```json
{
  "source": "public_image_or_gallery"
}
```

Important rule:

```text
Only run visual analysis when there is actual accessible visual input:
public images, public gallery images, extracted frames, uploaded screenshots, uploaded video, or screen recording.
```

---

# Visual Analysis Modes

The skill supports optional visual analysis modes.

```yaml
visual_mode:
  - off
  - subtitle_ocr
  - slide_summary
  - slide_diff_summary
  - auto
```

## off

Do not analyze visual content.

## subtitle_ocr

Use this mode when the video or image contains burned-in subtitles.

Process:

1. Crop likely subtitle area, usually bottom 30% to 40%.
2. Run OCR.
3. Merge repeated text.
4. Generate subtitle-like segments.

Source label:

```json
{
  "source": "ocr_burned_subtitle"
}
```

## slide_summary

Use this mode when the content looks like:

- PPT
- screenshots
- documents
- charts
- tables
- flowcharts
- image-based explanations

Output should explain:

- title
- visible text
- main idea
- chart/table meaning
- relation to transcript if available

Source label:

```json
{
  "source": "visual_slide_summary"
}
```

## slide_diff_summary

Use this mode when PPT or image content appears progressively.

Example:

```text
00:00 title appears
00:03 first bullet appears
00:06 second bullet appears
00:09 chart appears
```

The skill should not repeatedly summarize the whole screen.

Instead, it should compare frames or images and summarize only newly added content.

Process:

1. Extract keyframes or compare provided screenshots.
2. Group visually similar frames as the same slide.
3. Detect stable frames.
4. Compare current frame with previous stable frame.
5. Identify newly added text.
6. Identify newly added visual elements such as arrows, boxes, highlights, images, tables, or charts.
7. Summarize incremental changes.
8. Merge all changes into a final slide summary.

Example output:

```json
{
  "slide_id": 1,
  "start": "00:00:00.000",
  "end": "00:00:12.000",
  "title": "短视频账号增长模型",
  "build_steps": [
    {
      "time": "00:00:00.000",
      "change_type": "initial_state",
      "added_text": ["短视频账号增长模型"],
      "summary": "出现页面标题。"
    },
    {
      "time": "00:00:03.000",
      "change_type": "text_addition",
      "added_text": ["内容定位"],
      "summary": "新增第一步：确定内容方向。"
    }
  ],
  "final_summary": "这一页说明短视频账号从定位、测试到放大的流程。"
}
```

---

# Step 5: Decide Whether Media Acquisition Is Needed

Only attempt video or audio acquisition if all previous sources are insufficient.

Media acquisition should be used only when:

- no native subtitles are available
- public page metadata is insufficient
- public images or gallery information are insufficient
- user needs spoken content
- user needs frame-level visual analysis
- user explicitly asks for transcript, audio, or video-based analysis

The skill must not download video as the first step.

---

# Step 6: Legal Media Acquisition Fallback

When media acquisition is needed, try configured tools in this order:

```yaml
media_acquisition:
  enabled: true
  use_only_after:
    - native_subtitle_failed
    - public_metadata_insufficient
    - public_visual_info_insufficient

  tools:
    lux:
      priority: 1
      type: cli
      command: "lux"
      supports:
        - douyin
        - bilibili

    video_analyse:
      priority: 2
      type: api
      endpoint: "https://proxy.layzz.cn/lyz/platAnalyse/"
      supports:
        - douyin
        - bilibili

    galaxy_downloader:
      priority: 3
      type: api
      endpoint: "http://localhost:8788/api/parse"
      supports:
        - douyin
        - bilibili
```

## Tool Behavior

### lux

Use `lux` only after previous information sources are insufficient.

Recommended commands:

```bash
lux -j "URL"
```

for metadata / JSON extraction.

```bash
lux -o outputs "URL"
```

for media download when needed.

### video-analyse

Use as API fallback.

Expected useful fields:

```json
{
  "playAddr": "video_url",
  "music": "audio_url",
  "pics": ["image_url"],
  "videos": ["video_url"],
  "desc": "description"
}
```

### galaxy-downloader

Use only if an API endpoint is configured and reachable.

Do not assume the frontend alone can parse or download media.

---

# Step 7: Process Acquired or Uploaded Media

If video or audio is acquired legally, process it.

## For video

Run:

1. audio extraction
2. ASR transcription
3. frame extraction
4. burned-in subtitle OCR
5. slide summary
6. slide-diff summary if needed

## For audio

Run:

1. ASR transcription
2. export transcript and SRT

## For screenshots

Run:

1. OCR
2. image summary
3. slide comparison if multiple screenshots are provided

---

# Step 8: ASR Transcription

Use ASR only when native subtitles are unavailable or incomplete.

Recommended engine:

```yaml
asr:
  enabled: true
  engine: "faster-whisper"
  model: "medium"
  language: "zh"
```

Source label:

```json
{
  "source": "asr_audio"
}
```

ASR output should be treated as generated text and may require manual review.

---

# Step 9: OCR Subtitle Extraction

Use OCR when visual subtitles appear on screen.

Recommended process:

1. Sample frames every 0.5 or 1 second.
2. Crop bottom subtitle region.
3. Run OCR.
4. Remove duplicates.
5. Merge continuous identical or similar text.
6. Generate subtitle segments.

Source label:

```json
{
  "source": "ocr_burned_subtitle"
}
```

---

# Step 10: User Upload Fallback

If no native subtitles are available and media cannot be legally acquired, do not bypass restrictions.

Ask the user to upload one of:

- original video
- screen recording
- audio recording
- screenshot sequence
- image files

Response template:

```text
当前链接没有可读取的原生字幕，公开页面信息也不足，并且无法在允许的方式下获取视频或音频。

我不会绕过下载限制。请上传以下任意一种文件，我可以继续处理：

1. 原视频
2. 手机或电脑录屏
3. 单独音频
4. 视频截图序列
5. 图片/PPT截图

上传后我可以继续生成 TXT、SRT、视觉总结和时间线 JSON。
```

Source label after upload:

```json
{
  "source": "user_uploaded_media"
}
```

---

# Source Labels

Every extracted segment must include a source label.

Allowed source labels:

```yaml
source_labels:
  - native_subtitle
  - public_page_metadata
  - public_image_or_gallery
  - asr_audio
  - ocr_burned_subtitle
  - visual_slide_summary
  - visual_slide_diff
  - legal_media_acquisition
  - user_uploaded_media
  - unavailable
```

Confidence order:

```text
native_subtitle
  > ocr_burned_subtitle
  > asr_audio
  > public_page_metadata
  > visual_slide_summary
  > visual_slide_diff
```

---

# Output Rules

## transcript.txt

Contains readable transcript text.

Can include:

- native subtitle text
- ASR transcript
- OCR subtitle text

Should not include long PPT visual summaries.

## subtitle.srt

Contains only timestamped subtitle content.

Allowed content:

- native subtitles
- ASR speech segments
- OCR burned-in subtitles

Not allowed:

- PPT summaries
- chart explanations
- image interpretations
- inferred conclusions

## visual_report.md

Contains:

- image summaries
- PPT summaries
- slide-diff summaries
- chart/table explanations
- screenshot explanations
- relationship between visual content and transcript

## timeline.json

Contains all structured results.

Example:

```json
{
  "url": "https://example.com/video",
  "platform": "douyin",
  "status": "completed",
  "segments": [
    {
      "start": "00:00:00.000",
      "end": "00:00:03.000",
      "text": "今天我们讲一下账号定位。",
      "source": "asr_audio"
    }
  ],
  "visual_segments": [
    {
      "start": "00:00:03.000",
      "end": "00:00:10.000",
      "title": "账号定位",
      "summary": "这一页讲账号定位需要明确用户、内容和变现方向。",
      "source": "visual_slide_summary"
    }
  ]
}
```

---

# Status Machine

The skill should track processing state.

Possible statuses:

```yaml
statuses:
  - input_received
  - url_normalized
  - public_metadata_found
  - native_subtitle_found
  - native_subtitle_not_found
  - public_visual_info_found
  - public_visual_info_insufficient
  - media_acquisition_needed
  - media_acquired
  - media_acquisition_failed
  - user_upload_required
  - user_media_received
  - processing_completed
  - unavailable
```

Example:

```json
{
  "status": "media_acquisition_needed",
  "reason": "no_native_subtitle_and_public_info_insufficient",
  "next_action": "try_configured_media_tools"
}
```

Example when blocked:

```json
{
  "status": "user_upload_required",
  "reason": "no_subtitle_no_public_media_no_download_permission",
  "next_action": "ask_user_upload"
}
```

---

# Safety and Compliance Rules

The skill must not:

- bypass login requirements
- bypass CAPTCHA
- bypass anti-bot mechanisms
- bypass private content restrictions
- bypass paywalls
- bypass platform download restrictions
- remove watermark for redistribution
- claim ASR or OCR results are perfectly accurate

The skill may:

- process public metadata
- process public subtitles
- process public images
- process user-provided media
- use legally accessible media resources
- generate transcripts for personal study, accessibility, indexing, or summarization

---

# Final User-Facing Behavior

When the user provides a Douyin or Bilibili link, the skill should say:

```text
我会先尝试读取原生字幕、标题、描述、封面和公开图文信息。
如果没有可用字幕或公开信息不足，我会再尝试使用已配置工具合法获取视频或音频。
如果无法获取媒体，我会请你上传录屏、视频、音频或截图后继续处理。
```

When completed, respond with:

```text
已完成处理。

输出文件：
- transcript.txt
- subtitle.srt
- visual_report.md
- timeline.json

内容来源：
- 字幕来源：native_subtitle / asr_audio / ocr_burned_subtitle
- 视觉来源：public_image_or_gallery / visual_slide_summary / visual_slide_diff
- 是否需要人工复核：是 / 否
```
