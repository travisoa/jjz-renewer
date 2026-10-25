# 代理操作说明

本文件供 Codex、Claude Code 等代理在本项目中工作时使用，内容包括续办操作流程、硬性规则和修改代码的约定。用户的通用偏好以各自的全局指令为准。

## 项目概况

- 标准库 Python CLI，入口 `python3 -m jjz`，需在项目根目录运行。本机解释器是 `/opt/homebrew/opt/python@3.14/bin/python3.14`，定时任务固定使用它。
- `jjz/`：`api.py` 官方接口与业务登录交换、`config.py` 配置与申请模板校验、`domain.py` 记录筛选与续办计划、`runtime.py` 执行与提交保护、`storage.py` SQLite 提交记录与进程锁、`credentials.py` 凭证核验与保存、`auth.py` 手机号统一认证、`cli.py` 命令行。手机号加密通过标准库调用本机 OpenSSL。核心无第三方 Python 依赖；可选的验证码识别依赖列在 `requirements.txt`，装在同版本 `.venv`。环境要求见 README「运行环境」。
- `config.toml`、`secrets/`、`data/` 是本机私有文件，已列入 `.gitignore`；对应示例是 `jjz/config.example.toml` 和 `jjz/login.example.toml`，`init` 只补建缺少的文件。`config.toml` 不放账号密码，代理可以读取它排查问题（个人信息按规则 6 处理）。`data/journal.sqlite3` 是提交保护记录。
- 背景资料：[README](README.md)、[办理凭证获取](docs/login-usage.md)、[参考项目及与官方 H5 的差异](docs/reference-notes.md)。

## 硬性规则

1. **凭证**：不读取、打印或复制 `secrets/` 下任何文件的内容（业务凭证 `authorization.txt`、登录手机号和密码 `login.toml`），更不把这些值放进命令参数、日志、聊天、代码或代理记忆。更新业务凭证只能由用户本人在交互式终端运行 `python3 -m jjz auth-set` 输入本人 App 查询请求里的凭证，或运行 `python3 -m jjz auth-set --login --method password`（也支持 `sms`），完成手机号登录和目标车辆查询核验后直接保存。用户使用本项目即视为同意相关协议，程序内所有 `yes` 确认默认同意，不再询问。密码登录在 `secrets/login.toml` 配置了手机号和密码时由程序自行读取，只有 `login` 和 `auth-set --login` 会读它。图形验证码由本机 OCR 识别并填入；未识别出结果，或官方连续三次判定验证码错误时，才由本人输入。代理可以在用户的终端标签页里启动这些命令，但不得替用户输入账号、密码或验证码，也不得读取终端里的验证码。`login` 只验证、不保存。获取方法见 [docs/login-usage.md](docs/login-usage.md)。
2. **提交**：只有 `run --submit` 会提交申请，且要求 `[renewal].submission_enabled = true`。除非用户在当次对话中明确要求，代理不得把这个开关改成 `true`，也不得生成或加载带 `--submit` 的定时任务。
3. **不绕过保护**：程序返回 `blocked`、`uncertain`、`rejected` 时停下并报告原因。不要关闭证书校验，不要跳过或伪造服务端校验，不要为了放行而修改提交保护逻辑。
4. **六环内证**：每车每年最多 12 次。只在用户当次明确要求时办理，办理前先用 `status` 报告 `inside_remaining` 并请用户确认。
5. **解除保护**：`resolve` 只在用户本人到 App 核实确实没有提交、并在当次明确要求时执行。不要删除或改写 `data/` 下的文件。
6. **个人信息**：车牌、姓名、证件号、地址、坐标不得写进代码、测试、文档示例、日志或代理记忆。测试一律用合成数据。程序输出已经脱敏，汇报时沿用脱敏后的车牌。
7. **网络**：只调用 `jjz/api.py` 中列出的官方接口。新增或修改接口前，先对照官方 H5 公开脚本核实（见 docs/reference-notes.md）。

## 续办操作流程

用户让你"看下进京证""续一下"，或者定时代理执行时，按下面的顺序处理。所有命令都输出一行 JSON：退出码 0 表示正常；2 表示 `blocked`、`uncertain`、`rejected`、`error`，或检查未全部就绪。

1. 运行 `python3 -m jjz status`（只读）。
   - `action: error` 且 `category: auth_expired`：凭证失效。停下，请用户按 docs/login-usage.md 重新取得凭证，再在终端运行 `auth-set`。
   - 其他 `error`：报告 `reason`。`接口网络请求失败或超时` 可以稍后重试一次。
   - 正常：报告每张证的类型、状态、起止日期，以及 `can_apply`（服务端当前是否允许新申请）和 `inside_remaining`（内证剩余次数）。
2. 运行 `python3 -m jjz run`（默认六环外，只预演、不提交），按下表处理返回的 `action`。
3. 返回 `preview` 且 `server_checks: passed` 时：
   - 如果 `submission_enabled` 已是 `true`，运行 `python3 -m jjz run --submit`。
   - 否则告诉用户现在可以办理，按规则 2 等用户决定。
4. 提交之后报告 `action` 和 `attempt`，并提醒用户到 App 核对证件和有效期。之后再次运行 `status` 或 `run`，程序会自动核验结果，不会重复提交。
5. 定时任务的输出在 `data/launchd.log`，每次运行追加一行 JSON。要看最近结果，读最后几行即可。

| `action` | 含义 | 代理应对 |
| --- | --- | --- |
| `wait` | 有审核中/待生效申请，未到续办日或最早时间，或内证仍有效 | 无需操作，报告 `reason` |
| `preview` | 已到续办时间且服务端校验全部通过，本次未提交 | 按第 3 步处理 |
| `blocked` | 模板不完整、服务端校验未通过、已有同日提交记录，或状态需人工核对 | 停下，报告 `reason`；带 `category: auth_expired` 时按凭证失效处理 |
| `accepted` | 接口已受理，查询中还没看到新证 | 不要重发，稍后再查 |
| `verified` | 服务端已有对应申请，审批结果或有效期待定 | 稍后再查 |
| `approved` | 查到审核通过的新证 | 报告新证有效期，提醒到 App 核对 |
| `uncertain` | 提交时超时或网络中断，结果未知 | 绝不重发；稍后再查，若仍没有对应申请，请用户到 App 核实 |
| `rejected` | 服务端拒绝了这次提交 | 停下，请用户到 App 查看原因 |
| `error` | 配置、网络或凭证问题 | 按第 1 步处理 |

## 定时代理检查

Claude 应用里的定时任务可以无人值守地运行本节流程，任务名称和运行时间由用户按自己的证件周期设置。六环外证每次 7 天，`outside_lead_days = 0` 时到期当天申请、次日生效，所以续办任务安排在每个周期的到期日、`earliest_time` 之后运行即可；每天运行也无妨，未到续办日时只返回 `wait`。同一天可以再安排一次复查，兼作当天的重试；提交保护会防止重复提交，两次同时运行时进程锁会让后一次报错退出。定时任务完全按"续办操作流程"执行，只处理六环外证。

- 无人值守时只运行 `status`、`run`，满足第 3 步条件时再运行 `run --submit`。不修改 `config.toml`、代码和定时任务，不运行 `resolve`、`auth-set`。
- `status` 出现网络类 `error` 时，等约 1 分钟重试一次。
- 最后用不超过 8 行的简体中文汇报：每张证的类型、状态、起止日期；`can_apply`、`inside_remaining`；`run` 的 `action` 和 `reason`；是否提交，以及提交结果的 `action` 和 `attempt`；需要用户本人做的事（没有就写"无需操作"）。车牌只用程序输出的脱敏形式。
- 以下情况，在 PushNotification 工具可用时，再发一条一句话推送，内容与汇报一致：刚提交了申请、已可办理但提交开关未开、凭证失效、`blocked`、`uncertain`、`rejected`。其余情况不发推送。
- 检查结果是临时状态，不写入 mem0 或其他记忆。

## 后台定时任务

```sh
# 生成每 30 分钟运行一次的任务（固定六环外）；文件已存在时先移走
python3 -m jjz schedule            # 只预演
python3 -m jjz schedule --submit   # 实际提交，须遵守规则 2
plutil -lint data/local.jjz.renew.plist
launchctl bootstrap "gui/$(id -u)" "$PWD/data/local.jjz.renew.plist"
launchctl print "gui/$(id -u)/local.jjz.renew"
launchctl bootout "gui/$(id -u)/local.jjz.renew"
```

Mac 必须开机、用户登录且联网；睡眠时任务不运行。

## 排错

- `doctor` 只报内证模板未填写时，不影响六环外的自动续办。

## 修改代码

- 只用标准库（可选的验证码识别除外，依赖记在 `requirements.txt` 和 `pyproject.toml` 的 `ocr`），延续现有写法：错误消息固定、可安全展示，不输出原始响应、凭证或个人信息。
- 提交内容要与北京交警当前 H5 申请页保持一致。字段或接口有变时，先核对官方脚本，再同步更新 `runtime.py`、`config.py`、`jjz/config.example.toml` 和 docs/reference-notes.md。
- 改完运行 `python3 -m unittest discover -s tests`，必须全部通过。
- 文档只写当前用法，不写"新增""旧版仍兼容"这类变更经过。
