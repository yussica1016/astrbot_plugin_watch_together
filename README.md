# 🎬 一起看 Watch Together

AstrBot 同步/异步观影插件 · 和你的AI伴侣一起看电影。

**设计：叶枔枖　编写：叶克宝**

---

## 功能

### 模式一：同步观影 🍿

通过 WebSocket 实时同步播放状态，两个人打开同一个网页链接即可同步观影。

- 一方暂停/播放/拖动进度，另一方自动跟随
- 上传 SRT 字幕文件后，LLM 可以通过 `/现在演到哪了` 读取当前台词
- AI 伴侣通过字幕"跟着看"，可以实时讨论剧情

**视频源支持三种：**

| 方式 | 说明 |
|------|------|
| 在线链接 | 粘贴 mp4/webm/m3u8 直链 |
| 本地上传 | 上传视频文件到服务器 |
| WebDAV | 挂载网盘（阿里云盘等），需配置 WebDAV 地址 |

### 模式二：异步观影 📝

看完电影后记录感想，AI 伴侣可以搜索你的观影日志并和你讨论。

- 记录电影名、感想、评分
- AI 通过 `/搜片` 命令查询你看过什么
- 观影日志也可在 Web 页面查看

---

## QQ 命令

| 命令 | 功能 |
|------|------|
| `/一起看 电影名 [链接]` | 创建观影房间，生成 Web 链接 |
| `/正在看` | 查看当前活跃的观影房间 |
| `/现在演到哪了` | 获取当前播放时间点的字幕（需有 SRT） |
| `/看完了 电影名 [感想]` | 记录观影日志 |
| `/打分 电影名 分数` | 给电影打分（1-10） |
| `/片单` | 查看最近观影记录 |
| `/搜片 关键词` | 搜索观影记录 |

---

## 安装

### 1. 安装插件

将本仓库放入 AstrBot 的插件目录：

```bash
cd /path/to/AstrBot/data/plugins/
git clone https://github.com/yussica1016/astrbot_plugin_watch_together.git
```

### 2. 安装依赖

```bash
pip install flask flask-sock --break-system-packages
```

### 3. 配置（可选）

通过环境变量配置：

```bash
# Web 服务端口（默认 8902）
export WATCH_TOGETHER_PORT=8902

# 房间主人 QQ 号
export WATCH_TOGETHER_OWNER_UID="你的QQ号"

# Web 页面的外部访问地址（用于生成房间链接）
export WATCH_TOGETHER_BASE_URL="http://你的域名:8902"

# WebDAV（可选）
export WATCH_TOGETHER_WEBDAV_URL="https://你的WebDAV地址"
export WATCH_TOGETHER_WEBDAV_USER="用户名"
export WATCH_TOGETHER_WEBDAV_PASS="密码"
```

### 4. 放行端口

在服务器防火墙/安全组中放行 8902 端口（TCP）。

如果用 1Panel：**安全 → 防火墙 → 添加规则 → 端口 8902 / TCP / 允许**

### 5. 重启 AstrBot

重启后插件自动加载，Web 服务自动启动。

---

## 使用流程

### 同步观影

1. QQ 里发 `/一起看 星际穿越`
2. 插件返回一个网页链接
3. 你和 AI 伴侣（或另一个人）打开同一个链接
4. 在网页上加载视频（粘贴链接/上传文件/WebDAV）
5. 上传 SRT 字幕文件（可选，让 AI 能读到台词）
6. 开始看！播放/暂停/拖进度会自动同步
7. 边看边在 QQ 聊剧情，AI 可以发 `/现在演到哪了` 查看当前台词

### 异步观影

1. 自己看完电影
2. QQ 里发 `/看完了 星际穿越 时间感知那段太震撼了`
3. 之后随时和 AI 讨论这部电影
4. AI 可以发 `/搜片 星际穿越` 查看你的记录和感想

---

## 技术栈

- **后端**：AstrBot 插件（Python）+ Flask + Flask-Sock（WebSocket）
- **前端**：原生 HTML/CSS/JS 单文件
- **数据库**：SQLite
- **同步协议**：WebSocket 广播播放状态（play/pause/seek），零 LLM token 消耗

---

## 目录结构

```
astrbot_plugin_watch_together/
├── main.py          # 插件主逻辑 + API + WebSocket
├── metadata.yaml    # AstrBot 插件元数据
├── README.md
├── LICENSE          # GPL v3
├── .gitignore
├── web/
│   └── index.html   # 前端播放器页面
└── data/            # 运行时自动生成
    ├── watch.db     # SQLite 数据库
    ├── videos/      # 上传的视频文件
    └── subtitles/   # 上传的字幕文件
```

---

## 注意事项

- WebSocket 同步不经过 LLM，零 token 消耗
- 视频文件较大时建议使用在线链接或 WebDAV，减少服务器存储压力
- 字幕必须是 SRT 格式，UTF-8 编码
- AI 陪看的核心是字幕——没有字幕 AI 只知道你在看但不知道演到哪
- 在线链接需要是视频直链（能直接播放的 URL），不支持 B 站/优酷等平台页面链接

---

## License

GPL v3 © 2026 叶枔枖
