# 代理操作说明

本文件供 Codex、Claude Code 等代理在本项目中工作时使用，内容包括续办操作流程、硬性规则和修改代码的约定。用户的通用偏好以各自的全局指令为准。

## 项目概况

- 标准库 Python CLI，入口 `python3 -m jjz`，需在项目根目录运行。本机解释器是 `/opt/homebrew/opt/python@3.14/bin/python3.14`，定时任务固定使用它。
- `jjz/`：`api.py` 官方接口与业务登录交换、`config.py` 配置与申请模板校验、`domain.py` 记录筛选与续办计划、`runtime.py` 执行与提交保护、`storage.py` SQLite 提交记录与进程锁、`credentials.py` 凭证核验与保存、`auth.py` 手机号统一认证、`cli.py` 命令行。手机号加密通过标准库调用本机 OpenSSL。核心无第三方 Python 依赖；可选的验证码识别依赖列在 `requirements.txt`，装在同版本 `.venv`。环境要求见 README「运行环境」。
- 项目根目录下的 `.toml` 配置（`config.toml`、`config-*.toml`；`pyproject.toml` 除外）、`secrets/`、`data/` 是本机私有文件，已列入 `.gitignore`；对应示例是 `jjz/config.example.toml` 和 `jjz/login.example.toml`，`init` 只补建缺少的文件。配置文件不放账号密码，代理可以读取它排查问题（个人信息按规则 6 处理）。各配置数据目录下的 `journal.sqlite3` 是提交保护记录。
- 一辆车一份配置：默认 `config.toml`，其他车辆用 `config-<名称>.toml`，命令加 `--config` 指定，每份配置用独立的 `[storage].data_dir`。`vehicles` 只读列出账号下的车辆和对应配置；`auth-set --login` 保存凭证后、以及 `vehicles --create`，会为还没有配置的车生成 `config-car2.toml` 等，新配置的 `submission_enabled` 和 `auto_renew` 都是 `false`。`[renewal].auto_renew = false` 的车是按需办理，没写时为 `true`（自动续办）。同一账号下的车共用 `secrets/` 里的凭证和登录文件，自动重新登录按凭证文件互斥。说明见 README「多辆车」。
- 背景资料：[README](README.md)、[办理凭证获取](docs/login-usage.md)、[参考项目及与官方 H5 的差异](docs/reference-notes.md)。

## 硬性规则

1. **凭证**：不读取、打印或复制 `secrets/` 下任何文件的内容（业务凭证 `authorization.txt`、登录手机号和密码 `login.toml`），更不把这些值放进命令参数、日志、聊天、代码或代理记忆。更新业务凭证只能由用户本人在交互式终端运行 `python3 -m jjz auth-set` 输入本人 App 查询请求里的凭证，或运行 `python3 -m jjz auth-set --login --method password`（也支持 `sms`），完成手机号登录和目标车辆查询核验后直接保存。配置的 `[api].auto_relogin = true` 时，`status`、`plan`、`run`、`vehicles` 遇到凭证失效会由程序自己用 `secrets/login.toml` 的密码和本机 OCR 登录一次，核验后保存新凭证再重新执行；验证码识别不出或登录失败就直接报错，不等人输入。用户使用本项目即视为同意相关协议，程序内所有 `yes` 确认默认同意，不再询问。密码登录在 `secrets/login.toml` 配置了手机号和密码时由程序自行读取，只有 `login`、`auth-set --login` 和自动重新登录会读它。图形验证码由本机 OCR 识别并填入；交互登录时未识别出结果，或官方连续三次判定验证码错误，才由本人输入。代理可以在用户的终端标签页里启动这些命令，但不得替用户输入账号、密码或验证码，也不得读取终端里的验证码。`login` 只验证、不保存。获取方法见 [docs/login-usage.md](docs/login-usage.md)。
2. **提交**：只有 `run --submit` 会提交申请，且要求 `[renewal].submission_enabled = true`。除非用户在当次对话中明确要求，代理不得把这个开关或 `[renewal].auto_renew` 改成 `true`，也不得生成或加载带 `--submit` 的定时任务。
3. **不绕过保护**：程序返回 `blocked`、`uncertain`、`rejected` 时停下并报告原因。不要关闭证书校验，不要跳过或伪造服务端校验，不要为了放行而修改提交保护逻辑。
4. **六环内证**：每车每年最多 12 次。只在用户当次明确要求时办理，办理前先用 `status` 报告 `inside_remaining` 并请用户确认。
5. **解除保护**：`resolve` 只在用户本人到 App 核实确实没有提交、并在当次明确要求时执行。不要删除或改写 `data/` 下的文件。
6. **个人信息**：车牌、姓名、证件号、地址、坐标不得写进代码、测试、文档示例、日志或代理记忆。测试一律用合成数据。程序输出已经脱敏，汇报时沿用脱敏后的车牌。
7. **网络**：只调用 `jjz/api.py` 中列出的官方接口。新增或修改接口前，先对照官方 H5 公开脚本核实（见 docs/reference-notes.md）。

## 续办操作流程

用户让你"看下进京证""续一下"，或者定时代理执行时，按下面的顺序处理。所有命令都输出一行 JSON：退出码 0 表示正常；2 表示 `blocked`、`uncertain`、`rejected`、`error`，或检查未全部就绪。

项目根目录有多份配置（`config.toml`、`config-*.toml`）时，用户点名了车辆就只处理对应配置；没点名就逐份处理 `auto_renew` 不为 `false` 的配置，按需办理的车不处理。要确认车辆和配置的对应关系或各配置的 `auto_renew`，运行只读的 `python3 -m jjz vehicles`。每条命令都带上同一个 `--config <文件>`，汇报时分车说明。

1. 运行 `python3 -m jjz status`（只读）。
   - `action: error` 且 `category: auth_expired`：凭证失效。开启 `auto_relogin` 时程序已自动登录过一次，`relogin: failed` 和 `reason` 说明失败原因。停下，请用户按 docs/login-usage.md 重新取得凭证，再在终端运行 `auth-set`。
   - 输出带 `relogin: succeeded`：程序已自动重新登录并保存新凭证；带 `relogin: shared`：使用同一凭证的其他配置刚登录过，本次直接用了新凭证。两种情况输出都是重新执行后的结果，照常处理，汇报时说明。
   - 其他 `error`：报告 `reason`。`接口网络请求失败或超时` 可以稍后重试一次。
   - 正常：报告每张证的类型、状态、起止日期，以及 `can_apply`（服务端当前是否允许新申请）和 `inside_remaining`（内证剩余次数）。
2. 运行 `python3 -m jjz run`（默认六环外，只预演、不提交），按下表处理返回的 `action`。
3. 返回 `preview` 且 `server_checks: passed` 时：
   - 如果 `submission_enabled` 已是 `true`，运行 `python3 -m jjz run --submit`。
   - 否则告诉用户现在可以办理，按规则 2 等用户决定。
4. 提交之后报告 `action` 和 `attempt`，并提醒用户到 App 核对证件和有效期。之后再次运行 `status` 或 `run`，程序会自动核验结果，不会重复提交。
5. macOS 定时任务的输出在该配置数据目录下的 `launchd.log`（`config.toml` 默认是 `data/launchd.log`），每次运行追加一行 JSON，要看最近结果，读最后几行即可。Linux 的输出在 systemd 日志里，实例名是配置文件名，如 `journalctl --user -u jjz-renew@config.service -n 5 -o cat`。

| `action` | 含义 | 代理应对 |
| --- | --- | --- |
| `wait` | 有审核中/待生效申请，未到续办日或最早时间，内证仍有效，或定时运行跳过了按需办理的车 | 无需操作，报告 `reason` |
| `preview` | 已到续办时间且服务端校验全部通过，本次未提交 | 按第 3 步处理 |
| `blocked` | 模板不完整、服务端校验未通过、已有同日提交记录、同一辆车有多份配置，或状态需人工核对 | 停下，报告 `reason`；带 `category: auth_expired` 时按凭证失效处理 |
| `accepted` | 接口已受理，查询中还没看到新证 | 不要重发，稍后再查 |
| `verified` | 服务端已有对应申请，审批结果或有效期待定 | 稍后再查 |
| `approved` | 查到审核通过的新证 | 报告新证有效期，提醒到 App 核对 |
| `uncertain` | 提交时超时或网络中断，结果未知 | 绝不重发；稍后再查，若仍没有对应申请，请用户到 App 核实 |
| `rejected` | 服务端拒绝了这次提交 | 停下，请用户到 App 查看原因 |
| `error` | 配置、网络或凭证问题 | 按第 1 步处理 |

## 定时代理检查

Claude 应用里的定时任务可以无人值守地运行本节流程，任务名称和运行时间由用户按自己的证件周期设置。六环外证每次 7 天，`outside_lead_days = 0` 时到期当天申请、次日生效，所以续办任务安排在每个周期的到期日、`earliest_time` 之后运行即可；每天运行也无妨，未到续办日时只返回 `wait`。同一天可以再安排一次复查，兼作当天的重试；提交保护会防止重复提交，两次同时运行时进程锁会让后一次报错退出。定时任务完全按"续办操作流程"执行，只处理六环外证。多辆车时，任务说明里写明要处理的配置文件，每辆车可以单独建任务、按各自证件周期安排时间；没写明时按流程逐份处理。定时代理只处理自动续办的车：`run` 和 `run --submit` 都加 `--scheduled`，按需办理（`auto_renew = false`）的车会直接返回 `wait`，这类配置也不运行 `status`。

- 无人值守时只运行 `status`、`run` 和只读的 `vehicles`，满足第 3 步条件时再运行 `run --submit`。不修改配置文件、代码和定时任务，不运行 `resolve`、`login`、`auth-set`、`vehicles --create`。
- 自动重新登录只由程序在 `status`、`run`、`vehicles` 内部完成。输出仍是凭证失效（`category: auth_expired`）时立即结束本次运行，按下方要求汇报并推送，请用户本人重新登录。代理自己不尝试登录，不在终端标签页启动需要输入的命令，不等待用户输入，也不反复重试。
- `status` 出现网络类 `error` 时，等约 1 分钟重试一次。
- 最后用不超过 8 行的简体中文汇报：每张证的类型、状态、起止日期；`can_apply`、`inside_remaining`；`run` 的 `action` 和 `reason`；是否提交，以及提交结果的 `action` 和 `attempt`；是否自动重新登录（`relogin`）；需要用户本人做的事（没有就写"无需操作"）。车牌只用程序输出的脱敏形式。
- 以下情况，在 PushNotification 工具可用时，再发一条一句话推送，内容与汇报一致：刚提交了申请、已可办理但提交开关未开、凭证失效、`blocked`、`uncertain`、`rejected`。其余情况不发推送。
- 检查结果是临时状态，不写入 mem0 或其他记忆。

## 后台定时任务

```sh
# 生成每 30 分钟运行一次的任务（固定六环外）；文件已存在时先移走
python3 -m jjz schedule            # 只预演
python3 -m jjz schedule --submit   # 实际提交，须遵守规则 2
plutil -lint data/local.jjz.renew.config.plist
launchctl bootstrap "gui/$(id -u)" "$PWD/data/local.jjz.renew.config.plist"
launchctl print "gui/$(id -u)/local.jjz.renew.config"
launchctl list | grep local.jjz.renew   # 已加载的全部续办任务；停止或排查时以这里列出的任务名为准
launchctl bootout "gui/$(id -u)/local.jjz.renew.config"
```

任务名是 `local.jjz.renew.<配置文件名>`，plist 默认写在该配置的数据目录；其他车辆加 `--config`，后续命令用 `schedule` 输出的 `label` 和 `path`。按需办理（`auto_renew = false`）的配置不生成任务；任务运行的是 `run --scheduled`，配置改为按需办理后任务直接跳过这辆车。Mac 必须开机、用户登录且联网；睡眠时任务不运行。

Linux 用 systemd 用户定时器模板 `jjz-renew@.service`、`jjz-renew@.timer`，每辆车启用一个以配置文件名为实例名的定时器，`systemctl --user list-timers 'jjz-renew*'` 列出已启用的全部续办定时器，单元文件和命令见 README「自动续办」。带 `--submit` 的单元文件同样须遵守规则 2。同一辆车只在一台机器上运行定时任务，提交记录不跨机器共享。

## 排错

- `doctor` 只报内证模板未填写时，不影响六环外的自动续办。

## 修改代码

- 只用标准库（可选的验证码识别除外，依赖记在 `requirements.txt` 和 `pyproject.toml` 的 `ocr`），延续现有写法：错误消息固定、可安全展示，不输出原始响应、凭证或个人信息。
- 提交内容要与北京交警当前 H5 申请页保持一致。字段或接口有变时，先核对官方脚本，再同步更新 `runtime.py`、`config.py`、`jjz/config.example.toml` 和 docs/reference-notes.md。
- 改完运行 `python3 -m unittest discover -s tests`，必须全部通过。
- 文档只写当前用法，不写"新增""旧版仍兼容"这类变更经过。
