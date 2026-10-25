# 进京证定期续办

在这台 Mac 上运行的 Python 3.11+ 命令行程序，只用标准库。日常自动续六环外证，六环内证按需手动指定。可以由 macOS 定时任务运行，也可以由 Codex、Claude Code 等代理按 [AGENTS.md](AGENTS.md) 的流程代为操作。程序为独立实现，参考研究见 [研究记录](docs/reference-notes.md)。

## 当前状态

- 已接入本人账号：查询接口和全部提交前校验接口都已用本人凭证只读核验。
- `submission_enabled` 已开启。自动续办由 Claude 应用的两个定时代理任务按 [AGENTS.md](AGENTS.md) 执行：`jjz-renewal-check` 每天 08:30 左右检查，校验全部通过时提交；`jjz-status-check` 每天 09:20 左右复查办理状态，也作为当天的重试。macOS 定时任务未加载。程序还没有实际提交过申请。
- 办理凭证从北京交警 App 的查询请求中取得，有效期未知。程序没有自动登录、凭证刷新和通知推送。

## 首次配置

```sh
python3 -m jjz init          # 生成权限为 600 的 config.toml，不覆盖已有文件
python3 -m jjz auth-set      # 在本机终端隐藏输入 Authorization
python3 -m jjz doctor        # 离线检查配置和模板
python3 -m jjz status        # 在线只读查询
```

1. 在 `config.toml` 填写本人车牌。
2. 按 App「进京信息」页填写申请模板：进京目的、目的地区县及详细地址、目的地坐标、是否已在京（在京填在京地址及坐标，不在京填进京道路及坐标）、提交时所在位置坐标。代码表写在模板注释里，坐标均为高德（GCJ-02）经纬度。外证用 `[application]`，内证用 `[inside_application]`，内证模板不填不影响外证。
3. 按 [办理凭证获取](docs/login-usage.md) 从 App 取得 Authorization，再运行 `auth-set`。凭证保存为本地 600 文件，不进入命令参数、提交记录或程序输出。
4. `doctor` 显示 `outside_application: ready`，并且 `status` 能查到本人车辆后，配置就完成了。

## 日常命令

所有命令都在项目根目录运行，输出一行 JSON，车牌已脱敏。

| 命令 | 作用 | 会提交吗 |
| --- | --- | --- |
| `python3 -m jjz status` | 证件状态、有效期、服务端当前是否可办（`can_apply`）、内证剩余次数（`inside_remaining`） | 否 |
| `python3 -m jjz plan [--permit inside]` | 按规则给出续办计划 | 否 |
| `python3 -m jjz run [--permit inside]` | 到续办时间时跑完全部提交前校验，只预演 | 否 |
| `python3 -m jjz run --submit` | 校验通过后实际提交；还要求 `submission_enabled = true` | 是 |
| `python3 -m jjz history` | 本地提交记录 | 否 |
| `python3 -m jjz doctor` | 离线检查 | 否 |

六环内证每年限 12 次，办理前先用 `status` 看剩余次数，再到 App 核对目的地。

## 自动续办

**macOS 定时任务**：每 30 分钟运行一次，固定只处理六环外。

```sh
python3 -m jjz schedule --submit             # 去掉 --submit 则只预演
plutil -lint data/local.jjz.renew.plist
launchctl bootstrap "gui/$(id -u)" "$PWD/data/local.jjz.renew.plist"
launchctl print "gui/$(id -u)/local.jjz.renew"
launchctl bootout "gui/$(id -u)/local.jjz.renew"   # 停止
```

调度文件只生成一次，重新生成前要先移走旧文件。文件里写的是配置、解释器和项目目录的绝对路径，不含凭证。Mac 需要开机、用户登录并联网；睡眠时不会运行，所以不能保证有效期连续不断。项目或解释器移动后要重新生成。输出追加在 `data/launchd.log`。

**代理代办**：让 Codex 或 Claude Code 在本项目里"看下进京证"或"续一下"，代理会按 [AGENTS.md](AGENTS.md) 的流程查询、预演并报告。只有开关已打开、校验全部通过时，代理才会提交；凭证失效时会请你重新取得。定时任务和代理可以同时使用：进程锁和提交记录保证不会重复提交。

## 提交流程与保护

提交内容按北京交警当前的 H5 申请页组装：车辆字段取自查询结果，驾驶人信息取自官方驾驶人接口，再合并申请模板。提交前依次调用官方的车辆校验、人数校验、可选日期查询和行驶路线检查。遇到以下情况只返回 `blocked`，不写提交记录：

- 服务端显示该类型当前不可办；
- 计划日期不在服务端给出的可选日期内；
- 服务端要求填写行驶路线，或已开启同行人登记；
- 任一校验被拒；
- 状态未知或记录冲突。

程序先把提交意图写入 SQLite，再发出一次申请。接口受理、服务端已确认、审核通过分别输出 `accepted`、`verified`、`approved`，最终都应到 App 核对证件和有效期。网络超时或程序中断时输出 `uncertain`，这时保留提交保护，之后每次先查询核验，不会直接重发。

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
- 计划输出不能当作通行资格证明。

## 项目结构

```
jjz/        程序（api、config、domain、runtime、storage、credentials、auth、cli）
tests/      离线测试：python3 -m unittest discover -s tests
docs/       凭证获取、登录评估、参考研究
examples/   演示用响应样本（演示车牌与示例配置一致）：
            python3 -m jjz --config jjz/config.example.toml plan --fixture examples/state.json --at 2026-10-25T09:00:00+08:00
AGENTS.md   代理操作说明（CLAUDE.md 引用同一份）
```

`config.toml`、`secrets/`、`data/` 是本机私有文件，已列入 `.gitignore`。
