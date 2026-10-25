# 进京证定期续办

在 macOS 上运行的 Python 命令行工具：查询北京进京证状态，按官方规则自动续办六环外证，六环内证按需手动办理。核心功能只用标准库。可以由 macOS 定时任务运行，也可以由 Codex、Claude Code 等代理按 [AGENTS.md](AGENTS.md) 的流程代为操作。本项目参考了 [bihongfly/jinjingzheng](https://github.com/bihongfly/jinjingzheng)，代码为独立实现，见下文"参考与致谢"。

## 功能

- 查询证件状态和有效期、服务端当前是否可办（`can_apply`）、六环内证剩余次数（`inside_remaining`）。
- 按官方接续规则计算续办时间；提交前依次调用全部官方校验接口，校验全部通过才提交。
- 先把提交意图写入 SQLite 再提交；超时、中断或结果不明时只查询核验，不会盲目重发。
- 业务凭证有两种取得方式：在终端用手机号（密码或短信）登录，核验目标车辆后保存；或从北京交警 App 的查询请求中取得后手动保存。
- 图形验证码可由 `ddddocr` 在进程内自动识别，识别失败时再由本人输入。
- 凭证到期后，下次办理时自动登录续期（开启 `auto_relogin`，用密码和本机验证码识别）；程序只输出结果，通知由定时代理任务负责发送。登录各环节的验证情况见 [验证范围](docs/login-usage.md#验证范围与故障处理)。

## 运行环境

| 依赖 | 要求 | 用途 | 已测试版本 |
| --- | --- | --- | --- |
| macOS | 开机、用户已登录并联网；睡眠时定时任务不运行 | 进程锁（`fcntl`）、launchd 定时任务、用默认浏览器打开验证码图片 | — |
| Python | 3.11 及以上（标准库自带 `tomllib`） | 运行程序，核心功能不需要第三方包 | 3.14 |
| `openssl` 命令 | 在 `PATH` 中，支持 `pkey`、`pkeyutl` | 手机号登录时按官方协议做 RSA 加密 | OpenSSL 3.6；macOS 自带的 LibreSSL 3.3 |
| `ddddocr`、Pillow（可选） | 按 [requirements.txt](requirements.txt) 装进项目 `.venv`，`.venv` 须与运行程序的解释器是同一个 Python 小版本 | 自动识别图形验证码 | ddddocr 1.6.1、Pillow 13.0 |
| `tesseract`（可选） | 在 `PATH` 中 | `ddddocr` 不可用时的备用识别 | — |
| 网络 | 能访问 `jjz.jtgl.beijing.gov.cn`（443、2443）、`bjt.beijing.gov.cn`、`portal.bjt.beijing.gov.cn`、`ssp.jtgl.beijing.gov.cn` | 查询、提交前校验、手机号登录 | — |

```sh
# 以 Homebrew 为例；已有 Python 3.11+ 和 openssl 时可跳过
brew install python openssl@3

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

1. 在 `config.toml` 填写本人车牌。要用密码自动登录时，在 `secrets/login.toml` 填写手机号和密码；只有登录命令和自动重新登录读取这个文件。希望凭证失效时无人值守自动登录，再把 `config.toml` 的 `[api].auto_relogin` 设为 `true`，用 `doctor` 确认 `auto_relogin: ready`。两份示例分别是 [jjz/config.example.toml](jjz/config.example.toml) 和 [jjz/login.example.toml](jjz/login.example.toml)，真实文件已列入 `.gitignore`。
2. 按 App「进京信息」页填写申请模板：进京目的、目的地区县及详细地址、目的地坐标、是否已在京（在京填在京地址及坐标，不在京填进京道路及坐标）、提交时所在位置坐标。代码表写在模板注释里，坐标均为高德（GCJ-02）经纬度。外证用 `[application]`，内证用 `[inside_application]`，内证模板不填不影响外证。
3. 用 `auth-set --login` 登录并保存业务凭证；也可以按 [办理凭证获取](docs/login-usage.md) 从 App 查询请求中取得凭证，再运行 `auth-set` 隐藏输入。凭证保存为 600 权限的私有文件，不进入命令参数、提交记录或程序输出。
4. `doctor` 显示 `outside_application: ready`，并且 `status` 能查到本人车辆后，配置就完成了。

## 日常命令

所有命令都输出一行 JSON，车牌已脱敏。

| 命令 | 作用 | 会提交吗 |
| --- | --- | --- |
| `python3 -m jjz status` | 证件状态、有效期、`can_apply`、`inside_remaining` | 否 |
| `python3 -m jjz plan [--permit inside]` | 按规则给出续办计划 | 否 |
| `python3 -m jjz run [--permit inside]` | 到续办时间时跑完全部提交前校验，只预演 | 否 |
| `python3 -m jjz run --submit` | 校验通过后实际提交；还要求 `submission_enabled = true` | 是 |
| `python3 -m jjz history` | 提交记录 | 否 |
| `python3 -m jjz doctor` | 离线检查 | 否 |
| `python3 -m jjz login-check` | 只检查官方登录入口和证书，不需要账号 | 否 |
| `python3 -m jjz login --method sms\|password` | 手机号登录并核验目标车辆，不保存凭证 | 否 |
| `python3 -m jjz auth-set [--login --method sms\|password]` | 隐藏输入凭证，或登录核验后保存凭证 | 否 |

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

调度文件只生成一次，重新生成前要先移走旧文件。文件里写的是配置、解释器和项目目录的绝对路径，不含凭证；项目或解释器移动后要重新生成。电脑睡眠时不会运行，所以不能保证有效期连续不断。输出追加在 `data/launchd.log`。

**代理代办**：让 Codex 或 Claude Code 在本项目里"看下进京证"或"续一下"，代理会按 [AGENTS.md](AGENTS.md) 的流程查询、预演并报告。只有开关已打开、校验全部通过时，代理才会提交；凭证失效且自动重新登录未开启或未成功时，会请你重新登录。也可以在 Claude 应用中配置定时代理任务，按 AGENTS.md 的"定时代理检查"无人值守运行。定时任务和代理可以同时使用：进程锁和提交记录保证不会重复提交。

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
- 计划输出不能当作通行资格证明。

## 项目结构

```
jjz/              程序（api、config、domain、runtime、storage、credentials、auth、captcha_ocr、cli）
                  及示例配置 config.example.toml、login.example.toml
tests/            离线测试：python3 -m unittest discover -s tests
docs/             凭证获取与登录验证范围、参考研究
examples/         演示用响应样本（演示车牌与示例配置一致）：
                  python3 -m jjz --config jjz/config.example.toml plan --fixture examples/state.json --at 2026-10-25T09:00:00+08:00
requirements.txt  可选的验证码识别依赖（核心无第三方依赖）
AGENTS.md         代理操作说明（CLAUDE.md 引用同一份）
```

`config.toml`、`secrets/`、`data/` 是私有文件，`.venv/` 是虚拟环境，都已列入 `.gitignore`。

## 参考与致谢

感谢 [bihongfly/jinjingzheng](https://github.com/bihongfly/jinjingzheng)。本项目参考了它 2026-01-12 的 main 提交 [`70ea970`](https://github.com/bihongfly/jinjingzheng/commit/70ea97006b66c541733222ff0bb4d6229e08b97b)，用到的内容包括：

- 证件查询和提交两个接口的路径，以及请求头 `Authorization` 的用法；
- 申请体字段的最初线索；
- 作者在 [issue #13](https://github.com/bihongfly/jinjingzheng/issues/13#issuecomment-3722585683) 中关于凭证来源的说明：续办用的是办理请求里的 `Authorization`，不是京通登录 token。

本项目没有复制该项目的源码。之后又对照北京交警当前的官方 H5 页面，重新整理了提交字段和提交前校验，详见 [研究记录](docs/reference-notes.md)。该项目的 README 对商用、转载等有自定义限制；本仓库的许可只适用于本仓库自己的代码和文档。

感谢 [sml2h3/ddddocr](https://github.com/sml2h3/ddddocr)。图形验证码的自动识别使用这个库（MIT 许可），它作为可选依赖按 [requirements.txt](requirements.txt) 单独安装，本仓库不包含其代码。

## 许可

[PolyForm Noncommercial License 1.0.0](LICENSE)：可以免费用于个人、学习研究、非营利组织和政府机构等非商业用途，可以修改和再分发，但须附上许可证全文及其中的 `Required Notice` 署名行；**禁止任何商业用途**。商业使用请先联系作者另行授权。

本项目与北京市公安局交通管理局无关，也不是官方客户端。使用者须用本人账号和真实信息办理，并自行承担使用后果；资格、额度和审核结果以官方为准。
