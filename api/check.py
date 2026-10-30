import os, re, json, hashlib
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html.parser import HTMLParser

MOVIES = [
    {"name": "Avengers: Doomsday", "url": "https://egy.voxcinemas.com/movies/avengers-doomsday"},
    {"name": "Avengers: Doomsday (Infinity Vision)", "url": "https://egy.voxcinemas.com/movies/avengers-doomsday-infinity-vision"},
]
TIME_RE = re.compile(r"^\s*(\d{1,2}:\d{2}\s*(?:am|pm))\s*$", re.I)

class TimeLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.href=None; self.parts=[]
        self.cinema=None; self.heading_tag=None; self.heading_parts=[]
    def handle_starttag(self, tag, attrs):
        tag=tag.lower()
        if tag in ("h2", "h3", "h4"):
            self.heading_tag=tag; self.heading_parts=[]
        if tag=="a": self.href=dict(attrs).get("href"); self.parts=[]
    def handle_data(self, data):
        if self.href is not None: self.parts.append(data)
        if self.heading_tag is not None: self.heading_parts.append(data)
    def handle_endtag(self, tag):
        tag=tag.lower()
        if tag==self.heading_tag and self.heading_tag is not None:
            heading=" ".join("".join(self.heading_parts).split())
            if heading and heading.lower() not in ("showtimes", "avengers: doomsday", "avengers: doomsday - showtimes"):
                self.cinema=heading
            self.heading_tag=None; self.heading_parts=[]
        if tag=="a" and self.href is not None:
            label=" ".join("".join(self.parts).split()); href=self.href.strip()
            if TIME_RE.match(label) and href and href!="#" and not href.lower().startswith("javascript:"):
                if href.startswith("/"): href="https://egy.voxcinemas.com"+href
                if href.startswith("http"):
                    self.links.append({"cinema":self.cinema or "Cinema shown on VOX page", "time":label,"booking_url":href})
            self.href=None; self.parts=[]

def http_get(url, headers=None):
    req=Request(url,headers=headers or {"User-Agent":"Mozilla/5.0 (compatible; VOXBookingMonitor/1.0)"})
    with urlopen(req,timeout=20) as r: return r.read().decode("utf-8",errors="replace")

def redis(command):
    req=Request(os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/"),data=json.dumps(command).encode(),headers={"Authorization":"Bearer "+os.environ["UPSTASH_REDIS_REST_TOKEN"],"Content-Type":"application/json"},method="POST")
    with urlopen(req,timeout=15) as r: return json.loads(r.read().decode()).get("result")

def telegram(message):
    url="https://api.telegram.org/bot"+os.environ["TELEGRAM_BOT_TOKEN"]+"/sendMessage"
    data=json.dumps({"chat_id":os.environ["TELEGRAM_CHAT_ID"],"text":message,"disable_web_page_preview":False}).encode()
    result=json.loads(http_get(url,{"Content-Type":"application/json"}) if False else urlopen(Request(url,data=data,headers={"Content-Type":"application/json"}),timeout=15).read().decode())
    if not result.get("ok"): raise RuntimeError("Telegram sendMessage failed")

from http.server import BaseHTTPRequestHandler

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not os.environ.get("MONITOR_SECRET") or self.headers.get("x-monitor-secret") != os.environ["MONITOR_SECRET"]:
            self.send_json(401, {"ok": False, "error": "Unauthorized"}); return
        try:
            results = []
            for movie in MOVIES:
                parser = TimeLinkParser(); parser.feed(http_get(movie["url"]))
                unique, seen = [], set()
                for item in parser.links:
                    key = (item.get("cinema"), item["time"], item["booking_url"])
                    if key not in seen:
                        seen.add(key); unique.append(item)
                results.append({**movie, "showtimes": unique})

            bookable = [x for x in results if x["showtimes"]]
            if not bookable:
                self.send_json(200, {"ok": True, "notified": False, "message": "No clickable screening-time links found"}); return

            fingerprint = hashlib.sha256(json.dumps(bookable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            previous = redis(["GET", "vox:doomsday:last_fingerprint"])
            if previous == fingerprint:
                self.send_json(200, {"ok": True, "notified": False, "message": "No change; duplicate suppressed"}); return

            lines = ["🎬 VOX: Avengers: Doomsday", "", "تم العثور على روابط مواعيد عروض على موقع VOX الرسمي.", "افتح الروابط وتأكد من العرض قبل الدفع."]
            for movie in bookable:
                lines += ["", "🎥 " + movie["name"], movie["url"]]
                for show in movie["showtimes"][:30]:
                    lines.append("• " + show.get("cinema", "") + " | " + show["time"] + " — " + show["booking_url"])
            telegram("\n".join(lines))
            redis(["SET", "vox:doomsday:last_fingerprint", fingerprint])
            self.send_json(200, {"ok": True, "notified": True, "checked_at": datetime.now(timezone.utc).isoformat()})
        except (HTTPError, URLError, TimeoutError) as exc:
            self.send_json(502, {"ok": False, "error": "VOX/network error: " + str(exc)})
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)})

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
