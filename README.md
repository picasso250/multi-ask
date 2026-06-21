# multi-ask

`multi-ask` 是一个本地浏览器自动化命令行工具，用来把同一个问题并行发送给 ChatGPT 和 Gemini，并把两边的原始回复一起输出。

它通过 Chrome DevTools Protocol 连接你已经登录的浏览器页面，不保存账号密码，也不调用官方模型 API。

## 功能

- 同时询问 ChatGPT 和 Gemini
- 自动启动本项目的本地 daemon
- 支持查看 daemon 状态和开启新对话
- daemon 会识别自己的服务身份，避免误连到同端口上的其他服务
- 浏览器标签页被关闭后，会重新扫描已有标签页；找不到目标页面时会自动打开

## 前置条件

- Python 3.12 或兼容版本
- 已安装 Playwright Python 包
- 本机 Chrome 或 Chromium 已开启 CDP 调试端口，默认地址为 `http://127.0.0.1:9222`
- 浏览器里已经登录 ChatGPT 和 Gemini

示例启动 Chrome：

```powershell
chrome.exe --remote-debugging-port=9222
```

如果 `chrome.exe` 不在 `PATH` 中，请使用你的 Chrome 完整路径。

## 快速开始

先确认两个 daemon 状态；如果缺失，显式启动：

```powershell
python .\multi_ask.py status --json --ensure
```

向两个 provider 并行提问：

```powershell
python .\multi_ask.py ask "reply only 42"
```

输出 JSON：

```powershell
python .\multi_ask.py ask "用一句话解释 MCP" --json
```

开启两个新对话：

```powershell
python .\multi_ask.py new-chat
```

只查看状态，不自动启动 daemon：

```powershell
python .\multi_ask.py status --json
```

## daemon 和端口

`multi_ask.py` 默认管理两个本地 daemon：

- ChatGPT: `127.0.0.1:53165`
- Gemini: `127.0.0.1:53168`

`ask` 和 `new-chat` 会自动启动缺失的 daemon；`status` 默认只探测状态，只有加 `--ensure` 才会启动缺失 daemon。

每个 daemon 的 `/status` 会返回：

- `service: "multi-ask-daemon"`
- `provider: "chatgpt"` 或 `"gemini"`

如果目标端口被其他 HTTP 服务占用，`multi_ask.py` 会返回 `identity_mismatch`，不会杀掉或覆盖该服务。

## 单 provider CLI

也可以直接操作单个 provider daemon：

```powershell
python .\chatgpt_agent.py status --json
python .\chatgpt_agent.py ask "hello"
python .\chatgpt_agent.py new-chat

python .\gemini_agent.py status --json
python .\gemini_agent.py ask "hello"
python .\gemini_agent.py new-chat
```

单 provider CLI 不会自动启动 daemon；自动启动只在 `multi_ask.py` 中实现。

## 日志

自动启动的 daemon 会把日志写到项目目录：

- `chatgpt_agent.out.log`
- `chatgpt_agent.err.log`
- `gemini_agent.out.log`
- `gemini_agent.err.log`

这些日志不会提交到 Git。

## 常见问题

### `daemon_unavailable`

通常表示 daemon 没有运行，或者端口不可连接。可以运行：

```powershell
python .\multi_ask.py status --json --ensure
```

### `identity_mismatch`

目标端口上有服务，但不是本项目 daemon。请换端口，或停止占用该端口的其他服务。

### 找不到浏览器或 CDP

确认浏览器是用 `--remote-debugging-port=9222` 启动的，并且 `http://127.0.0.1:9222/json/version` 可访问。

### 页面要求登录

本项目复用你的浏览器会话。请先在浏览器中手动登录 ChatGPT 和 Gemini。

## 许可证

当前仓库没有提供开源许可证。未经授权时，请不要假定可以复制、修改或再分发本项目代码。
