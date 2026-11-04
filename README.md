# another-kvm

基于 CH9329 / CH340 和 HD33 HDMI 采集卡的 macOS 局域网 Web KVM。

本项目原创代码使用 [BSD 3-Clause License](LICENSE)。原厂 SDK 和第三方文章不随公开仓库分发；本地已有文件仍保留。

## CH9329 + CH340 双头 USB 键鼠线测试

本项目已在 macOS 上完成串口查询、键鼠 HID 闭环和 `hello world` 实际输入验证。
两端 USB 均插入同一台电脑，主控端通过 Python 发送串口命令，被控端以 USB 键鼠形式产生输入。

## Web KVM

HD33 视频采集和 CH340 串口迁移至 `local@192.168.11.99`。网页服务、使用方法及常驻部署见 [Web KVM 文档](web_kvm/README.md)。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| [ch9329.py](ch9329.py) | 协议编解码、串口设备查询、波特率探测、键鼠测试入口 |
| [mac_hid.py](mac_hid.py) | macOS USB 枚举及指定设备的 HID 输入监听 |
| [type_hello_world.py](type_hello_world.py) | 倒计时后在当前输入位置键入 `hello world` |
| [test_ch9329.py](test_ch9329.py) | 无需硬件的协议单元测试 |
| [requirements.txt](requirements.txt) | Python 依赖，目前仅需 pyserial |
| `device_inventory.json`（本地生成） | 本机串口和 USB 设备枚举记录，不纳入公开仓库 |
| [hardware_test_result.json](hardware_test_result.json) | 实机信息和配置查询结果 |
| [loopback.json](loopback.json) | 四阶段 HID 闭环测试的实际事件与结果 |

## 产品分析

链路是：主控电脑 USB → CH340 USB/UART → CH9329 UART/HID → 被控电脑 USB。
两端插同一台电脑时，电脑同时承担主控端和被控端，可做本机闭环测试。

CH340 负责提供串口；CH9329 在被控端枚举为硬件 USB HID 键盘、鼠标。
这不是操作系统里的软件虚拟 HID 驱动，也不是两台电脑之间的普通 USB 数据线。
它适合给 KVM 项目提供键鼠注入通道，但本身不含视频采集、屏幕回传或 USB 主机功能。
它不能读取另一把键盘的按键，也不会自动告诉主控端当前窗口或鼠标实际屏幕坐标。
普通键盘模式可同时报告 8 个修饰键和最多 6 个普通键；还支持相对/绝对鼠标。
自定义 HID 是另一种工作模式，不能把当前键鼠模式当作任意数据收发通道。

本机于 2026-11-04 识别到：

| 端口 | VID:PID | 本机标识 |
| --- | --- | --- |
| CH340 串口端 | `1A86:7523` | `/dev/cu.usbserial-1120`，USB location `0x01120000` |
| CH9329 HID 端 | `1A86:E129` | `WCH UART TO KB-MS_V1.8`，location `0x01130000` |

USB 枚举、串口查询和 HID 闭环均已验证。产品字符串中的 V1.8 不作为芯片版本解析依据；GET_INFO 实际返回的版本原始值为 `0x38`。

## 协议要点

本目录芯片手册和串口通信协议 V1.0 描述默认配置为 **9600、8N1、无流控、地址 0、协议模式**。
实际配置需通过查询确认。ASCII 模式只适合字符输入，完整键鼠控制应使用协议模式。

```
57 AB ADDR CMD LEN DATA... SUM
SUM = 前面所有字节累加后 & 0xff
```

正常应答命令是 `CMD | 0x80`，异常应答是 `CMD | 0xC0`。
发送动作后须检查应答状态，不能只以串口写入成功作为测试通过。
协议等待应答上限为 500ms。脚本默认采用该超时，不自动重发动作，避免 ACK 丢失后重复移动或输入。

| 命令 | 功能 | 测试用途 |
| --- | --- | --- |
| `01` | 版本、USB 枚举、键盘灯状态 | `info` / `probe` |
| `08` | 50 字节参数配置 | `info` |
| `02` | 8 字节普通键盘状态 | Shift 按下、释放 |
| `05` | `01 buttons dx dy wheel` | 相对鼠标左右移动 |
| `04` | `02 buttons x_lo x_hi y_lo y_hi wheel` | 库函数支持绝对坐标 |

配置中的波特率按大端解析，绝对鼠标的 X/Y 按小端编码；鼠标相对移动按有符号字节编码。
绝对鼠标默认逻辑范围为 4096×4096，脚本限制坐标为 0..4095。
屏幕像素需要映射到此范围，多显示器、缩放和系统鼠标加速需要实际校准。
相对移动 +8/-8 的 HID 数值抵消，不保证系统光标因边界或加速而精确回到原点。

9600bps 下 14 字节键盘帧仅串口发送就约需 14.6ms（每字节 10bit），还需应答和 USB 处理时间。
高频控制应在基线验证后另行评估波特率和实际延迟，不能把 USB 12Mbps 当作键鼠指令吞吐率。

资料存在以下歧义，代码保留边界：

- GET_INFO 手册示例将 `0x30` 解释为 V1.0，不能直接按十六进制半字节解析版本，所以输出 `version_raw`。
- 多媒体键命令 `03` 的表格标注 2 字节，示例却为 4 字节，本轮不执行该命令。
- 参数表没有明确 VID/PID 各字节顺序，查询输出 `vid_pid_raw`；设备身份使用本机 USB 枚举数据。

## 运行

```bash
git clone https://github.com/networkextension/another-kvm.git
cd another-kvm
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python ch9329.py list
.venv/bin/python ch9329.py info --verbose --output info.json
```

若没有应答，可显式执行常见波特率探测，仅发送 GET_INFO，不更改设备配置：

```bash
.venv/bin/python ch9329.py probe --verbose --output probe.json
```

若设备原本处于 ASCII 模式，协议探测字节可能被当作字符输入。因此探测时保持前台为可丢弃的文本输入区。
探测不会更改芯片波特率。若地址不是 0，需要传 `--address 0xNN`。
有多个串口时可使用 `--port /dev/cu.usbserial-1120`，串口路径可能随插拔改变。

确认处于复合键鼠模式和协议模式后，可执行动作测试：

```bash
.venv/bin/python ch9329.py smoke --verbose --output smoke.json
.venv/bin/python ch9329.py loopback --verbose --output loopback.json
```

`smoke` 短按左 Shift、鼠标右移和左移各 8 单位，检查串口 ACK。
`loopback` 除 ACK 外，使用 macOS IOHIDManager 只监听选定设备的 HID 值，分别要求观察到：

| 步骤 | Usage Page | Usage | 值 |
| --- | --- | --- | --- |
| 左 Shift 按下 | 7 | `0xE1` | 1 |
| 左 Shift 释放 | 7 | `0xE1` | 0 |
| 鼠标向右 | 1 | `0x30` | 8 |
| 鼠标向左 | 1 | `0x30` | -8 |

测试期间暂时不要操作实体键鼠，避免已有修饰键和本次测试叠加。
程序正常结束或捕获到异常时会尽力释放键鼠；拔线或进程强制终止无法保证执行清理。
`ch9329.py` 的上述测试不发送文本、回车、点击、滚轮或配置写入，不修改 VID/PID，不复位设备。
完整复合模式以外的设备会在动作前拒绝测试。
若 USB 标识已更改，可使用 `--hid-vid`、`--hid-pid`，多条同型号线用 `--hid-location` 区分。

macOS HID 监听可能要求启动终端具备“系统设置 → 隐私与安全性 → 输入监控”权限。
IOHID 监听失败或未收到匹配事件时，不能声称闭环已通过。
串口 ACK 也不证明某个应用已经消费输入。当前闭环检查覆盖相对鼠标和修饰键，不覆盖绝对坐标、多媒体、BIOS 或其他操作系统兼容性。

## 在当前光标位置输入 hello world

先确认 `info` 查询通过，然后在终端执行：

```bash
.venv/bin/python type_hello_world.py
```

程序确认 USB 已枚举、设备处于复合键鼠和串口协议模式后，输出 5 秒倒计时提示。
在这 5 秒内，点击目标应用中的文本框，将输入光标放到期望位置，并切换到英文输入源。
倒计时结束后，设备键入 `hello world`，不发送回车。
它使用 US 键盘布局的 HID 键码；实际字符受被控端的输入源和键盘布局影响。
它不会自动切换应用或移动文本插入光标，文字进入发送时拥有键盘焦点的输入控件。

需要更多时间切换输入位置，或需要指定串口时：

```bash
.venv/bin/python type_hello_world.py --delay 10 --port /dev/cu.usbserial-1120
```

该脚本按本次实测配置使用 9600bps、地址 0。
每个字符发送一次按下和一次释放，按下后及释放后各等待 40ms；重复的两个 `l` 也分别释放。
脚本会读取 Caps Lock 状态，必要时通过 Shift 组合输出小写字母，不切换 Caps Lock。
发送过程异常时尽力释放按键；结束后会显示：

```text
Sent hello world; all serial ACKs passed. No Enter sent.
```

该提示表示串口动作应答正常，目标应用的文字仍需实际检查。
本次执行后，用户在当前会话中提交了 `hello world`，完成目标输入结果确认。

## 已执行验证及限制

```bash
python3 -m unittest -v test_ch9329.py
```

离线测试覆盖手册已知帧、拆包粘包、噪声、校验和、无关地址/命令过滤、主动 HID 包、错误 ACK、动作超时不重发、异常释放和参数边界。
当前 10 项离线测试通过；本机设备枚举通过，结果见 `device_inventory.json`。

2026-11-04 21:00（Asia/Shanghai），用户修改会话权限后实机测试通过：

| 检查 | 实际结果 |
| --- | --- |
| GET_INFO / GET_CONFIG | 应答帧和校验通过，USB 已枚举 |
| 当前参数 | 9600bps、地址 0、复合键鼠模式 0、串口协议模式 0、包间隔 3ms |
| 芯片版本原始字节 | `0x38` |
| VID/PID 参数原始字节 | `86 1a 29 e1`，与 USB 枚举的 `1A86:E129` 一致，说明本设备这两个字段为小端 |
| 左 Shift 按下/释放 | HID Page 7、Usage `0xE1`，实际收到值 1、0 |
| 相对鼠标右移/左移 | HID Page 1、Usage `0x30`，实际收到值 +8、-8 |
| 动作应答及最终释放 | 所有 ACK 为成功，清理释放成功 |
| 普通字符输入 | 执行 `python3 type_hello_world.py` 后输入 `hello world`；所有串口 ACK 正常，用户确认实际文本 |

串口查询结果保存在 `hardware_test_result.json`，四阶段闭环事件保存在 `loopback.json`。
闭环及实际文本输入证明本机 CH340 → UART → CH9329 → USB HID → 目标输入框的链路可用。
普通字符的实测范围为 `hello world`；其他字符、组合快捷键、绝对鼠标、多媒体和其他系统兼容性未测试。

## 故障排查

| 现象 | 检查方法 |
| --- | --- |
| 找不到 CH340 或检测到多个 | 执行 `ch9329.py list`，核对串口端插头；用 `--port` 指定目标串口 |
| 串口 `Operation not permitted` | 检查运行环境是否允许打开 `/dev/cu.*`；本次初始问题来自会话沙箱，修改会话权限后解决 |
| 串口占用或无法独占打开 | 关闭串口调试器等正在使用该设备的进程，一次只运行一个测试 |
| GET_INFO 超时 | 核对波特率、地址、供电和两端连接；默认值为 9600、地址 0；必要时执行 `probe` |
| USB 未枚举 | 核对 CH9329 HID 端连接，用 `list` 确认 HID 产品是否出现 |
| ACK 正常但 loopback 失败 | 检查 `loopback.json` 的逐阶段事件、HID 设备匹配和终端输入监控权限 |
| ACK 正常但没有文字或文字不对 | 检查目标输入框是否拥有焦点、是否可编辑、英文输入源及键盘布局；可增大 `--delay` |

权限修改解决的是本次运行环境的串口访问限制，不需要改写芯片配置。
运行命令会产生真实键鼠输入；重新执行文字脚本会再次输入同一段文字。

## 参考资料

原厂资料可从 [WCH CH9329 产品页](https://www.wch.cn/products/ch9329.html) 和 [CH9329 手册下载页](https://www.wch.cn/downloads/CH9329DS1_PDF.html) 获取。
下列文件为开发时使用的本地资料，不随公开仓库分发：

- `CH9329EVT-2022/CH9329DS1.PDF`：芯片手册 V1.0，工作模式、接口、默认参数。
- `CH9329EVT-2022/CH9329¥Æø⁄Õ®–≈–≠“È.pdf`：串口通信协议 V1.0，帧格式和命令细节（原文件名乱码）。
- `CH9329EVT-2022/EVT/PUB/` 下另一份协议用于交叉核对。
- `CH9329双头线使用说明-CSDN博客.pdf`：成品线接线及操作说明。
