"""
Speed bot: once an hour, checks IShowSpeed's YouTube streams. When a stream has
ended, it scans the stream's audio for the loudest moments (screaming, hype),
cuts those parts, edits them vertical with captions, and sends them to Discord.
"""
import os, json, subprocess, time, datetime, shutil
from pathlib import Path
import numpy as np
import requests
import clipbot as cb

ROOT = Path(__file__).parent
STATE = ROOT / "state" / "speed.json"
WORK = Path("/tmp/speedwork")
CHANNEL = "https://www.youtube.com/@IShowSpeed/streams"
CLIPS_PER_STREAM = 5
SPEED = {"login": "IShowSpeed", "hashtags": "#ishowspeed #speed",
         "cam_box": [0.0, 0.42, 0.205, 0.23], "game_box": [0.205, 0.0, 0.6, 1.0]}


COOKIES = Path("/tmp/yt_cookies.txt")
if os.environ.get("YT_COOKIES", "").strip() and not COOKIES.exists():
    COOKIES.write_text(os.environ["YT_COOKIES"].strip() + "\n")


def ytdlp(*args, timeout=3600):
    extra = ["--cookies", str(COOKIES)] if COOKIES.exists() else []
    return subprocess.run(["yt-dlp", "--no-warnings", *extra, *args],
                          capture_output=True, text=True, timeout=timeout)


def list_streams():
    r = ytdlp("--flat-playlist", "--playlist-end", "4", "-J", CHANNEL, timeout=180)
    if r.returncode:
        raise RuntimeError("could not list Speed's streams: " + r.stderr[-300:])
    return json.loads(r.stdout).get("entries", []) or []


LIVE_URL = "https://www.youtube.com/@IShowSpeed/live"
LIVE_RECORD_MIN = 45   # each hourly run records this many minutes of the live stream
LIVE_CLIPS = 3


def live_info():
    """If Speed is live right now, returns (video_id, title, stream_url). Otherwise None."""
    r = ytdlp("-f", "best[height<=720]/best", "--print", "id", "--print", "title", "-g", LIVE_URL, timeout=120)
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    if r.returncode or len(lines) < 3:
        return None
    return lines[0], lines[1], lines[-1]


def record_live(stream_url, out, minutes):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", stream_url, "-t", str(minutes * 60),
                    "-c", "copy", str(out)], timeout=minutes * 60 + 600)


def cut_local(src, start, length, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-i", str(src), "-t", str(length),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", str(out)], check=True)


def process_live(vid, title, stream_url):
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    rec = WORK / "live.mkv"
    print("Speed is live, recording", LIVE_RECORD_MIN, "min")
    record_live(stream_url, rec, LIVE_RECORD_MIN)
    if not rec.exists() or rec.stat().st_size < 1_000_000:
        raise RuntimeError("live recording failed (YouTube may be blocking the download)")
    peaks = loud_moments(rec, LIVE_CLIPS, skip_start=0)
    print("live loud moments (sec):", peaks)
    entry = {"title": title}
    url = "https://www.youtube.com/watch?v=" + vid
    for i, t in enumerate(peaks, 1):
        start = max(0, t - 25)
        sec = WORK / f"lsec{i}.mp4"
        cut_local(rec, start, 40, sec)
        dur = cb.probe_duration(sec)
        ass = WORK / f"ls{i}.ass"
        ass.write_text(cb.build_ass(cb.transcribe(sec), "SPEED LOST IT", "WAIT FOR IT...", "cam", dur))
        final = WORK / f"lfinal{i}.mp4"
        cb.render(sec, final, SPEED, ass, dur)
        send_speed(final, entry, i, len(peaks), None, url)
        time.sleep(2)


def loud_moments(audio, n, skip_start=180):
    """Returns up to n clearly-loud moments (in seconds), at least 2 minutes apart."""
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], stdout=subprocess.PIPE)
    db = []
    while True:
        b = p.stdout.read(16000)  # one second
        if len(b) < 16000:
            break
        x = np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768
        db.append(20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-9))
    p.wait()
    db = np.array(db)
    if len(db) < 300:
        return []
    roll = np.convolve(db, np.ones(4) / 4, "same")
    roll[:skip_start] = -200  # skip the stream intro
    roll[-20:] = -200         # need room after the moment
    floor = np.median(db) + 6  # must be clearly louder than normal
    picks = []
    for i in np.argsort(-roll):
        if roll[i] < floor:
            break
        if all(abs(int(i) - q) > 120 for q in picks):
            picks.append(int(i))
        if len(picks) == n:
            break
    return sorted(picks)


def send_speed(path, entry, i, n, start, url):
    title = cb.clean(entry.get("title") or "Speed stream")[:70]
    msg = (f"🎬 **SPEED MOMENT {i}/{n}** from \"{title}\"\n"
           f"📈 Growth clip (no campaign). Watch it first and only post it if it's actually good.\n\n"
           f"**TikTok caption:**\n```Speed lost it 😭 #ishowspeed #speed #streamer #funny #fyp```\n"
           f"**YouTube title:**\n```Speed lost it 😭 #ishowspeed #speed #shorts```\n"
           f"**YouTube description:**\n```IShowSpeed live stream moment. Full stream on IShowSpeed's YouTube channel. #ishowspeed #speed```\n"
           f"Source: <{url}{'' if start is None else f'&t={int(start)}s'}>")
    with open(path, "rb") as f:
        r = requests.post(cb.WEBHOOK, data={"payload_json": json.dumps({"content": msg[:1990]})},
                          files={"files[0]": (f"speed_{i}.mp4", f, "video/mp4")}, timeout=120)
    r.raise_for_status()


def report_error(state, err):
    print("ERROR:", err)
    today = datetime.date.today().isoformat()
    if state.get("last_error_day") == today:
        return  # only warn once a day
    state["last_error_day"] = today
    try:
        requests.post(cb.WEBHOOK, json={"content": f"⚠️ Speed bot problem: {str(err)[:400]}\nSend this message to Claude."}, timeout=30)
    except Exception:
        pass


def process(url, entry):
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    r = ytdlp("-f", "wa/ba", "-o", str(WORK / "audio.%(ext)s"), url, timeout=3600)
    if r.returncode:
        raise RuntimeError("audio download failed: " + r.stderr[-300:])
    audio = next(WORK.glob("audio.*"))
    peaks = loud_moments(audio, CLIPS_PER_STREAM)
    print("loud moments (sec):", peaks)
    sent = 0
    for i, t in enumerate(peaks, 1):
        start, end = max(0, t - 25), t + 15
        sec = WORK / f"sec{i}.mp4"
        r = ytdlp("-f", "bv*[height<=720]+ba/b[height<=720]", "--download-sections", f"*{start}-{end}",
                  "--merge-output-format", "mp4", "-o", str(sec), url, timeout=900)
        if r.returncode or not sec.exists():
            print("section download failed:", r.stderr[-300:])
            continue
        dur = cb.probe_duration(sec)
        words = cb.transcribe(sec)
        ass = WORK / f"s{i}.ass"
        ass.write_text(cb.build_ass(words, "SPEED LOST IT", "WAIT FOR IT...", "cam", dur))
        final = WORK / f"final{i}.mp4"
        cb.render(sec, final, SPEED, ass, dur)
        send_speed(final, entry, i, len(peaks), start, url)
        sent += 1
        time.sleep(2)
    if peaks and not sent:
        raise RuntimeError("found moments but could not download any of them")


def main():
    STATE.parent.mkdir(exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {"done": [], "last_error_day": ""}
    live = live_info()
    if live:
        vid, title, stream_url = live
        if vid not in state["done"]:
            state["done"].append(vid)  # we clip it live, so skip its replay later
        try:
            process_live(vid, title, stream_url)
        except Exception as ex:
            report_error(state, ex)
        STATE.write_text(json.dumps(state, indent=0))
        return
    try:
        entries = list_streams()
    except Exception as e:
        report_error(state, e)
        STATE.write_text(json.dumps(state, indent=0))
        return
    ended = [e for e in entries if e.get("id") and e.get("duration") and e.get("live_status") != "is_live"]
    if not state["done"] and ended:
        state["done"] = [e["id"] for e in ended[1:]]  # first run: only do the newest stream
    todo = [e for e in ended if e["id"] not in state["done"]][:1]
    print(f"{len(todo)} new ended stream(s)")
    for e in todo:
        url = "https://www.youtube.com/watch?v=" + e["id"]
        try:
            process(url, e)
        except Exception as ex:
            report_error(state, ex)
        state["done"].append(e["id"])
    state["done"] = state["done"][-200:]
    STATE.write_text(json.dumps(state, indent=0))


if __name__ == "__main__":
    main()
