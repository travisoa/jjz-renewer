# 办理凭证获取与核验

续办使用的是进京证业务 `Authorization`。京通统一认证票据、北京交警 App 的 SSP 登录态和业务凭证分别处理，只有目标车辆查询成功后才认定业务登录已验证。

## 手机号登录

在项目根目录运行，要求 Python 3.11+ 和可用的 `openssl` 命令，详见 [README 运行环境](../README.md#运行环境)。先在 `config.toml` 填好本人车牌，并核对 `api.base_url` 为官方进京证服务源。业务交换使用 `https://jjz.jtgl.beijing.gov.cn:2443`；车辆查询使用配置中的官方 443 或 2443 服务源。

```sh
# 公开入口和 TLS 预检，不需要账号，也不发送短信
python3 -m jjz login-check

# 本人交互登录并只读查询目标车辆，凭证仅保留在内存
python3 -m jjz login --method sms
python3 -m jjz login --method password

# 本人交互登录、核对目标车辆后直接保存业务凭证
python3 -m jjz auth-set --login --method sms
python3 -m jjz auth-set --login --method password
```

需要供续办使用时，选择一条 `auth-set --login` 命令即可。只希望验证登录时，选择 `login` 命令。使用本项目即视为同意官方登录协议，程序不再询问 `yes`。手机号和密码写在私有文件 `secrets/login.toml` 中，只有 `login` 和 `auth-set --login` 会读取它，`status`、`run` 和定时任务都不加载；没有该文件或两项留空时，在终端隐藏输入。图形验证码由 OCR 在进程内识别并填入，识别失败或连续三次未通过时再由本人输入。短信验证码仍由本人输入。这些值都不通过命令参数传入。

操作流程：

1. 程序检查官方统一认证入口、新版 SSP 回调和进京证业务页的 HTTPS 状态。
2. 程序显示官方登录协议地址，按已阅读并同意直接继续。`secrets/login.toml` 已填写 `phone` 和 `password` 时密码登录自动使用这两项；未配置时在终端隐藏输入手机号和密码。
3. 程序获取图形验证码后，优先用已安装的 `ddddocr` 识别，没有该库时再尝试 `tesseract`。得到 4 到 6 位字母或数字就直接填入。未识别、或官方连续三次拒绝时，才打开图片并由本人输入。短信方式在图形验证码通过后只发送一次登录短信，再由本人输入收到的验证码。
4. 程序沿官方实际返回的回调处理业务票据，取得业务 `Authorization` 后，只读查询配置中的目标车辆。
5. 核对成功输出 `action: business_login_verified`、`business_auth_verified: true`。`login` 的 `credential_saved` 为 `false`；`auth-set --login` 在核验成功后直接以 600 权限原子保存，`credential_saved` 为 `true`。

`python3 -m jjz init` 会按 [jjz/login.example.toml](../jjz/login.example.toml) 创建 600 权限的 `secrets/login.toml`；手动创建时须 `chmod 600 secrets/login.toml`，否则程序拒绝读取。`config.toml` 中不能再有 `[login]` 节，否则所有命令都会报错停止。路径可由 `config.toml` 的 `api.login_file` 修改。格式如下：

```toml
[login]
phone = ""
password = ""
```

保存之前不读取或复制已有凭证文件。证书失败、实名认证等级不足、未知落地页、缺失票据、交换被拒或车辆不匹配时，程序停止并保留已有凭证。密码、验证码、统一认证 Cookie 和跳转票据不进入命令参数、日志或最终 JSON；验证码图片使用私有临时目录，用后清理。代理不得读取 `secrets/` 下的文件，也不得代填验证码。

保存完成后，可运行：

```sh
python3 -m jjz status
python3 -m jjz plan
```

登录命令不提交办理申请、不改变提交开关，也不加载定时任务。配置中的手机号和密码，以及图形验证码识别，只用于本人在场的这次交互登录。识别库按 [requirements.txt](../requirements.txt) 安装在与运行解释器同版本的项目 `.venv` 中，`python3 -m jjz` 会自动使用这份安装。没有该环境或版本不一致时改为本人输入。程序不绕过实名验证，不自动重试已发出的短信，也没有无人值守重新登录或凭证刷新。

## 手动输入凭证

手机号登录不可用时（例如官方登录流程变化），可以由本人从北京交警 App 进京证查询请求（`stateList`）的 `Authorization` 请求头取得业务凭证，再在终端运行：

```sh
python3 -m jjz auth-set
python3 -m jjz status
```

`auth-set` 隐藏输入凭证并以 600 权限原子保存。凭证只留在本人设备上，不发到聊天、代码仓库或其他人。

## 验证范围与故障处理

手机号登录的在线验证情况只记录在这里，其他文档链接到本节。

| 环节 | 状态 |
| --- | --- |
| 公开入口、SSP 回调和业务页 TLS 预检 | 2026-10-25 在保持完整证书验证的条件下通过 |
| 密码登录全流程 | 2026-10-25 本人在线验证一次：读取 `secrets/login.toml`、OCR 识别图形验证码、统一认证、回调票据选择、业务交换和目标车辆只读查询均通过 |
| `auth-set --login` 保存 | 2026-10-25 保存的凭证经 `status` 只读查询可用 |
| 短信登录全流程 | 2026-10-25 本人在线验证一次：读取 `secrets/login.toml` 中的手机号、OCR 识别图形验证码、发送一次登录短信、本人输入短信验证码、统一认证、业务交换和目标车辆只读查询均通过 |
| 凭证有效期、刷新和无人值守重新登录 | 有效期未实测；没有经验证的刷新协议，未实现无人值守重新登录 |

真实流程若出现程序未识别的回调页、额外验证或新的响应结构，应停止并核对该次官方流程，不将通用登录 token 直接当作业务凭证。实名核验需本人在官方 App 完成。`status` 提示凭证失效时，由本人重新运行 `auth-set --login` 或从本人 App 查询请求取得凭证后运行 `auth-set`。
