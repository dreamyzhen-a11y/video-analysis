# Douyin Subtitle Skill

This skill processes Douyin/Bilibili links and local media while following the privacy and source-order rules in `SKILL.md`.

Run the implementation:

```powershell
python "D:\skill\douyin-subtitle-skill\scripts\douyin_subtitle.py" "<URL or local file>" -o "D:\skill\douyin-subtitle-skill\outputs\run-001"
```

Use `--allow-media` only after public subtitles/metadata/images are insufficient. Use `--allow-external-api` only when sending the URL to the configured API is acceptable.

Do not commit secrets or generated media. Put private overrides in ignored `config.local.yaml` and use environment variables such as `DOUYIN_SUBTITLE_VIDEO_ANALYSE_TOKEN` for tokens.
