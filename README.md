# GAC Computer Use（DSH 插件）

面向 Windows 的 Computer Use / GUI 自动化能力，打包成 **DSH 插件**，可上架到 DSH 插件市场。它让智能体通过一组暴露给 DSH 的 Model Context Protocol（MCP）工具来操控本地机器——截图、移动鼠标、点击、输入文本、读写剪贴板、启动应用、切换桌面等。

这是 GAC 协议 v1 服务端与客户端的**自包含重实现**。不随包分发任何第三方运行时：传输层加密采用公开的 RFC 8439 ChaCha20-Poly1305，协议客户端依据线格式规范从零编写，Win32 后端仅依赖 `ctypes` 与 Python 标准库。你**不需要**豆包、`gac_runtime_extract`，也不需要安装任何 pip 包。

## 它能做什么

- **`gac-mcp-bridge`** —— 单个 stdio 模式的 MCP 服务（`python/gac_server/mcp_bridge.py`），在随机回环端口上启动自带的 GAC TCP 服务，并通过 `@deepseek-ai/dsh-mcp-client` 宿主向 DSH 提供 18 个 computer-use 工具。
- **`gac-pip-view`** —— 侧边栏标签页 + 同源 HTTP 代理，实时展示一个**隔离的"pip"虚拟桌面**画面。在那里启动的应用永远不会触碰你真实的屏幕、鼠标或键盘。

### 暴露的工具（`serverName: gac`）

| 工具 | 用途 |
|------|------|
| `gac_screenshot` | 截取当前平面（主桌面为全屏，pip 为最前窗口），返回 PNG。 |
| `gac_list_apps` | 列出可启动的 Windows 应用。 |
| `gac_launch_app` | 按精确名称启动应用。 |
| `gac_click` / `gac_double_click` / `gac_right_click` | 在归一化 0–1000 坐标处点击。 |
| `gac_hover` | 移动鼠标指针。 |
| `gac_drag` | 在两个归一化坐标点之间拖拽。 |
| `gac_scroll` | 在指定位置滚动。 |
| `gac_type` | 输入 Unicode 文本。 |
| `gac_hotkey` / `gac_press` | 组合键 / 单键。 |
| `gac_wait` | 等待若干毫秒。 |
| `gac_get_clipboard` / `gac_set_clipboard` | 读 / 写剪贴板。 |
| `gac_copy_selection` | Ctrl+C 后读取剪贴板内容。 |
| `gac_switch_desktop` | 切换平面：`main`（真实桌面）或 `pip`（隔离桌面）。 |
| `gac_handoff` | **暂停并把控制权交给真人**，用于登录 / 二次验证 / 验证码 / 支付，侧边栏提供"完成"按钮。 |

## 支持平台

**仅支持 Windows。** 服务端通过 `ctypes` 调用 Win32 API（`user32` / `gdi32` / `kernel32`）。本机需安装 **Python 3.10+**（仅标准库，无需 `pip install`）。

## 安装

### 从插件市场安装
在 DSH 插件市场安装 `gac-computer-use` 后，运行安装脚本，将 MCP 命令绑定到你的 Python：

```bash
cd <插件安装目录>
node scripts/setup.mjs
```

随后重载 DSH 配置（或重启 DSH）。

### 手动 / 本地开发
```bash
git clone <仓库地址> gac-computer-use
cd gac-computer-use
node scripts/setup.mjs        # 将你的 Python 路径写入 cordis.patch.yml
# 将此文件夹软链或复制到你的 DSH 插件目录，然后重载
```

`setup.mjs` 会查找 Python 3.10+（Windows 上优先 `py -3`，其次 `python3` / `python`），解析为绝对可执行路径，在非 Windows 时给出警告，并就地重写 `cordis.patch.yml`。重复运行安全。

## 整体架构

```
DSH ──(stdio)──> mcp_bridge.py  ──(进程内 TCP)──> GAC 服务端 (server.py)
                       │                                      │
                       │ ComputerUse 客户端 (gac_client.py)    │ RFC 8439 认证加密
                       │                                      │
                  侧边栏标签  <──HTTP /gac-pip/*──  pip_view.py (MJPEG + 人工接管)
                       │                                      │
                  lib/client.js  <──代理──  lib/index.js  <── 127.0.0.1:52360
```

- **协议**：TCP，4 字节 `GACF` 魔数 + u32 长度帧。基于 token 挑战的 HMAC-SHA256 握手通过 HKDF 派生会话密钥；随后每条命令均用 ChaCha20-Poly1305（RFC 8439）加密封装。详见 `gac_crypto.py` 与 `server.py`。
- **坐标**：归一化到 0–1000 空间（客户端再缩放到真实屏幕 / 窗口），与上游 computer-use 约定一致。
- **pip 桌面**：`gac_switch_desktop("pip")` 将后续操作转移到私有的 `HDESK` 桌面；侧边栏实时展示，并可通过 `gac_handoff` 在敏感步骤（登录 / 支付等）把控制权交给你。

## 安全模型

- 服务端**仅绑定回环地址**（`127.0.0.1`），外部网络无法访问。
- 会话通过共享密钥认证（`HKDF(token, secret)`）；密钥每次启动生成，经环境变量传给桥接进程（作用域限定在桥接进程内）。
- `gac_handoff` 的存在正是为了让智能体**不**替你执行登录 / 二次验证 / 支付操作——它会暂停并把手键盘交还给你。

## 开发与测试

自包含端到端测试（无需任何外部运行时），在同一个进程内通过真实客户端与服务端走通全部 19 条命令路径：

```bash
python python/gac_server/test_e2e.py
```

若要针对类 DSH 的 stdio 传输手动运行 MCP 桥：

```bash
python python/gac_server/mcp_bridge.py      # 通过 stdin/stdout 讲 JSON-RPC
```

## 许可证

MIT —— 见 [LICENSE](./LICENSE)。
