"""
Caught On Stream clip bot.
Every run: finds hot new Twitch clips for the streamers in config.json,
edits them into vertical 9:16 videos with captions, and sends them to Discord
with ready-to-paste captions. Runs free on GitHub Actions.
"""
import os, json, time, subprocess, datetime, shutil, re
from pathlib import Path
import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_FILE = ROOT / "state" / "posted.json"
WORK = Path("/tmp/clipwork")

CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
MAX_MB = 9.3  # Discord free upload limit is 10 MB


# ---------------------------------------------------------------- Twitch
def twitch_token():
    r = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def helix(path, token, params):
    r = requests.get("https://api.twitch.tv/helix/" + path, params=params, timeout=30,
                     headers={"Client-Id": CLIENT_ID, "Authorization": "Bearer " + token})
    r.raise_for_status()
    return r.json().get("data", [])


def find_new_clips(token, posted):
    logins = [s["login"] for s in CONFIG["streamers"]]
    users = {u["login"].lower(): u for u in helix("users", token, [("login", l) for l in logins])}
    game_ids = {}
    since = (datetime.datetime.utcnow() - datetime.timedelta(hours=CONFIG["lookback_hours"])
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    picks = []
    for s in CONFIG["streamers"]:
        u = users.get(s["login"].lower())
        if not u:
            print("streamer not found:", s["login"]); continue
        game_id = None
        if s.get("game"):
            if s["game"] not in game_ids:
                g = helix("games", token, {"name": s["game"]})
                game_ids[s["game"]] = g[0]["id"] if g else None
            game_id = game_ids[s["game"]]
        clips = helix("clips", token, {"broadcaster_id": u["id"], "started_at": since, "first": 50})
        good = [c for c in clips
                if c["id"] not in posted
                and c["view_count"] >= s["min_views"]
                and c["duration"] >= CONFIG["min_seconds"]
                and (not game_id or not c.get("game_id") or c["game_id"] == game_id)]
        good.sort(key=lambda c: c["view_count"], reverse=True)
        for c in good[: s.get("max_per_run", 2)]:
            picks.append((s, u["display_name"], c))
    picks.sort(key=lambda p: p[2]["view_count"], reverse=True)
    return picks[: CONFIG["max_per_run_total"]]


# ---------------------------------------------------------------- captions
def ass_time(t):
    t = max(0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def clean(txt):
    return re.sub(r"[{}\\\n\r]", " ", txt).strip()


def transcribe(path):
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("base.en", device="cpu", compute_type="int8")
        segs, _ = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
        return [(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or []) if w.word.strip()]
    except Exception as e:
        print("transcribe failed:", e)
        return []


def build_ass(words, hook1, hook2, layout, duration):
    cap_y = 935 if layout == "cam" else 1330
    hook_y = 70 if layout == "cam" else 260
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,DejaVu Sans,74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,8,50,50,{hook_y},1
Style: Cap,DejaVu Sans,80,&H0000D2FF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,7,0,8,60,60,{cap_y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{ass_time(0)},{ass_time(duration + 1)},Hook,,0,0,0,,"
             f"{clean(hook1).upper()}\\N{{\\c&H0000D2FF&}}{clean(hook2).upper()}"]
    # group words: max 3 words or 1.3 s per caption
    chunk = []
    for w in words:
        if chunk and (len(chunk) >= 3 or w[1] - chunk[0][0] > 1.3):
            lines.append(cap_line(chunk)); chunk = []
        chunk.append(w)
    if chunk:
        lines.append(cap_line(chunk))
    return head + "\n".join(lines) + "\n"


def cap_line(chunk):
    text = clean(" ".join(w[2] for w in chunk)).upper()
    return f"Dialogue: 1,{ass_time(chunk[0][0])},{ass_time(chunk[-1][1] + 0.05)},Cap,,0,0,0,,{text}"


# ---------------------------------------------------------------- editing
def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return float(out.strip() or 0)


def box(b):
    x, y, w, h = b
    return f"crop={int(1920*w)}:{int(1080*h)}:{int(1920*x)}:{int(1080*y)}"


def render(src, out, streamer, ass_path, duration):
    cam = streamer.get("cam_box")
    if cam:
        game = streamer.get("game_box") or [0.2, 0.0, 0.6, 1.0]
        vf = (f"[0:v]scale=1920:1080,setsar=1,split=2[a][b];"
              f"[a]{box(cam)},scale=1080:606,setsar=1[cam];"
              f"[b]{box(game)},scale=1080:1012,setsar=1[game];"
              f"color=c=0x0d0812:s=1080x1920:r=30[bg];"
              f"[bg][cam]overlay=0:296:shortest=1[t1];[t1][game]overlay=0:908[t2];"
              f"[t2]drawbox=x=0:y=902:w=1080:h=6:color=0xff2d6f:t=fill,ass={ass_path}[v]")
    else:
        vf = (f"[0:v]scale=1920:1080,setsar=1,split=2[a][b];"
              f"[a]scale=-2:1920,crop=1080:1920,boxblur=25:2,eq=brightness=-0.18[bgv];"
              f"[b]scale=1080:-2[fg];[bgv][fg]overlay=0:(H-h)/2,ass={ass_path}[v]")
    kbps = int(MAX_MB * 8 * 1024 / max(duration, 5)) - 130
    kbps = max(700, min(kbps, 6000))
    for _ in range(3):
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", vf,
               "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
               "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps*1.15)}k", "-bufsize", f"{kbps*2}k",
               "-r", "30", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
        subprocess.run(cmd, check=True)
        if out.stat().st_size <= MAX_MB * 1024 * 1024:
            return
        kbps = int(kbps * 0.75)
    raise RuntimeError("could not get video under Discord size limit")


# ---------------------------------------------------------------- discord
def send_discord(path, streamer, name, clip):
    title = clean(clip["title"])[:80]
    tags = streamer["hashtags"]
    tiktok = f"{name} {title} 😭 {tags} #gta6 #gtavi #gta #twitch #streamer"
    yt_title = f"{name} {title} 😭 {tags.split()[0]} #shorts"[:100]
    yt_desc = f"{name} live on Twitch: twitch.tv/{streamer['login']} {tags} #gta6 #twitch"
    if streamer.get("campaign"):
        action = ("💰 **CAMPAIGN CLIP**: post on TikTok + YouTube, then submit BOTH within 30 min at "
                  "https://contentrewards.com/c/discover → " + streamer["campaign"])
    else:
        action = "📈 Growth clip (no campaign): just post on TikTok + YouTube."
    msg = (f"🎬 **NEW {name.upper()} CLIP**: {clip['view_count']} views on Twitch\n"
           f"{action}\n\n**TikTok caption:**\n```{tiktok}```\n**YouTube title:**\n```{yt_title}```\n"
           f"**YouTube description:**\n```{yt_desc}```\nSource: <{clip['url']}>")[:1990]
    with open(path, "rb") as f:
        r = requests.post(WEBHOOK, data={"payload_json": json.dumps({"content": msg})},
                          files={"files[0]": (f"{streamer['login']}_{clip['id'][:20]}.mp4", f, "video/mp4")},
                          timeout=120)
    r.raise_for_status()


# ---------------------------------------------------------------- main
def main():
    STATE_FILE.parent.mkdir(exist_ok=True)
    posted = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
    token = twitch_token()
    picks = find_new_clips(token, set(posted))
    print(f"{len(picks)} new clip(s) to make")
    for streamer, name, clip in picks:
        try:
            shutil.rmtree(WORK, ignore_errors=True); WORK.mkdir(parents=True)
            src = WORK / "src.mp4"
            subprocess.run(["yt-dlp", "-q", "-f", "best[ext=mp4]/best", "-o", str(src), clip["url"]],
                           check=True, timeout=300)
            dur = probe_duration(src)
            words = transcribe(src)
            hook1 = f"{name} in GTA 6" if streamer.get("game") else name
            ass_path = WORK / "subs.ass"
            ass_path.write_text(build_ass(words, hook1, clip["title"], "cam" if streamer.get("cam_box") else "blur", dur))
            out = WORK / "out.mp4"
            render(src, out, streamer, ass_path, dur)
            send_discord(out, streamer, name, clip)
            print("sent:", name, clip["title"], clip["view_count"])
        except Exception as e:
            print("FAILED:", clip.get("url"), e)
        posted.append(clip["id"])  # mark as done either way so we never spam retries
        STATE_FILE.write_text(json.dumps(posted[-1000:], indent=0))
        time.sleep(2)


if __name__ == "__main__":
    main()
