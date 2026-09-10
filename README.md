# Genshin Subtitle Translator

一个只面向英文版《原神》剧情字幕的 Windows 屏幕翻译工具。按 `Ctrl+Shift+T` 进入翻译待命状态；需要翻译当前完整字幕时按单独的 `T`，程序立即截取屏幕下部居中的英文字幕区域并进行一次本机 OCR；识别到文字后才请求翻译 API，并将简体中文以鼠标穿透的置顶文字层显示在英文字幕下方。

它不修改游戏文件，不注入进程，不读取内存，也不模拟游戏输入。是否允许第三方覆盖层仍以游戏运营方规则为准，请自行评估使用风险。

## 安装

在 PowerShell 中执行：

```powershell
cd Genshin-Subtitle-Translator
py -3 -m venv .venv-cpython
.\.venv-cpython\Scripts\python.exe -m pip install --upgrade pip
.\.venv-cpython\Scripts\python.exe -m pip install -r requirements.txt
```

本机还需要安装 **Tesseract OCR**（英文识别语言包）。项目默认在 `tesseract-ocr\tesseract.exe` 查找它；本仓库不附带 OCR 或 Python 二进制，请自行安装并配置路径。若手动安装到其他位置，在 `config.json` 的 `capture.tesseract_path` 填入完整路径，例如 `C:\Program Files\Tesseract-OCR\tesseract.exe`。推荐安装方式：

```powershell
winget install --id UB-Mannheim.TesseractOCR -e
```

安装后重新打开 PowerShell。

## 配置翻译 API

复制模板，然后填写你所选服务商的兼容 OpenAI Chat Completions 接口地址和模型名：

```powershell
Copy-Item .\config.example.json .\config.json
notepad .\config.json
```

不要把密钥写入配置文件。按 `api_key_env` 的名字，在启动前设置环境变量：

```powershell
$env:GENSHIN_TRANSLATOR_API_KEY = '你的 API Key'
```

大多数兼容 OpenAI 的廉价模型只需要改这两项：

```json
"chat_completions_url": "https://你的服务商/v1/chat/completions",
"model": "你选择的模型"
```

`api.max_tokens` 默认是 `96`，足够覆盖较长的中文字幕，并避免为一条字幕预留过多生成长度。每次手动采样后，`genshin_translator.log` 会记录截图、OCR 和翻译 API 各自的耗时（不记录字幕内容或 API Key），可据此判断网络模型是否仍是主要延迟来源。

`capture` 的默认区域基于 1920x1080 的长剧情字幕：横向 `15% - 85%`、纵向 `81.5% - 93.5%`。它覆盖最多三行英文字幕，同时避开角色名称区域。中文固定在英文字幕下方的底部安全区，优先单行并按长度自动缩小，避免长句重叠。翻译开启后不会轮询截图、OCR 或调用 API，只有按单独的 `T` 才会采样一次。分辨率变化时按比例保持一致；如识别到角色名或漏字，只需微调这四个比例。

## 运行

通过管理员脚本启动：

```powershell
.\run_as_admin.ps1
```

启动后，任务栏右下角会显示圆点系统托盘图标：灰色表示翻译关闭，绿色表示翻译开启。右键图标可切换翻译或退出程序。`Ctrl+Shift+T` 开启翻译待命，单独按 `T` 对当前字幕采样一次，`Ctrl+Shift+Q` 暂停翻译但不退出程序；之后可再次按 `Ctrl+Shift+T` 恢复。输入监听仅观察 `T`，始终把按键交还给游戏。控制台日志写在 `genshin_translator.log`，不记录 API Key。

为了让独立置顶层稳定显示在游戏上方，建议使用原神的无边框窗口模式；程序与游戏都应以管理员权限运行。

## 紧急停止

覆盖层使用 `WM_NCHITTEST=HTTRANSPARENT`，所有鼠标命中会直接交给下层游戏窗口。如果系统输入仍异常，按 `Ctrl+Alt+Del` 打开系统安全界面后启动任务管理器，结束命令行包含 `genshin_translator.py` 的 Python 进程。

正常恢复输入后，也可以双击 `force_stop.cmd`。它会请求管理员权限，并且只结束本项目的翻译器 Python 进程，不会结束其他 Python 程序。

可先不依赖 OCR 和 API 预览覆盖层位置：

```powershell
.\run_as_admin.ps1 -Preview
```

## 打包

安装依赖后，可将程序打包为始终请求管理员权限的单文件 EXE：

```powershell
.\.venv-cpython\Scripts\python.exe -m pip install pyinstaller
.\build_exe.ps1
```

生成文件为 `dist\GenshinSubtitleTranslator.exe`。运行 EXE 时，仍需在其同目录放置已配置的 `config.json`。


## 求职展示与验证边界

采用 mss、Pillow、Tesseract、requests 和 pystray 等成熟组件，工程重点是按需截图、OCR 预处理、术语表、异步翻译与鼠标穿透覆盖层。界面与游戏内实际表现须在 Windows 上验证。随仓库提供的检查结果不代表已完成本轮游戏内端到端验收。

`run_as_admin.ps1` 为跨 UAC 启动会将 API Key 保存到当前用户环境变量中；不希望持久保存时，请自行在已提升权限的终端设置进程环境变量并运行 Python 主程序。
