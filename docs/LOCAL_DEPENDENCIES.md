# 本地与远端依赖

## 本地启动环境

| 依赖 | 要求 | 使用场景 |
| --- | --- | --- |
| Python | 3.11+ | 全部本地命令 |
| PyYAML | >=6.0,<7 | YAML 配置读取 |
| Git | PATH 中可执行 | deploy 快照和 doctor |
| Node.js | 20+ | npussh / npursync |
| npussh / npursync | `npm install -g npussh@0.2.4` | 远端连接及上传 |
| OpenSSH client | 提供 ssh | 远端连接 |
| rsync | 系统 rsync | 任务文件传输 |

```bash
python3 -m pip install /absolute/path/afxx-1.0-py3-none-any.whl
npm install -g npussh@0.2.4
npussh login
npussh list
```

npursync 由 npussh npm 包提供。npm 用于安装工具。PyYAML 可装在现有启动解释器中。连接工具使用本机 PATH，远端 Python 包由 workflow 管理。

## 启动检查

工具预检检查 PATH 并执行版本命令，每项最多 10 秒。连接前以非交互方式运行 npussh list，最多 30 秒，验证登录和服务可用性。检查通过后，目标 NPU 的 SSH 连通性仍由实际操作确认。

| 命令 | 本地连接依赖 |
| --- | --- |
| init/update/deploy/replay/check | npussh、Node.js、ssh、npursync、rsync；deploy 另需 Git |
| status / issue verify | npussh、Node.js、ssh |
| doctor | 按 deploy 检查工具及登录 |
| issue claim/preview/renew/status/wait | 直接 HTTPS 访问平台，读取本地凭据 |
| 任务命令 --dry-run | 离线配置检查；deploy/doctor 仍需 Git |

status 和 issue-* 命令要求实际查询或操作，不支持 dry-run。init/update 支持 detach。

## 远端环境

远端使用 Linux NPU 主机，需要用于任务启动的系统 Python 3.9+、curl、rsync、构建工具和 LLVM/LLD。系统 Python 不必是 3.11，也不要求其具备 SSL、SQLite 或 venv 能力。init 自动下载托管 Python 3.11 到 `<workflow_root>/python/`，通过完整能力检查后才准备 CANN 和目标 venv。检查包括 ssl、ctypes、venv、ensurepip、SQLite >=3.35，以及临时 venv/pip 创建验证。uv 默认安装在专用 venv 内；引导下载工具位于 python/bootstrap/，两者均自动准备。`python_env.base_python`、`uv_bin` 仅用于主动覆盖默认工具。

CANN 在 init 中检查或安装，AF run 包在 deploy 中构建和安装。远端需具备下载配置包源的访问能力。平台请求需要 HTTPS 连通性和有效用户凭据。AFX 官方客户端独立运行，按 AFX 文档准备其运行依赖。

任务上传时的 config.yaml 使用可由标准库读取的 JSON 表示。环境冻结后写入 YAML，平台重试使用专用 venv 中的 PyYAML 读取。直接在工具目录执行 `python -m afxx.environment.python` 的 YAML 入口时，启动解释器也需要 PyYAML。

## 测试依赖

运行脚本测试需 pytest、PyYAML、Git 和 rsync。Python 3.11 使用标准库 tomllib。MCP 协议测试需要可选 MCP SDK；开发测试安装使用 `python3 -m pip install -e '.[dev,mcp]'`。

```bash
python3 -m pytest tests -q
```
