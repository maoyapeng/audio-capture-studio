# AudioCapture Studio 0.1.1

Windows 多设备音频采集桌面初版，使用 Python + PySide6 开发。支持本地音频录制、远程命令终端、独立文件保存与采集记录。

0.1.1 修复设备列表：默认只显示确认的 USB 音频输入，优先使用 WASAPI，内置声卡和蓝牙可切换到全部本地音频查看。

## Windows 启动

1. 安装 **64 位 Python 3.11 或更新版本**，建议 Python 3.12，保留 Python Launcher（`py`）。
2. 解压项目，双击 `setup.bat`，联网安装依赖。
3. 双击 `demo.bat` 体验界面，或双击 `start.bat` 使用真实设备。

演示模式使用虚拟设备生成真实的多通道正弦测试文件，**不访问麦克风或远程设备**。任务清单中标记 `demo: true`，不要与真实录音混用。

命令行启动：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py --demo
# 真实采集
.venv\Scripts\python.exe main.py
```

## 已实现

- 设备列表、勾选参与采集、别名与每设备采样参数配置。
- USB / 本地输入设备发现；默认优先选 WASAPI，其他驱动接口仍可选择。
- USB 使用驱动报告的最大输入通道数。启动时验证格式；如果所选格式不能开启全部通道则报错，不悄悄减少通道。
- 多设备独立采集、后台缓冲与磁盘写入、实时通道峰值电平。
- 闲置时每 10 秒重新发现 USB / ADB；录制中 USB 输入中断会结束该设备的采集，保留已收到的音频，其余设备继续。
- SSH / Telnet / ADB 持久终端、命令发送、Ctrl+C、每设备输出、日志搜索 / 导出。
- SSH SFTP 下载、ADB pull 下载、Telnet 可选 HTTP 下载，结束后校验音频格式。
- 独立设备目录、录制开始 / 结束时间、异常和音频参数清单。
- 密码只保留在内存；本地配置自动保存设备模板。
- USB 音频保存为 **RF64（WAV 家族）**，避免普通 RIFF WAV 的 4 GiB 大小限制；使用支持 RF64 的音频工具读取。

## USB 使用

1. 插入设备，点击“发现 USB / ADB”。系统麦克风权限应允许桌面应用访问麦克风。
2. 列表显示系统名称、驱动接口、最大通道和连接状态。
3. 双击设备设置别名、采样率、位深；最大通道不可手动缩减。
4. 勾选需要的设备，选择保存目录，点击“开始录制已勾选设备”。

默认仅显示通过 Windows PnP 硬件信息确认的 USB 音频输入，并优先显示 WASAPI 这一套驱动接口，避免 MME / DirectSound 重复出现。内置声卡、蓝牙和未识别输入可在“全部本地音频输入”中查看；无法识别 USB 时请切换到该选项并手动选择驱动接口。一个 USB 设备如果暴露多个录音端点，仍可能有多条输入。

通过 Windows ContainerId 关联音频端点与 USB 硬件，唯一匹配时保存硬件实例 ID。**尚不保证能提取厂商序列号或物理端口。** 同名称设备暂按相同驱动接口下的枚举顺序匹配，重新插拔后请检查别名对应关系。驱动通道上限也可能与设备实际物理通道不同，需要实机确认。录制时新接入的设备在本次任务结束后发现，不会中途加入。

## SSH / ADB 采集模板

点击“添加远程设备”，填写连接信息、通道数、采样率、位深、启动命令和远端 WAV 路径。ADB 需安装 Android Platform Tools，并配置 `adb.exe` 路径；在设备上启用调试、允许本机授权。

启动命令必须**以前台方式运行**，不要添加 `&`、`nohup` 或自行转入后台。远程 shell 需支持 POSIX shell 语法。将输出文件写成 **`{remote_file}`**，不要自行给这个占位符加引号。软件会自动生成唯一远端文件名并完成 shell 引号处理，避免把上次的录音当成本次文件。

ADB / tinycap 示例（以设备实际工具参数为准）：

```text
启动命令：tinycap {remote_file} -c 8 -r 48000 -b 16
远端路径：/data/local/tmp/audio.wav
模板参数：8 通道 / 48000 Hz / 16 bit
停止命令：（留空，发送 Ctrl+C）
```

SSH / arecord 示例（确认声卡编号）：

```text
启动命令：arecord -D hw:0,0 -c 6 -r 48000 -f S16_LE {remote_file}
远端路径：/tmp/audio.wav
模板参数：6 通道 / 48000 Hz / 16 bit
```

实际远端文件类似 `/tmp/audio_9da123456789.wav`，路径会写入采集信息和日志。软件不会删除远端录音。

先“连接终端”，确认 shell 就绪，再勾选参与统一采集。SSH 首次连接会在日志显示 SHA256 主机指纹；与设备可信来源核对后，将其填入设备配置的“SSH SHA256 主机指纹”再连接。已有 OpenSSH known_hosts 记录也可使用。

停止命令留空时向终端发送 Ctrl+C；SSH / ADB 可以配置独立的停止命令，在另一个命令通道执行。命令需停止本次采集程序，不能只停止本地 adb 客户端。系统等待采集命令退出标记后才下载，不按固定延时假定文件已经写好；20 秒仍未退出会报异常，需检查远端进程。

## Telnet

- 自动识别常见 `login:` / `username:` / `password:` 提示。特殊登录流程可通过命令框交互登录。
- 当前采集模板使用 Ctrl+C 停止；不支持独立的 Telnet 停止命令。
- **Telnet 本身不传输文件**。要加入统一录制并自动保存，需要设备提供 HTTP 文件下载服务，并配置类似 `http://192.168.1.102/recordings/{filename}` 的地址。
- `{filename}` 是本次远端文件的文件名，`{remote_file}` 是本次完整路径，两者都会按 URL 编码替换。
- 没有文件服务的设备仍可使用终端，但不能在本版中参与自动保存流程。

## 输出

```text
语音采集_001_20261031_153000_a1b2c3/
├── 采集信息.json
├── USB麦克风A_设备ID/
│   ├── audio.wav
│   └── terminal.log
└── Android测试机_设备ID/
    ├── audio.wav
    └── terminal.log
```

每台设备一个多通道音频文件，声道顺序沿用驱动或远端程序的顺序，不合并不同设备。远程文件须为 PCM WAV / WAVEX / RF64；不符合模板通道、采样率或位深时保留为 `audio.download` 并报错。未实现原始 PCM 自动转 WAV。失败后保留远端文件，可修复问题后“重试取回文件”。

## 配置与开发

- Windows 配置：`%LOCALAPPDATA%\AudioCaptureStudio\devices.local.json`。
- 密码不写入配置；SSH 私钥仅保存路径。
- 采集命令与文件路径会写入配置和任务信息，请勿在采集命令中嵌入密码。
- 终端是文本输出视图，支持日志和命令交互，不是完整的 ANSI / VT 终端模拟器。
- 统一启停是软件协调，各设备使用独立时钟，**不保证采样同步，也不做漂移补偿**。
- 当前验证环境为 Linux 无显示环境，已测试演示采集与界面流程、远程退出和下载校验逻辑；Windows 驱动、设备真实采集、网络连接与打包结果仍需在 Windows 实机验证。

测试：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Windows 本机打包：双击 `build_windows.bat`。完成后分发整个 `dist\AudioCaptureStudio` 目录，运行其中的 `AudioCaptureStudio.exe`。ADB 工具仍需按本机配置安装。此源码包不包含已经构建好的 Windows EXE。

上传到独立 GitHub 仓库后，也可在 Actions 中手动运行 `Windows build`，下载 Windows 应用构建产物。已提供工作流配置，本次尚未在 GitHub 上运行。
