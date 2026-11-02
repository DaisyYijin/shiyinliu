# 拾音流

自动把**抖音博主的新视频**转成音频，上传到你的**喜马拉雅账号专辑**，在喜马拉雅 App 里随时听。

```
定时轮询博主主页 → 发现新视频 → 下载无水印视频 → ffmpeg 提取音频 → Playwright 上传喜马拉雅专辑 → SQLite 记录状态
```

## 功能

- 📡 **账号监控**：可配置多个抖音博主，定时（默认 30 分钟）增量拉取新作品，自动去重
- 🔗 **手动提交**：状态页粘贴视频链接 / 分享口令 / 主页链接，立即处理
- 🎵 **音频提取**：ffmpeg 转码 mp3 / m4a / aac，码率可配，自动跳过无音轨与图集内容
- 📤 **自动上传**：Playwright 驱动喜马拉雅创作中心，上传到你指定的专辑（可设公开/私密）
- 🔐 **扫码登录**：浏览器打开状态页即可扫码，登录态持久化，无需手动抓 Cookie
- 🔄 **失败重试**：下载/转码/上传失败自动重试（默认 3 次），断点续推（已下载不重下）
- 🖥️ **状态页**：处理记录、错误信息、统计一屏可见
- 🐳 **Docker 一键部署**：GitHub Actions 自动构建镜像到 GHCR，直接 `docker pull`

## 快速开始

### 1. 准备配置

```bash
git clone https://github.com/DaisyYijin/shiyinliu.git && cd shiyinliu
mkdir -p config data media
cp config/app.example.yaml config/app.yaml
```

编辑 `config/app.yaml`，至少填写两处：

| 配置 | 怎么拿 |
|---|---|
| `douyin.accounts[].url` | 抖音 App 打开博主主页 → 分享 → 复制链接，形如 `https://www.douyin.com/user/MS4wLjABAAAA...` |
| `ximalaya.album_id` | 浏览器登录 [喜马拉雅创作中心](https://www.ximalaya.com/reform-upload/page/webCenter/upload) → 专辑管理，地址栏 `sound/manage/` 后面那串数字 |

> 💡 也可以**完全不写配置文件里的博主**：启动后先在状态页完成「抖音扫码登录」，然后直接**搜索博主昵称一键添加**，新添加的博主会自动全量拉取所有作品并逐条转音频上传，之后转为增量监控。

### 2. 启动

```bash
docker compose up -d
```

（`docker-compose.yml` 已预填 GitHub Actions 自动构建的镜像 `ghcr.io/daisyyijin/shiyinliu:latest`，直接 `docker compose pull && docker compose up -d` 即可；如需本地构建，把 `build: .` 取消注释并改回本地 tag。）

### 3. 扫码登录喜马拉雅（只需一次）

浏览器打开 `http://<主机>:8300`，点击右侧 **「扫码登录」**，用喜马拉雅 App 扫页面出现的二维码。登录态保存在 `data/ximalaya_profile/`，重启不丢失；若失效，状态页会提示重新扫码。

之后就是全自动：每隔 `poll_interval_minutes` 分钟轮询一次，新视频自动转音频上传。

## 配置说明

全部配置在 `config/app.yaml`（挂载到容器 `/app/config/app.yaml`），见 `config/app.example.yaml` 内注释。

关键项：

```yaml
poll_interval_minutes: 30   # 轮询间隔
douyin:
  accounts:                 # 监控列表，可多条
    - name: 某博主
      url: https://www.douyin.com/user/MS4wLjABAAAA...
  cookie: ""                # 可选，游客模式通常够用
audio:
  format: mp3               # mp3 / m4a / aac
  bitrate: 192k
ximalaya:
  album_id: "12345678"      # 上传目标专辑
  visibility: public        # public / private
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/records` | 处理记录与统计 |
| GET | `/api/config` | 当前配置（脱敏） |
| POST | `/api/poll` | 立即触发一次轮询 |
| POST | `/api/submit` | 手动提交链接 `{"url": "..."}` |
| POST | `/api/login/start` | 发起喜马拉雅扫码登录 |
| GET | `/api/login/status` | 登录任务状态 |
| POST | `/api/douyin/login/start` | 发起抖音扫码登录 |
| GET | `/api/douyin/login/status` | 抖音登录状态 |
| GET | `/api/accounts` | 监控账号列表 |
| GET | `/api/accounts/search?q=` | 搜索抖音博主（需已登录抖音） |
| POST | `/api/accounts` | 添加监控 `{"sec_uid": "...", "name": "..."}` |
| DELETE | `/api/accounts/{sec_uid}` | 移除搜索添加的博主 |

## 数据目录

```
config/   app.yaml 配置
data/     records.db 状态库 / ximalaya_profile 登录态 / tmp 临时视频 / debug 排查截图 / login_qr.png
media/    产出音频归档
```

## 排错

- **上传失败**：看 `data/debug/` 里最新截图（每一步失败都会整页截图），多为喜马拉雅页面改版导致选择器失效，按截图调整 `app/ximalaya/uploader.py` 中对应步骤的候选选择器
- **提示未登录**：登录态过期，状态页重新扫码
- **拉取作品失败**：游客接口被风控时，在 `config/app.yaml` 填入浏览器抖音 Cookie；f2 库随抖音接口变动需要升级（`pip install -U f2` 后重建镜像）
- **查看日志**：`docker logs -f shiyinliu`

## 开发

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest
```

推送 `main` 分支会触发 GitHub Actions：先跑测试，通过后构建镜像推送到 `ghcr.io/daisyyijin/shiyinliu`（`latest` 与 commit sha 双标签）。

## 声明

本项目仅供个人学习与自用（把自己关注的内容转成音频便于收听）。请遵守抖音、喜马拉雅的用户协议与相关法律，尊重内容创作者版权，勿用于批量搬运或商业用途。
