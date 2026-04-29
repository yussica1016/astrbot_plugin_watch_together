"""
一起看 Watch Together —— AstrBot 同步/异步观影插件
设计：叶枔枖  编写：叶克宝
协议：GPL v3

功能：
  模式一（同步观影）：Web播放器 + WebSocket实时同步 + 字幕喂给LLM陪看
  模式二（异步观影）：观影日志 + LLM搜索讨论

视频源：本地上传 / 在线链接 / WebDAV
"""

import os
import re
import json
import sqlite3
import threading
import time

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger

# ============================================================
# 配置
# ============================================================

# 从环境变量读取，或在此修改默认值
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "videos")
SUBTITLE_DIR = os.path.join(DATA_DIR, "subtitles")
DB_PATH = os.path.join(DATA_DIR, "watch.db")

# Web服务端口（API + WebSocket + 前端静态文件）
WEB_PORT = int(os.environ.get("WATCH_TOGETHER_PORT", "8902"))

# WebDAV 配置（可选，不填则不启用）
WEBDAV_URL = os.environ.get("WATCH_TOGETHER_WEBDAV_URL", "")
WEBDAV_USER = os.environ.get("WATCH_TOGETHER_WEBDAV_USER", "")
WEBDAV_PASS = os.environ.get("WATCH_TOGETHER_WEBDAV_PASS", "")

# 房间主人 UID（用于权限判断，填你的QQ号）
OWNER_UID = os.environ.get("WATCH_TOGETHER_OWNER_UID", "")

# ============================================================
# 数据库初始化
# ============================================================

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(SUBTITLE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 房间表（同步观影用）
    c.execute('''CREATE TABLE IF NOT EXISTS rooms (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        video_source TEXT,
        video_type TEXT DEFAULT 'url',
        subtitle_path TEXT,
        status TEXT DEFAULT 'waiting',
        current_time REAL DEFAULT 0,
        is_playing INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # 观影日志表（异步观影用）
    c.execute('''CREATE TABLE IF NOT EXISTS watch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid TEXT,
        title TEXT NOT NULL,
        category TEXT DEFAULT '电影',
        rating INTEGER,
        thoughts TEXT,
        watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.commit()
    conn.close()

# ============================================================
# 字幕解析器（SRT格式）
# ============================================================

def parse_srt(filepath):
    """解析SRT字幕文件，返回 [(start_sec, end_sec, text), ...]"""
    if not filepath or not os.path.exists(filepath):
        return []

    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    blocks = re.split(r'\n\s*\n', content.strip())
    subtitles = []

    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        # 解析时间轴
        time_match = re.match(
            r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
            lines[1]
        )
        if not time_match:
            continue
        g = time_match.groups()
        start = int(g[0])*3600 + int(g[1])*60 + int(g[2]) + int(g[3])/1000
        end = int(g[4])*3600 + int(g[5])*60 + int(g[6]) + int(g[7])/1000
        text = ' '.join(lines[2:])
        # 去除HTML标签
        text = re.sub(r'<[^>]+>', '', text)
        subtitles.append((start, end, text))

    return subtitles


def get_subtitles_in_range(subtitles, from_sec, to_sec):
    """获取指定时间范围内的字幕文本"""
    result = []
    for start, end, text in subtitles:
        if start >= from_sec and start <= to_sec:
            result.append(text)
    return '\n'.join(result)

# ============================================================
# Web 服务（Flask + WebSocket）
# ============================================================

def start_web_server():
    """启动Web服务，提供API + WebSocket + 静态文件"""
    try:
        from flask import Flask, jsonify, request, send_from_directory, send_file
        from flask_sock import Sock
    except ImportError:
        import subprocess
        subprocess.check_call([
            "pip", "install", "flask", "flask-sock",
            "--break-system-packages", "-q"
        ])
        from flask import Flask, jsonify, request, send_from_directory, send_file
        from flask_sock import Sock

    app = Flask(__name__, static_folder=os.path.join(PLUGIN_DIR, "web"))
    app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB 视频上传上限
    sock = Sock(app)

    # CORS 支持（允许跨域访问API）
    @app.after_request
    def add_cors(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    # WebSocket 连接池：room_id -> [ws1, ws2, ...]
    ws_rooms = {}

    # ---- 静态文件 ----

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(app.static_folder, filename)

    # ---- 房间 API ----

    @app.route("/api/rooms", methods=["GET"])
    def list_rooms():
        conn = sqlite3.connect(DB_PATH)
        rooms = conn.execute(
            "SELECT id, title, video_type, status, created_at FROM rooms ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return jsonify([{
            "id": r[0], "title": r[1], "video_type": r[2],
            "status": r[3], "created_at": r[4]
        } for r in rooms])

    @app.route("/api/rooms", methods=["POST"])
    def create_room():
        data = request.json or {}
        room_id = data.get("id", f"room_{int(time.time())}")
        title = data.get("title", "未命名")
        video_source = data.get("video_source", "")
        video_type = data.get("video_type", "url")  # url / upload / webdav
        subtitle_path = data.get("subtitle_path", "")

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO rooms (id, title, video_source, video_type, subtitle_path) VALUES (?,?,?,?,?)",
            (room_id, title, video_source, video_type, subtitle_path)
        )
        conn.commit()
        conn.close()
        return jsonify({"room_id": room_id, "status": "created"})

    @app.route("/api/rooms/<room_id>", methods=["GET"])
    def get_room(room_id):
        conn = sqlite3.connect(DB_PATH)
        room = conn.execute(
            "SELECT id, title, video_source, video_type, subtitle_path, status, current_time, is_playing FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone()
        conn.close()
        if not room:
            return jsonify({"error": "房间不存在"}), 404
        return jsonify({
            "id": room[0], "title": room[1], "video_source": room[2],
            "video_type": room[3], "has_subtitle": bool(room[4]),
            "status": room[5], "current_time": room[6], "is_playing": bool(room[7])
        })

    # ---- 视频上传 ----

    @app.route("/api/upload/video", methods=["POST"])
    def upload_video():
        if "file" not in request.files:
            return jsonify({"error": "没有文件"}), 400
        f = request.files["file"]
        # 安全处理文件名，防止路径遍历
        safe_name = re.sub(r'[^\w\-.]', '_', f.filename or "video")
        filename = f"{int(time.time())}_{safe_name}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        f.save(filepath)
        return jsonify({"path": f"/api/videos/{filename}", "filename": filename})

    @app.route("/api/upload/subtitle", methods=["POST"])
    def upload_subtitle():
        if "file" not in request.files:
            return jsonify({"error": "没有文件"}), 400
        f = request.files["file"]
        safe_name = re.sub(r'[^\w\-.]', '_', f.filename or "subtitle.srt")
        if not safe_name.endswith('.srt'):
            return jsonify({"error": "只支持SRT格式字幕"}), 400
        filename = f"{int(time.time())}_{safe_name}"
        filepath = os.path.join(SUBTITLE_DIR, filename)
        f.save(filepath)
        return jsonify({"path": filepath, "filename": filename})

    @app.route("/api/videos/<filename>")
    def serve_video(filename):
        return send_from_directory(UPLOAD_DIR, filename)

    # ---- 字幕查询（LLM用）----

    @app.route("/api/rooms/<room_id>/subtitles", methods=["GET"])
    def get_current_subtitles(room_id):
        """获取房间当前播放时间点前后的字幕，供LLM读取"""
        conn = sqlite3.connect(DB_PATH)
        room = conn.execute(
            "SELECT subtitle_path, current_time FROM rooms WHERE id=?", (room_id,)
        ).fetchone()
        conn.close()
        if not room or not room[0]:
            return jsonify({"subtitles": "", "message": "没有字幕文件"})

        subtitles = parse_srt(room[0])
        current = room[1] or 0
        # 返回当前时间点前后30秒的字幕
        text = get_subtitles_in_range(subtitles, max(0, current - 15), current + 15)
        return jsonify({"current_time": current, "subtitles": text})

    # ---- 观影日志 API（异步模式）----

    @app.route("/api/watchlog", methods=["GET"])
    def list_watchlog():
        conn = sqlite3.connect(DB_PATH)
        logs = conn.execute(
            "SELECT id, uid, title, category, rating, thoughts, watched_at FROM watch_log ORDER BY watched_at DESC"
        ).fetchall()
        conn.close()
        return jsonify([{
            "id": l[0], "uid": l[1], "title": l[2], "category": l[3],
            "rating": l[4], "thoughts": l[5], "watched_at": l[6]
        } for l in logs])

    @app.route("/api/watchlog", methods=["POST"])
    def add_watchlog():
        data = request.json or {}
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO watch_log (uid, title, category, rating, thoughts) VALUES (?,?,?,?,?)",
            (data.get("uid", ""), data.get("title", ""),
             data.get("category", "电影"), data.get("rating"),
             data.get("thoughts", ""))
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})

    @app.route("/api/watchlog/search", methods=["GET"])
    def search_watchlog():
        q = request.args.get("q", "")
        conn = sqlite3.connect(DB_PATH)
        logs = conn.execute(
            "SELECT id, uid, title, category, rating, thoughts, watched_at FROM watch_log WHERE title LIKE ? OR thoughts LIKE ? ORDER BY watched_at DESC",
            (f"%{q}%", f"%{q}%")
        ).fetchall()
        conn.close()
        return jsonify([{
            "id": l[0], "uid": l[1], "title": l[2], "category": l[3],
            "rating": l[4], "thoughts": l[5], "watched_at": l[6]
        } for l in logs])

    # ---- WebSocket 同步 ----

    @sock.route("/ws/<room_id>")
    def ws_sync(ws, room_id):
        """WebSocket同步播放状态"""
        if room_id not in ws_rooms:
            ws_rooms[room_id] = []
        ws_rooms[room_id].append(ws)

        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                data = json.loads(msg)

                # 更新房间状态到数据库
                conn = sqlite3.connect(DB_PATH)
                if data.get("type") in ("play", "pause", "seek"):
                    conn.execute(
                        "UPDATE rooms SET current_time=?, is_playing=? WHERE id=?",
                        (data.get("time", 0), 1 if data["type"] == "play" else 0, room_id)
                    )
                    conn.commit()
                conn.close()

                # 广播给房间内其他人
                for client in ws_rooms.get(room_id, []):
                    if client != ws:
                        try:
                            client.send(msg)
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            if room_id in ws_rooms and ws in ws_rooms[room_id]:
                ws_rooms[room_id].remove(ws)

    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)

# ============================================================
# AstrBot 插件注册
# ============================================================

@register("watch_together", "叶枔枖 & 叶克宝", "一起看·同步/异步观影插件", "1.0.0")
class WatchTogetherPlugin(Star):

    def __init__(self, context: Context):
        super().__init__(context)
        init_db()
        # 启动Web服务（后台线程）
        t = threading.Thread(target=start_web_server, daemon=True)
        t.start()
        self._subtitles_cache = {}  # room_id -> parsed subtitles

    # ---- 同步观影命令 ----

    @filter.command("一起看")
    async def create_watch_room(self, event: AstrMessageEvent):
        """创建观影房间：/一起看 电影名 [视频链接]"""
        text = event.message_str.replace("/一起看", "").replace("一起看", "").strip()
        parts = text.split(maxsplit=1)
        title = parts[0] if parts else "未命名"
        video_source = parts[1] if len(parts) > 1 else ""
        room_id = f"room_{int(time.time())}"

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO rooms (id, title, video_source, video_type) VALUES (?,?,?,?)",
            (room_id, title, video_source, "url" if video_source else "upload")
        )
        conn.commit()
        conn.close()

        # 替换为你的实际域名或IP
        base_url = os.environ.get("WATCH_TOGETHER_BASE_URL", f"http://localhost:{WEB_PORT}")
        url = f"{base_url}/?room={room_id}"

        yield event.plain_result(
            f"🎬 观影房间已创建！\n"
            f"电影：{title}\n"
            f"房间链接：{url}\n"
            f"两个人打开同一个链接就能同步观影～\n"
            f"{'视频链接已设置' if video_source else '进入房间后上传视频或粘贴链接'}"
        )

    @filter.command("正在看")
    async def whats_playing(self, event: AstrMessageEvent):
        """查看当前正在播放的房间"""
        conn = sqlite3.connect(DB_PATH)
        rooms = conn.execute(
            "SELECT id, title, current_time, is_playing FROM rooms WHERE status='waiting' ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        conn.close()

        if not rooms:
            yield event.plain_result("当前没有活跃的观影房间～")
            return

        msg = "🎬 当前房间：\n"
        for room_id, title, cur_time, playing in rooms:
            status = "▶️ 播放中" if playing else "⏸ 暂停"
            minutes = int(cur_time // 60)
            seconds = int(cur_time % 60)
            msg += f"  {title} [{status} {minutes:02d}:{seconds:02d}]\n"
        yield event.plain_result(msg)

    @filter.command("现在演到哪了")
    async def get_current_subtitle(self, event: AstrMessageEvent):
        """获取当前字幕内容（LLM可以用这个了解剧情进展）"""
        conn = sqlite3.connect(DB_PATH)
        room = conn.execute(
            "SELECT id, title, subtitle_path, current_time FROM rooms ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.close()

        if not room:
            yield event.plain_result("现在没有在看电影～")
            return

        room_id, title, sub_path, cur_time = room
        if not sub_path or not os.path.exists(sub_path):
            yield event.plain_result(f"正在看《{title}》，但没有字幕文件，我看不到台词～")
            return

        # 解析字幕
        if room_id not in self._subtitles_cache:
            self._subtitles_cache[room_id] = parse_srt(sub_path)

        subtitles = self._subtitles_cache[room_id]
        text = get_subtitles_in_range(subtitles, max(0, cur_time - 30), cur_time + 5)

        if text:
            minutes = int(cur_time // 60)
            seconds = int(cur_time % 60)
            yield event.plain_result(
                f"🎬《{title}》[{minutes:02d}:{seconds:02d}]\n"
                f"最近的台词：\n{text}"
            )
        else:
            yield event.plain_result(f"🎬《{title}》当前没有台词（可能是无对白片段）")

    # ---- 异步观影命令 ----

    @filter.command("看完了")
    async def mark_watched(self, event: AstrMessageEvent):
        """记录看完：/看完了 电影名 [感想]"""
        text = event.message_str.replace("/看完了", "").replace("看完了", "").strip()
        parts = text.split(maxsplit=1)
        title = parts[0] if parts else ""
        thoughts = parts[1] if len(parts) > 1 else ""
        uid = event.get_sender_id()

        if not title:
            yield event.plain_result("格式：/看完了 电影名 [感想]")
            return

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO watch_log (uid, title, thoughts) VALUES (?,?,?)",
            (uid, title, thoughts)
        )
        conn.commit()
        conn.close()

        msg = f"📝 已记录：看完了《{title}》"
        if thoughts:
            msg += f"\n感想：{thoughts}"
        msg += "\n随时可以聊聊这部电影～"
        yield event.plain_result(msg)

    @filter.command("打分")
    async def rate_movie(self, event: AstrMessageEvent):
        """给电影打分：/打分 电影名 8"""
        text = event.message_str.replace("/打分", "").replace("打分", "").strip()
        match = re.match(r'(.+?)\s+(\d+)', text)
        if not match:
            yield event.plain_result("格式：/打分 电影名 分数（1-10）")
            return

        title = match.group(1).strip()
        rating = min(10, max(1, int(match.group(2))))
        uid = event.get_sender_id()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # 更新最近一条同名记录的评分
        cursor.execute(
            "UPDATE watch_log SET rating=? WHERE uid=? AND title=? AND id=(SELECT MAX(id) FROM watch_log WHERE uid=? AND title=?)",
            (rating, uid, title, uid, title)
        )
        if cursor.rowcount == 0:
            # 没有记录则新建
            cursor.execute(
                "INSERT INTO watch_log (uid, title, rating) VALUES (?,?,?)",
                (uid, title, rating)
            )
        conn.commit()
        conn.close()

        stars = "⭐" * rating
        yield event.plain_result(f"《{title}》评分：{stars} {rating}/10")

    @filter.command("片单")
    async def show_watchlog(self, event: AstrMessageEvent):
        """查看观影记录"""
        conn = sqlite3.connect(DB_PATH)
        logs = conn.execute(
            "SELECT title, rating, thoughts, watched_at FROM watch_log ORDER BY watched_at DESC LIMIT 10"
        ).fetchall()
        conn.close()

        if not logs:
            yield event.plain_result("片单空空的，快去看电影吧～")
            return

        msg = "🎬 片单：\n"
        for title, rating, thoughts, watched_at in logs:
            date_str = watched_at[:10] if watched_at else ""
            rating_str = f" {'⭐' * rating}" if rating else ""
            msg += f"  《{title}》{rating_str} {date_str}\n"
            if thoughts:
                msg += f"    💭 {thoughts[:50]}{'...' if len(thoughts or '') > 50 else ''}\n"
        yield event.plain_result(msg)

    @filter.command("搜片")
    async def search_movie(self, event: AstrMessageEvent):
        """搜索观影记录：/搜片 关键词"""
        q = event.message_str.replace("/搜片", "").replace("搜片", "").strip()
        if not q:
            yield event.plain_result("格式：/搜片 关键词")
            return

        conn = sqlite3.connect(DB_PATH)
        logs = conn.execute(
            "SELECT title, rating, thoughts, watched_at FROM watch_log WHERE title LIKE ? OR thoughts LIKE ? ORDER BY watched_at DESC",
            (f"%{q}%", f"%{q}%")
        ).fetchall()
        conn.close()

        if not logs:
            yield event.plain_result(f"没找到跟「{q}」相关的观影记录～")
            return

        msg = f"🔍 搜索「{q}」：\n"
        for title, rating, thoughts, watched_at in logs:
            rating_str = f" {'⭐' * rating}" if rating else ""
            msg += f"  《{title}》{rating_str}\n"
            if thoughts:
                msg += f"    💭 {thoughts[:80]}\n"
        yield event.plain_result(msg)
