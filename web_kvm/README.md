# HD33 + CH9329 Web KVM

服务部署在采集 Mac `local@192.168.11.99` 的 `~/kvm-usb`，局域网入口为：

**http://192.168.11.99:8080/**

HD33 USB 视频采集和 CH340 串口均连接这台 Mac；HD33 的 HDMI 输入和 CH9329 的 HID 端连接被控 Mac。
浏览器看到的是 HD33 的输入画面，键鼠经真实 USB HID 送到被控 Mac。

## 功能

- HD33 实时视频，目标 30fps；本次实际收到 1920×1080、约 30fps，多浏览器观看共享一路采集。
- 浏览器普通键盘、修饰键、方向键、F1–F12、鼠标左右中键及滚轮。
- 默认绝对鼠标定位；可切换相对模式处理多屏或坐标偏差。
- 英文、数字和 US 布局标点文字发送；不自动追加回车，文字中已有的换行会发送 Enter。
- 全屏、保存当前画面、设备连接状态和控制连接往返时间。
- 同一时刻只有一个浏览器接管键鼠；断线、窗口失焦、页面隐藏或控制心跳丢失会释放键鼠。
- 访问码登录、同源 WebSocket 校验，登录 cookie 有效期 12 小时。

视频使用 JPEG over WebSocket，浏览器在 canvas 中显示。每个观看者只取最新帧，慢网络不会无限积压视频队列。
这是优先保证局域网可用的版本，暂不提供音频、WebRTC、远程电源操作或文件传输。

## 使用

1. 打开上述地址，输入访问码。
2. 确认“视频”和“键鼠”在线，点击“接管键鼠”。
3. 点击视频画面聚焦，然后输入或移动鼠标；Tab、方向键等也发送至远端。
4. 文字发送前先让被控 Mac 的目标输入框聚焦，并切换至 US/英文输入源。
5. 点“释放控制”或离开浏览器窗口结束接管。

访问码首次启动自动生成，仅存于采集 Mac 的 `~/kvm-usb/web_kvm/.access-code`（权限 600）。读取方式：

```bash
ssh local@192.168.11.99 'cat ~/kvm-usb/web_kvm/.access-code'
```

也可以通过进程环境变量 `KVM_PASSWORD` 指定至少 8 字符的访问码。
本服务按局域网部署，HTTP 不加密；需要跨不可信网络访问时，使用 SSH 隧道或受保护的 HTTPS 反向代理。

## 安装与前台启动

要求 macOS、Python 3.10+ 和 Xcode Command Line Tools（构建原生采集程序）。
在仓库根目录运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r web_kvm/requirements.txt
sh web_kvm/native/build.sh
.venv/bin/python -m web_kvm.server --port 8080
```

默认自动选择唯一的 CH340 `1A86:7523`，使用 9600bps、地址 0。
本次远端串口是 `/dev/cu.usbserial-110`，可显式传 `--serial /dev/cu.usbserial-110`；自动选择更能适应插拔后设备路径变化。
脚本查询 CH9329 并确认处于复合键鼠模式、串口协议模式，不写芯片配置。

首次摄像头授权如无法通过后台进程弹窗，可从采集 Mac 的图形会话打开授权程序：

```bash
open web_kvm/Camera.app --args --authorize
```

允许“Remote Desk Capture”访问摄像头。应用只请求视频权限，不请求麦克风或屏幕录制。
原生采集源码在 `native/Capture.swift`，不录制文件，JPEG 帧通过管道传给服务。

可选 FFmpeg 后端：

```bash
.venv/bin/python -m web_kvm.server --capture-backend ffmpeg --capture-fps 60 --fps 30
```

本机 HD33 的 720p 原生档位含 60fps 和 10fps；FFmpeg 的 AVFoundation 格式选择要求匹配档位最大帧率。
直接用 720p/30 输入会被拒绝，因此 FFmpeg 后端采用 60 输入、30 输出。
参考：[FFmpeg AVFoundation 文档](https://www.ffmpeg.org/ffmpeg-devices.html#avfoundation)、[帧率选择实现](https://github.com/FFmpeg/FFmpeg/blob/master/libavdevice/avfoundation.m)。
HTTP/WebSocket 由 [aiohttp](https://docs.aiohttp.org/en/stable/web_reference.html#aiohttp.web.WebSocketResponse) 提供。

## 常驻服务

先停止前台服务，再执行：

```bash
.venv/bin/python web_kvm/install_service.py
```

安装用户级 LaunchAgent `local.web-kvm`，登录后自动启动，进程退出后自动恢复，不需要 sudo。
采集 Mac 必须有登录的图形会话及摄像头权限；用户注销后该 LaunchAgent 不运行。

```bash
# 查看状态
launchctl print gui/$(id -u)/local.web-kvm
# 重启
launchctl kickstart -k gui/$(id -u)/local.web-kvm
# 停止
launchctl bootout gui/$(id -u)/local.web-kvm
# 日志
tail -f ~/kvm-usb/web_kvm/service.log
```

## 测试与结果

2026-11-04 已完成部署与实机验证：

| 检查 | 结果 |
| --- | --- |
| 局域网访问、登录及静态资源 | 通过 |
| 串口设备 | `/dev/cu.usbserial-110`，9600bps，CH9329 USB 已枚举 |
| HD33 画面 | 实际 JPEG 为 1920×1080，约 30fps，可见被控 Mac 桌面 |
| 视频 WebSocket | 连续收到 15 个有效 JPEG 帧 |
| 控制 WebSocket | 左 Shift 按下/释放、鼠标相对 +2/-2，4 个实际串口 ACK 通过 |
| 控制权清理 | 测试结束后无控制者，释放正常 |
| 常驻服务 | 用户 LaunchAgent `local.web-kvm` 运行中 |
| 离线检查 | 17 项 Python 测试通过，JavaScript 语法和字符映射检查通过 |

完整网络实测记录见 [verification.json](verification.json)，串口单独测试记录见 [control_verification.json](control_verification.json)。
原生采集请求 1280×720，但 macOS 实际输出 1920×1080；服务从 JPEG 头读取实际分辨率，页面按实际画面定位。
授权后的首次启动曾触发这台开发版 macOS 的 Metal 驱动断言，采集子进程自动重启后恢复；已验证后续重启和采集。
服务对采集退出和无帧设有重启机制。若该驱动问题频繁出现，可在采集源码中启用 Core Image 软件渲染后重建并重新授权。


离线协议及服务测试：

```bash
.venv/bin/python -m unittest -v test_ch9329 web_kvm.test_server
node --check web_kvm/static/app.js
```

测试覆盖输入参数边界、访问码、会话签名、跨站请求拒绝、单控制者排他、未接管输入拒绝及断线释放。

服务运行时进行真实视频和控制检查：

```bash
.venv/bin/python -m web_kvm.smoke \
  --url http://127.0.0.1:8080 \
  --snapshot /tmp/hd33-frame.jpg \
  --exercise-input \
  --output web_kvm/verification.json
```

`--exercise-input` 会短按左 Shift，并相对移动鼠标 +2/-2，不输入文字或点击。
视频检查要求连续收到 15 个 JPEG 帧，控制检查要求实际串口 ACK，结束后验证控制权释放。

## 限制与排查

- 视频一直等待：检查 HD33 HDMI 输入、被控 Mac 是否输出画面、采集 Mac 的摄像头权限，以及是否被其他采集软件占用。
- 键鼠未连接：检查两端线缆、CH340 串口和服务日志；服务会在无控制者时重连串口。
- 绝对鼠标偏移：被控端多显示器、系统缩放或鼠标映射可能造成偏差，可切换相对鼠标模式。
- 浏览器保留的 Cmd+L、Cmd+W 等快捷键可能由本机处理；本机操作系统快捷键也不保证能发送至远端。
- 浏览器键盘事件基于物理按键编码。字符发送使用 US 布局，不支持直接发送中文，也不会自动切换远端输入法或 Caps Lock。
- 本版为鼠标指针设备设计；手机可观看，但没有完整触摸键鼠面板。
- 界面延迟显示是控制连接 ping 往返时间，不是 HDMI 到浏览器的端到端视频延迟。
- 当前浏览器自动化工具不可用，未完成浏览器中的人工式端到端交互验收；网络流、原生采集和实际串口控制分别进行验证。
