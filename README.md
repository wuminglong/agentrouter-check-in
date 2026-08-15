# AgentRouter Check-in

AgentRouter 本地自动签到脚本。

通过持久化 GitHub 浏览器登录态，每次执行时重新完成 GitHub OAuth 登录，从而触发 AgentRouter 签到，并尝试读取签到后的账户余额。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- 可正常访问 GitHub 和 AgentRouter

## 安装

克隆项目并安装依赖：

```bash
git clone https://github.com/wuminglong/agentrouter-check-in.git
cd agentrouter-check-in
uv sync
uv run python -m cloakbrowser install
```

创建本地配置：

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
AGENTROUTER_ACCOUNTS=["account1","account2"]
CHECKIN_PROXY_URL=http://127.0.0.1:7890
CHECKIN_HEADLESS=false
```

`account1`、`account2` 是本地 Profile 名称，可自行定义。

如果不需要代理：

```dotenv
CHECKIN_PROXY_URL=
```

为保持 GitHub OAuth 登录环境一致，当前签到固定使用有界面浏览器模式。

## 添加账号

首次使用时，需要为每个账号保存一次 GitHub 登录态：

```bash
uv run python checkin.py add account1
```

浏览器打开后，完成对应 GitHub 账号的登录和验证。

添加多个账号时分别执行：

```bash
uv run python checkin.py add account1
uv run python checkin.py add account2
```

查看已配置账号：

```bash
uv run python checkin.py list
```

示例：

```text
✅ account1: valid
✅ account2: valid
```

## 执行签到

执行所有已配置账号：

```bash
uv run python checkin.py
```

示例输出：

```text
[SYSTEM] AgentRouter GitHub OAuth 本地签到，账号数: 2
[account1] [INFO] 已检测到新的 AgentRouter session
[account1] [INFO] OAuth 回调已完成
[account1] [SUCCESS] 余额 $12.34，累计消耗 $100.00；本次签到 +$1.00

[account2] [INFO] 已检测到新的 AgentRouter session
[account2] [INFO] OAuth 回调已完成
[account2] [SUCCESS] 余额 $8.76，累计消耗 $50.00；本次总额度无新增（通常表示今天已经签到）

[STATS] 成功 2/2
```

## 重新登录

如果 GitHub 登录态失效，重新执行：

```bash
uv run python checkin.py add <name>
```

## 删除账号

```bash
uv run python checkin.py delete <name>
```

## 本地文件

以下文件包含本地配置或登录状态，已通过 `.gitignore` 排除：

```text
.env
.browser_profiles/
agentrouter_local_state.json
```

请勿提交或分享 `.browser_profiles/`，其中包含浏览器登录状态。
