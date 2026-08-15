# AgentRouter Check-in

一个只做一件事的本地 AgentRouter 自动签到项目：

**保存 GitHub 浏览器登录态 → 每天重新走 GitHub OAuth → 触发 AgentRouter 签到 → 查询余额。**

这个项目不包含：

- GitHub Actions 定时任务
- AnyRouter / 其他 Provider
- 邮箱密码登录
- Mihomo 订阅下载/节点筛选
- 钉钉、Bark、Telegram 等通知
- 原 fork 的旧签到逻辑
- 云端保存 GitHub Cookie / Browser Profile

## 为什么必须在本地运行

AgentRouter 的签到实际发生在 GitHub OAuth 登录完成时。

`.browser_profiles/` 保存的是 GitHub 浏览器登录态，因此应该始终留在自己的 Mac 上，不应上传到 GitHub Actions、Artifact、Cache 或仓库。

## 环境要求

- macOS
- Python 3.11+
- `uv`
- 可以访问 GitHub 和 AgentRouter 的网络环境

安装 uv：

```bash
brew install uv
```

安装项目依赖：

```bash
uv sync
uv run python -m cloakbrowser install
```

> 项目目前固定 `cloakbrowser==0.3.31`，因为这是当前实测签到成功的版本。不要为了消除升级提示而直接升级，后续单独验证新版兼容性再调整。

## 配置

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
AGENTROUTER_ACCOUNTS=["account1","account2"]
CHECKIN_PROXY_URL=http://127.0.0.1:7890
CHECKIN_HEADLESS=true
```

其中 `account1`、`account2` 只是本地 Profile 名称，可自行定义，不需要与 GitHub 用户名一致。

如果不需要代理：

```dotenv
CHECKIN_PROXY_URL=
```

## 首次添加账号

第一个账号：

```bash
uv run python checkin.py add account1
```

浏览器打开后登录对应的 GitHub 账号并完成可能的二次验证。

第二个账号：

```bash
uv run python checkin.py add account2
```

检查：

```bash
uv run python checkin.py list
```

正常应看到：

```text
✅ account1: valid
✅ account2: valid
```

## 每日签到

```bash
uv run python checkin.py
```

成功输出示例：

```text
[SYSTEM] AgentRouter GitHub OAuth 本地签到，账号数: 2
[account1] [INFO] 已检测到新的 AgentRouter session
[account1] [SUCCESS] 余额 $xx.xx，累计消耗 $xx.xx；本次签到 +$xx.xx

[account2] [INFO] 已检测到新的 AgentRouter session
[account2] [SUCCESS] 余额 $xx.xx，累计消耗 $xx.xx；本次签到 +$xx.xx

[STATS] 成功 2/2
```

如果今天已经签到过，余额不增加也是正常的：

```text
本次总额度无新增（通常表示今天已经签到）
```

## Codex Automation

建议让本地 Codex 每天 09:00 在本项目目录执行：

```bash
uv run python checkin.py
```

Automation 规则建议：

- 只执行签到，不修改代码
- 不执行 git pull / push / commit
- 使用现有 `.env`
- 使用现有 `.browser_profiles`
- `[STATS] 成功 N/N` 视为成功
- OAuth 成功但当天无余额新增，不应视为失败

## 账号重新登录

如果出现：

```text
GitHub 登录态已失效
```

重新执行：

```bash
uv run python checkin.py add <name>
```

## 删除账号

```bash
uv run python checkin.py delete <name>
```

## 安全

以下文件均为本地敏感状态，已加入 `.gitignore`：

```text
.env
.browser_profiles/
agentrouter_local_state.json
```

尤其是 `.browser_profiles/`，其中包含 GitHub 登录态，**禁止提交、上传或分享**。
