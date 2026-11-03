# 进京证定期续办

在 macOS 或 Linux 上运行的 Python 命令行工具：查询北京进京证状态，按官方规则自动续办六环外证，六环内证按需手动办理。核心功能只用标准库。可以由 macOS launchd 或 Linux systemd 定时任务运行，也可以由 Codex、Claude Code 等代理按 [AGENTS.md](AGENTS.md) 的流程代为操作。本项目参考了 [bihongfly/jinjingzheng](https://github.com/bihongfly/jinjingzheng)，代码为独立实现，见下文"参考与致谢"。

## 功能

- 查询证件状态和有效期、服务端当前是否可办（`can_apply`）、六环内证剩余次数（`inside_remaining`）。
- 按官方接续规则计算续办时间；提交前依次调用全部官方校验接口，校验全部通过才提交。
- 先把提交意图写入 SQLite 再提交；超时、中断或结果不明时只查询核验，不会盲目重发。
- 业务凭证有两种取得方式：在终端用手机号（密码或短信）登录，核验目标车辆后保存；或从北京交警 App 的查询请求中取得后手动保存。
- 图形验证码可由 `ddddocr` 在进程内自动识别，识别失败时再由本人输入。
- 凭证到期后，下次办理时自动登录续期（`auto_relogin`，示例配置默认开启，用密码和本机验证码识别）。登录各环节的验证情况见 [验证范围](docs/login-usage.md#验证范围与故障处理)。
- 系统定时任务可选用飞书机器人私信结果卡片，办理没成功也能及时知道；AI 代理定时任务由代理推送通知。

## 运行环境

| 依赖 | 要求 | 用途 | 已测试版本 |
| --- | --- | --- | --- |
| macOS | 开机、用户已登录并联网；睡眠时错过的定时任务在唤醒后补跑 | 进程锁（`fcntl`）、launchd 定时任务、用默认浏览器打开验证码图片 | — |
| Linux | 联网，装有时区数据（`tzdata`）；定时任务要在未登录时运行须开启 linger | 进程锁（`fcntl`）、systemd 用户定时任务；没有图形界面时输出验证码图片路径，由本人自行打开 | Debian 13 |
| Python | 3.11 及以上（标准库自带 `tomllib`） | 运行程序，核心功能不需要第三方包 | 3.14（macOS）、3.11（Debian 13） |
| `openssl` 命令 | 在 `PATH` 中，支持 `pkey`、`pkeyutl` | 手机号登录时按官方协议做 RSA 加密 | OpenSSL 3.6（macOS）、3.5（Debian 13）；macOS 自带的 LibreSSL 3.3 |
| `ddddocr`、Pillow（可选） | 按 [requirements.txt](requirements.txt) 装进项目 `.venv`，`.venv` 须与运行程序的解释器是同一个 Python 小版本 | 自动识别图形验证码 | ddddocr 1.6.1、Pillow 13.0（macOS） |
| `tesseract`（可选） | 在 `PATH` 中 | `ddddocr` 不可用时的备用识别 | — |
| `lark-cli`（可选） | 飞书 CLI，npm 包 `@larksuite/cli`，需要 Node.js；已用 `lark-cli config init` 绑定飞书机器人 | 系统定时任务的飞书通知 | 1.0.98（macOS） |
| 网络 | 能访问 `jjz.jtgl.beijing.gov.cn`（443、2443）、`bjt.beijing.gov.cn`、`portal.bjt.beijing.gov.cn`、`ssp.jtgl.beijing.gov.cn`；飞书通知另需访问 `open.feishu.cn` | 查询、提交前校验、手机号登录、飞书通知 | — |

```sh
# macOS 以 Homebrew 为例；已有 Python 3.11+ 和 openssl 时可跳过
brew install python openssl@3
# Debian 12+ / Ubuntu 24.04+；更早的发行版自带 Python 低于 3.11，需另装
sudo apt install python3 python3-venv openssl tzdata

# 可选：图形验证码自动识别。用运行程序的同一个 python3 创建 .venv
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

程序始终用系统的 `python3` 运行（`python3 -m jjz`），会自动加载同版本 `.venv` 中的识别库；没有 `.venv` 或版本不一致时不加载，登录改为本人输入图形验证码。升级 Python 小版本后需要重建 `.venv`。运行测试不需要安装任何包。

## 快速开始

所有命令都在项目根目录运行。

```sh
python3 -m jjz init                              # 按示例补建 600 权限的 config.toml 和 secrets/login.toml，不覆盖已有文件
python3 -m jjz auth-set --login --method sms     # 手机号登录，核验目标车辆后保存业务凭证（也可用 --method password）
python3 -m jjz doctor                            # 离线检查配置和模板
python3 -m jjz status                            # 在线只读查询
```

1. 在 `config.toml` 填写本人车牌。要用密码自动登录时，在 `secrets/login.toml` 填写手机号和密码；只有登录命令和自动重新登录读取这个文件。示例配置默认开启凭证失效时的无人值守自动登录（`[api].auto_relogin = true`），用 `doctor` 确认 `auto_relogin: ready`；不保存密码时改为 `false`。两份示例分别是 [jjz/config.example.toml](jjz/config.example.toml) 和 [jjz/login.example.toml](jjz/login.example.toml)，真实文件已列入 `.gitignore`。
2. 按 App「进京信息」页填写申请模板：进京目的、目的地区县及详细地址、目的地坐标、是否已在京（在京填在京地址及坐标，不在京填进京道路及坐标）、提交时所在位置坐标。代码表写在模板注释里，坐标均为高德（GCJ-02）经纬度。外证用 `[application]`，内证用 `[inside_application]`，内证模板不填不影响外证。
3. 用 `auth-set --login` 登录并保存业务凭证；也可以按 [办理凭证获取](docs/login-usage.md) 从 App 查询请求中取得凭证，再运行 `auth-set` 隐藏输入。凭证保存为 600 权限的私有文件，不进入命令参数、提交记录或程序输出。
4. `doctor` 显示 `outside_application: ready`，并且 `status` 能查到本人车辆后，配置就完成了。

## 日常命令

所有命令都输出一行 JSON，车牌已脱敏。

| 命令 | 作用 | 会提交吗 |
| --- | --- | --- |
| `python3 -m jjz status` | 证件状态、有效期、`can_apply`、`inside_remaining` | 否 |
| `python3 -m jjz vehicles [--create]` | 账号下的车辆和对应配置；`--create` 为还没有配置的车生成配置 | 否 |
| `python3 -m jjz plan [--permit inside]` | 按规则给出续办计划 | 否 |
| `python3 -m jjz run [--permit inside]` | 到续办时间时跑完全部提交前校验，只预演 | 否 |
| `python3 -m jjz run --submit` | 校验通过后实际提交；还要求 `submission_enabled = true` | 是 |
| `python3 -m jjz history` | 提交记录 | 否 |
| `python3 -m jjz doctor` | 离线检查 | 否 |
| `python3 -m jjz login-check` | 只检查官方登录入口和证书，不需要账号 | 否 |
| `python3 -m jjz login --method sms\|password` | 手机号登录并核验目标车辆，不保存凭证 | 否 |
| `python3 -m jjz auth-set [--login --method sms\|password]` | 隐藏输入凭证，或登录核验后保存凭证 | 否 |

六环内证每年限 12 次，办理前先用 `status` 看剩余次数，再到 App 核对目的地。查询结果里没有要办类型的证件时，程序按类型切换处理：从六环外切到六环内，北京时间 12 点前申请当天生效，12 点后申请次日生效；从六环内切回六环外，等六环内证到期当天申请、次日生效，定时续办照常处理。能否办理和生效日以服务端可办标志和可选日期为准。切换后到 App 核对新证和原证的状态。

## 多辆车

一辆车一份配置，命令加 `--config` 指定，不加时使用 `config.toml`。先按"快速开始"配好 `config.toml` 这辆车；`auth-set --login` 保存凭证后，程序会为账号下其他还没有配置的车各生成一份配置，新文件名列在输出的 `configs_created` 里。已经保存过凭证时，直接运行：

```sh
python3 -m jjz vehicles            # 账号下的车辆、可办类型和对应的配置文件，只读
python3 -m jjz vehicles --create   # 为还没有配置的车生成配置，不改动已有文件
python3 -m jjz --config config-car2.toml doctor
```

- 新配置依次命名为 `config-car2.toml`、`config-car3.toml`，数据目录是 `data/car2`、`data/car3`。车牌写在文件里，输出只显示脱敏车牌。申请模板、`earliest_time`、凭证和登录设置照抄 `--config` 指定的配置（默认 `config.toml`），请按这辆车的实际情况核对。
- `[renewal].auto_renew` 决定这辆车怎么办理：`true` 是自动续办，系统定时任务和 AI 代理定时任务都处理这辆车；`false` 是按需办理，只在本人点名时查询和办理，`schedule` 也不为它生成定时任务。配置里没写时按 `true` 处理。新生成的配置 `auto_renew` 和 `submission_enabled` 都是 `false`，要自动续办时两项都改为 `true`，再生成定时任务。
- 每辆车只保留一份配置。这几份配置的提交记录互不相通，所以同一辆车有多份配置时，`vehicles` 退出码为 2 并把这些文件列在 `duplicates`，`run` 直接返回 `blocked`、不提交。账号里已经没有的车，其配置列在 `unmatched_configs`。官方每个账号最多绑定 3 辆车，程序不按这个数限制查询结果。
- 手动新建配置用 `python3 -m jjz --config config-<名称>.toml init`，名称只用英文字母、数字、`-` 和 `_`，数据目录自动设为 `data/<名称>`，`auto_renew` 和 `submission_enabled` 也都是 `false`。`init` 和 `schedule` 只接受 `config.toml` 和这种文件名，定时任务按文件名区分；`.gitignore` 忽略项目根目录下除 `pyproject.toml` 以外的全部 `.toml` 文件。提交记录、运行锁和定时任务日志都在各自的数据目录里；几份配置共用一个目录时，同时运行会互相跳过。
- 同一账号下的车共用 `secrets/authorization.txt` 和 `secrets/login.toml`，用其中任一份配置运行一次 `auth-set` 即可。凭证失效时，几份配置的自动重新登录依次进行：先登录的保存新凭证，后面的不再登录，直接用新凭证重新执行，输出 `relogin: shared`；后面的最多等 2 分钟，超时按自动重新登录失败处理。不同账号的车把 `api.auth_file`、`api.login_file` 指向各自的文件。
- 续办日按每辆车自己的证件到期日计算，每天最早几点办由各自的 `earliest_time` 决定。每辆车的定时任务也可以分别安排运行时间。

## 自动续办

定时任务有两种，选一种即可，都只办六环外证，并跳过按需办理（`auto_renew = false`）的车：

- **系统定时任务**：由 macOS launchd 或 Linux systemd 直接运行本程序，结果写日志，可选用飞书机器人通知（见「飞书通知」）。
- **AI 代理定时任务**：在 Claude 应用等支持定时任务的代理里新建任务，代理按 [AGENTS.md](AGENTS.md) 办理和检查，汇报结果，需要你处理时推送通知。运行时间自己定，建议到期当天 `earliest_time` 之后办理一次，稍晚再检查一次。

不管用哪种，实际提交都还要求配置里 `[renewal].submission_enabled = true`；开关没开时只做校验，返回 `blocked`。同一辆车只在一台机器上定时运行，提交记录不跨机器共享。

### 系统定时任务

有两种排法：

- **每天一次（默认）**：每天检查一次，到期当天自动续办，其余日子只返回 `wait`。
- **每周一次**：加 `--weekday`，在这天办理一次，1 小时后再运行一次核对结果，前一次没提交成功时顺带补办（`--check-after` 改间隔分钟数）。适合证件固定在每周同一天到期。

运行时间默认是 `earliest_time`（默认 08:00）后 10 分钟，`--time` 可改，不能早于 `earliest_time`。星期和时间以运行定时任务的主机本地时间为准。

**macOS**：`schedule` 生成调度文件，不自动加载。`--weekday` 是星期几（1 为周一，7 为周日）。任务名是 `local.jjz.renew.<配置文件名>`，每周排法的两次运行在同一个任务里。

```sh
python3 -m jjz schedule --submit                              # 每天 08:10；不带 --submit 只预演
python3 -m jjz schedule --submit --weekday 7 --time 08:10     # 或者：每周日 08:10 办理，09:10 检查
launchctl bootstrap "gui/$(id -u)" "$PWD/data/local.jjz.renew.config.plist"
launchctl list | grep local.jjz.renew                         # 已加载的任务
launchctl bootout "gui/$(id -u)/local.jjz.renew.config"       # 停止
tail -n 5 data/launchd.log                                    # 最近几次结果
```

调度文件和日志都在该配置的数据目录，`schedule` 输出的 `label`、`path` 就是任务名和文件路径；其他车辆把 `--config config-car2.toml` 写在 `schedule` 前面。`schedule` 不覆盖已有文件，改排法或时间时先 `bootout`、移走旧调度文件再重新生成；项目目录移动或 Python 升级小版本后也要这样重新生成。睡眠时错过的任务在唤醒后补跑一次。

**Linux**：用 systemd 用户定时器，实例名是配置文件名（不含 `.toml`）。按下面内容创建两个文件，把 `/path/to/jjz-renewer` 换成项目目录，`/usr/bin/python3` 换成 `command -v python3` 的结果。改成每周排法时，把 `OnCalendar` 换成两行，如 `Sun *-*-* 08:10:00` 和 `Sun *-*-* 09:10:00`。

```ini
# ~/.config/systemd/user/jjz-renew@.service
[Unit]
Description=jjz outside renewal (%i)

[Service]
Type=oneshot
WorkingDirectory=/path/to/jjz-renewer
ExecStart=/usr/bin/python3 -m jjz --config /path/to/jjz-renewer/%i.toml run --scheduled --permit outside --submit
UMask=0077
Environment=PYTHONUNBUFFERED=1 PYTHONUTF8=1
```

```ini
# ~/.config/systemd/user/jjz-renew@.timer
[Unit]
Description=Daily jjz outside renewal (%i)

[Timer]
# 每天 08:10；关机时错过的开机后补跑
OnCalendar=*-*-* 08:10:00
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now jjz-renew@config.timer          # 其他车辆如 jjz-renew@config-car2.timer
systemctl --user list-timers 'jjz-renew*'                     # 已启用的定时器和下次运行时间
journalctl --user -u jjz-renew@config.service -n 5 -o cat     # 最近几次结果
loginctl enable-linger "$USER"                                # 用户未登录时也运行
```

平时也可以直接让 Codex 或 Claude Code 在本项目里"看下进京证""续一下"。

## 飞书通知

系统定时任务可以选用飞书机器人通知结果。任务带 `--notify` 时，只要结果需要你处理（可办理、已提交、被拦下、结果不明、被拒绝或出错），就由本机的飞书 CLI（[`lark-cli`](https://www.npmjs.com/package/@larksuite/cli)）以机器人身份私信你一张卡片；未到续办日等 `wait` 不发。卡片版式和 AI 代理定时任务发的一样，规范见项目技能 [jjz-feishu-card](.agents/skills/jjz-feishu-card/SKILL.md)。

1. 安装 Node.js 和飞书 CLI：`npm install -g @larksuite/cli@1.0.98`。
2. 运行 `lark-cli config init`，按提示在浏览器里创建或绑定一个飞书自建应用。应用要开启机器人能力、有以应用身份发消息的权限（`im:message:send_as_bot`），可用范围包含你本人。
3. 查自己的 open_id：运行 `lark-cli auth login` 并按提示授权，再运行 `lark-cli contact +get-user --as user`，输出里 `ou_` 开头的 `open_id` 就是。
4. 发一条测试消息，确认机器人能私信到你：`lark-cli im +messages-send --as bot --user-id ou_xxx --text "进京证通知测试"`。
5. 把 open_id 填进配置的 `[notify]`：`feishu_open_id = "ou_xxx"`。open_id 只放在本机配置里，不要写进仓库。
6. 生成定时任务时加 `--notify`，如 `python3 -m jjz schedule --submit --notify`，`schedule` 会把 lark-cli 和 Node.js 所在目录写进任务的 `PATH`。Linux 在 `ExecStart` 末尾加 `--notify`；lark-cli 不在 `/usr/bin` 这类默认目录时，再加一行 `Environment=PATH=<lark-cli 和 node 所在目录>:/usr/bin:/bin`。

日志里每次运行的结果带 `notify`：`sent` 是卡片已发出，`sent_text` 是卡片失败、改发了文字，`failed` 是没发出去，原因见 `notify_reason`。通知失败不影响办理结果和退出码。卡片只含程序输出的脱敏信息。AI 代理定时任务自己发通知，运行的命令不带 `--notify`，以免重复。

## 提交流程与保护

提交内容按北京交警当前的 H5 申请页组装：车辆字段取自查询结果，驾驶人信息取自官方驾驶人接口，再合并申请模板。提交前依次调用官方的车辆校验、人数校验、可选日期查询和行驶路线检查。遇到以下情况只返回 `blocked`，不写提交记录：

- 服务端显示该类型当前不可办；
- 计划日期不在服务端给出的可选日期内；
- 服务端要求填写行驶路线，或已开启同行人登记；
- 任一校验被拒；
- 状态未知或记录冲突。

程序先把提交意图写入 SQLite，再发出一次申请。接口受理、服务端已确认、审核通过分别输出 `accepted`、`verified`、`approved`，最终都应到 App 核对证件和有效期。网络超时或程序中断时输出 `uncertain`，这时保留提交保护，之后每次先查询核验，不会直接重发。

服务端查到对应申请已取消或审核失败时，程序返回 `rejected`。长时间停机后，只有服务端返回同一类型、同一生效日的证件，且完整有效期确已结束，才会把旧提交记录保留为 `expired`，随后按当前状态和全部服务端校验重新评估续办。记录缺失、日期不完整或状态矛盾时仍保留保护；已确认过期的记录也不能通过 `resolve` 删除。

提交前校验结束后会重新检查北京时间和续办计划。跨日期或计划变化时返回 `blocked`，本次不写提交记录、不提交；下一次运行必须重新查询并完成校验。

```sh
python3 -m jjz history
# 仅在本人到 App 核实确实没有提交后使用，不能解除服务端已确认的申请
python3 -m jjz resolve --attempt '<history 中的 key>' --confirmed-not-submitted
```

不要删除数据目录来"重试"，那样会丢掉保护记录。

## 策略依据与限制

- [官方类型说明](https://jtgl.beijing.gov.cn/jgj/qaknowledge/325750668/325750929/744063797/index.html)：六环内证最长 7 天，载客汽车每年每车最多 12 次；六环外证每次 7 天，不限次数。资格和额度最终以服务端为准。
- [官方接续说明](https://jtgl.beijing.gov.cn/jgj/qaknowledge/325750668/325750929/744063862/index.html)：有效内证须在有效期最后一天提交接续申请；内证生效时外证作废。遇到审核中或待生效的申请时程序等待；内证仍有效、又没到最后一天时，不会自动切换到外证。
- [交管部门转载的办理时点说明](https://www.bjmtg.gov.cn/bjmtg/c104458/202310/98d1fd37fdc245e39c028c16cab0437f.shtml)：内证每天 12 时后申请，次日生效。程序在网络查询之后重新读取北京时间，避免查询跨过中午或午夜时还用旧时间。
- 外证默认在到期当天北京时间 08:00 后申请次日生效，提交前还要确认这一天在服务端可选日期内。`outside_lead_days = 1` 只在本人确认允许提前办理时使用。
- 官方车辆管理页按站点配置限定每个账号最多绑定 3 辆车。一份配置只处理 `vehicle.plate` 这一辆车，账号下其他车辆不影响它的查询和办理；这个上限可能调整，程序不据此拦截。
- 计划输出不能当作通行资格证明。

## 项目结构

```
jjz/              程序（api、config、domain、runtime、storage、credentials、auth、captcha_ocr、feishu、cli）
                  及示例配置 config.example.toml、login.example.toml
tests/            离线测试：python3 -m unittest discover -s tests
docs/             凭证获取与登录验证范围、参考研究
examples/         演示用响应样本（演示车牌与示例配置一致）：
                  python3 -m jjz --config jjz/config.example.toml plan --fixture examples/state.json --at 2026-10-25T09:00:00+08:00
requirements.txt  可选的验证码识别依赖（核心无第三方依赖），文末注明可选的飞书 CLI
.agents/skills/   代理技能：jjz-feishu-card 飞书卡片规范（.claude/skills 链接到这里）
AGENTS.md         代理操作说明（CLAUDE.md 引用同一份）
```

项目根目录下的 `.toml` 配置（`pyproject.toml` 除外）、`secrets/`、`data/` 是私有文件，`.venv/` 是虚拟环境，都已列入 `.gitignore`。

## 参考与致谢

感谢 [bihongfly/jinjingzheng](https://github.com/bihongfly/jinjingzheng)。本项目参考了它 2026-01-12 的 main 提交 [`70ea970`](https://github.com/bihongfly/jinjingzheng/commit/70ea97006b66c541733222ff0bb4d6229e08b97b)，用到的内容包括：

- 证件查询和提交两个接口的路径，以及请求头 `Authorization` 的用法；
- 申请体字段的最初线索；
- 作者在 [issue #13](https://github.com/bihongfly/jinjingzheng/issues/13#issuecomment-3722585683) 中关于凭证来源的说明：续办用的是办理请求里的 `Authorization`，不是京通登录 token。

本项目没有复制该项目的源码。之后又对照北京交警当前的官方 H5 页面，重新整理了提交字段和提交前校验，详见 [研究记录](docs/reference-notes.md)。该项目的 README 对商用、转载等有自定义限制；本仓库的许可只适用于本仓库自己的代码和文档。

感谢 [sml2h3/ddddocr](https://github.com/sml2h3/ddddocr)。图形验证码的自动识别使用这个库（MIT 许可），它作为可选依赖按 [requirements.txt](requirements.txt) 单独安装，本仓库不包含其代码。

## 许可

[PolyForm Noncommercial License 1.0.0](LICENSE)：可以免费用于个人、学习研究、非营利组织和政府机构等非商业用途，可以修改和再分发，但须附上许可证全文及其中的 `Required Notice` 署名行；**禁止任何商业用途**。

本项目与北京市公安局交通管理局无关，也不是官方客户端。使用者须用本人账号和真实信息办理，并自行承担使用后果；资格、额度和审核结果以官方为准。
