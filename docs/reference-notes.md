# 参考项目研究记录

研究日期：2026-10-25。参考项目：[bihongfly/jinjingzheng](https://github.com/bihongfly/jinjingzheng)。本记录依据远端公开内容整理，不保存或执行其源代码。

## 已确认的技术路线

- 参考的是 `main` 分支提交 [`70ea97006b66c541733222ff0bb4d6229e08b97b`](https://github.com/bihongfly/jinjingzheng/commit/70ea97006b66c541733222ff0bb4d6229e08b97b)（2026-01-12）。
- [jinjingzheng.py](https://github.com/bihongfly/jinjingzheng/blob/70ea97006b66c541733222ff0bb4d6229e08b97b/jinjingzheng.py) 是使用 `requests` 的单文件 HTTP 客户端，不依赖 Android、模拟器或 ADB。
- [README](https://github.com/bihongfly/jinjingzheng/blob/70ea97006b66c541733222ff0bb4d6229e08b97b/README.md) 建议由长期运行的机器或青龙等工具定时调用；脚本自身没有调度器、自动登录或凭证刷新功能。

## 公开协议线索

源码隐藏了服务主机和 Authorization 值，仅公开以下 JSON POST 路径；这些是第三方脚本提供的线索，不是官方公开 API 承诺。

| 用途 | 路径 |
| --- | --- |
| 查询办理记录 | `/pro/applyRecordController/stateList` |
| 提交办理申请 | `/pro/applyRecordController/insertApplyRecord` |

请求使用 `Authorization` 和 `Content-Type: application/json` 请求头。查询请求体为 `{}`，脚本以业务字段 `code == 200` 判断调用成功。

查询响应的车辆数组为 `data.bzclxx`。脚本仅选择第一辆车，优先取其 `ecbzxx[0]`，否则取 `bzxx[0]`；不能据此认定数组已按时间排序，也不能认定第一条就是目标车辆或最新证件。使用到的记录字段包括 `applyId`、`vId`、`hpzl`、`hphm`、`jsrxm`、`jszh`、`blztmc`、`yxqs`、`yxqz`、`jjzzlmc`、`sqsj`。

申请体使用原记录提供的 `applyIdOld`、`vId`、`hpzl`、`hphm`、`jsrxm`、`jszh`，另传地址和经纬度（`xxdz`、`zjxxdz`、`sqdzgdjd`、`sqdzgdwd`、`sqdzbdjd`、`sqdzbdwd`）、是否在京（`sfzj`）、同行人（`txrxx`）、目的地区及事由（`jjdq`、`jjmd`、`jjmdmc`）、道路（`jjlk`、`jjlkmc`）、证件类型（`jjzzl`）和申请生效日期（`jjrq`）。源码将 `jjzzl` 的 `01` 映射为六环内、`02` 映射为六环外，默认六环外。真实业务数据应由本人确认，不能沿用作者示例地址或默认事由。

## 与当前官方 H5 的差异

2026-10-25 对照北京交警当前的进京证 H5（`https://jjz.jtgl.beijing.gov.cn:2443/H5_NewUC/`）公开脚本，并用本人凭证只读调用相关校验接口后确认：

- 申请体不再带 `applyIdOld`，也不发百度坐标 `sqdzbdjd`/`sqdzbdwd`。
- 车辆字段取自查询结果里的车辆对象：`vId`、`hphm`、`hpzl`、`ylzsfkb`、`elzsfkb`、`ylzmc`、`elzmc`、`ylzqyms`、`elzqyms`、`cllx`。其中 `elzsfkb`/`ylzsfkb` 是服务端给出的"六环外/六环内当前可办"标志，选证页（`pagesSqbz/certype`）只据此决定能否选择，不要求同类型的历史证件，所以两类证可以直接互相切换；不可办时显示 `elzqyms`/`ylzqyms` 中的原因。2026-10-26 实测：持有生效中的六环外证时办理六环内证，审核通过后查询结果只返回六环内证，不再返回原六环外证。
- 驾驶人信息取自 `getJsrxx`（`jsrxm`、`jszh`、`dabh`），不是取自证件记录。
- 驾驶人页（`pagesSqbz-sqbz`）有「港澳台及外籍人士」选项，对应字段 `rylx`（选中为 `"1"`，否则 `"0"`）。页面按 `getJsrxx` 返回的 `rylx` 预选，`applyCheckNum` 和提交请求体都带上它；缺少该字段时 `applyCheckNum` 返回业务错误（2026-11-01 核对）。程序固定按非港澳台及外籍人士发送 `"0"`。
- 页面提交目的地区县名 `area`、目的地坐标 `jjdzgdjd`/`jjdzgdwd`，以及 `jingState`（固定为空串）。`sfzj=1` 时发在京地址 `zjxxdz` 及坐标，并清空道路字段；`sfzj=0` 时发道路 `jjlk`、`jjlkmc` 及坐标 `jjlkgdjd`/`jjlkgdwd`，并清空在京地址字段。
- `sqdzgdjd`/`sqdzgdwd` 是提交时手机的当前定位，不是目的地坐标。`jjmd`/`jjmdmc` 可多选，用逗号拼接。
- 提交前依次调用：`applyVehicleCheck`（`data` 为 `"200"` 才继续）、`getJsrxx`、`getConfigRecordInfo`（取同行人开关 `txrkg`）、`applyCheckNum`（驾驶人、`rylx`、同行人）、`checkHandle`（返回可选进京日期 `jjrqs`，格式 `YYYY-MM-DD`）、`checkInputRoadInfo`（`data` 不为 `"0"` 时要求填写行驶路线 `xslx`）。
- 每个账号最多绑定 3 辆车（2026-10-26 核对）：车辆管理页（`pagesClgl-list`）从 `/vehicleController/getUserIdInfo` 读取已绑定车辆，按站点配置 `yhbdsx` 禁用"添加车辆"，已绑定 3 辆时提示无法继续添加。程序不调用该接口；上限来自站点配置，可能调整，所以程序不按它限制 `stateList` 的车辆数组 `bzclxx`。
- 区县、进京目的、进京道路的代码来自 `/pro/ucDicController/queryDic`，`type` 分别为 `qjxz`、`jjmd`、`jjlk`（`jjlk` 需带 `vId`）。

## AUTH 来源与有效期

当前手机号登录按以下官方公开资料实现：

- [H5 生产配置脚本](https://jjz.jtgl.beijing.gov.cn:2443/H5_NewUC/static/js/index.f33133eb.js)的配置模块实际选择 `prod_new`。统一认证使用 `client_id=100100001423`、回调 `https://ssp.jtgl.beijing.gov.cn/sso/callback/userauth`、`state=300001`；业务和认证根路径分别为 `https://jjz.jtgl.beijing.gov.cn:2443/pro/`、`https://jjz.jtgl.beijing.gov.cn:2443/auth/`。
- [用户分中心落地页](https://jjz.jtgl.beijing.gov.cn:2443/login_h5_byUserCenter.html)从实际 URL 读取唯一 `token` 和 `state`，调用 `POST /auth/userController/loginUserByUserCenter?state=…`，请求体为 JSON `{token, state}`。它将成功响应交给 `/H5_NewUC/`。
- [通用落地页](https://jjz.jtgl.beijing.gov.cn:2443/login_h5.html)使用相同的票据字段，但调用 `POST /auth/userController/loginUser?state=…`。程序只按官方实际返回的落地页选择接口，不猜测或混用两类票据。
- 两份页面均通过 `Content-Type: application/json` 发送票据，并设置 `source`。只有 `state` 属于页面列出的渠道码时才取其原值，否则使用页面声明的 H5 默认渠道；当前生产 `state=300001` 不属于该渠道码表，因此走 H5 默认渠道。URL 中的票据按 `decodeURIComponent` 语义解析，保留原样 `+`。缺失 `state` 才使用页面默认值 `101000004071`。
- [业务首页脚本](https://jjz.jtgl.beijing.gov.cn:2443/H5_NewUC/static/js/pages-bzzx-bzzx.f2a22f75.js)解码交换响应，取 `data.accessToken` 作为后续请求头 `Authorization`。认证 URL 中的 token、SSP 自身的登录态和该业务凭证不能互换。
- 程序以新业务凭证只读查询配置目标车辆后，才输出 `business_login_verified`。`login` 只把凭证保留在内存；`auth-set --login` 在车辆核验成功后直接以 600 权限原子保存，不读取或复制已有凭证。未知落地页、缺失票据、交换失败或车辆不匹配均停止并保留原凭证。

手机号登录通过 Python 标准库调用系统的 `openssl` 命令完成官方 RSA 加密，不依赖额外 Python 加密库。密码登录可从私有文件 `secrets/login.toml` 读取手机号和密码。图形验证码在当前解释器能导入 `ddddocr` 时直接使用它，否则使用项目 `.venv` 中的安装；两者都没有时尝试 `tesseract`，仍无可用结果时由本人输入。程序不绕过实名验证、不自动重试已发出的短信，也没有自动刷新或无人值守重新登录协议。

作者在 [issue #13 的说明](https://github.com/bihongfly/jinjingzheng/issues/13#issuecomment-3722585683)（2026-01-08）明确：京通登录返回的 token 不是续签使用的 AUTH，需要从上述办理请求的 `Authorization` 请求头获得。通用京通登录成功不能作为续签凭证有效的证据。

README 曾声称测试中 AUTH 不失效，但作者在 [issue #2](https://github.com/bihongfly/jinjingzheng/issues/2#issuecomment-1756586952)（2023-10-11）也报告过 AUTH 变化。凭证寿命不能由这些旧测试保证。不得在源码、示例、日志或研究记录中存放真实 AUTH、身份证号等敏感值。

## 原实现的边界与问题

- 第一辆车、第一条记录的硬编码会选错对象；空数组或 null 会触发异常。
- `yxqz` 缺失/null 时日期解析会失败；对未知状态缺少保守处理。
- 有效期剩余天数计算包含当天，生效中且剩余天数小于等于 1 时申请次日生效，实际触发点是到期当天。README 的“到期前一天”表述不能直接作为实现规则。
- 对已取消、审核失败直接重办，没有区分业务拒绝原因，也没有重复提交保护。
- 网络请求没有超时，异常处理、失败通知不完整；没有互斥锁或持久化提交记录。
- 提交响应成功后就使用“续签成功”措辞，未区分已受理、审核中、审核通过；后续查询固定索引也可能再次异常。
- 尚未合并的 [PR #16](https://github.com/bihongfly/jinjingzheng/pull/16)（2026-10-16）专门修复无证数组/null、`yxqz` 缺失、请求超时和异常通知，印证这些是当前 main 的实际缺口。

新程序应明确选择车辆和类型，根据最新可解释的状态判断办理窗口；提交前持久化意图，提交超时后先查结果，避免盲目重发。业务规则另以当前官方说明为准，不能直接沿用脚本中全部状态分支。

## 许可与验证范围

研究时仓库根目录未发现标准 LICENSE。README 对商用、转载等有自定义限制；本项目独立实现，只引用公开协议线索与流程，不整体复制远端代码。

参考项目部分仅为公开内容的只读研究。与官方 H5 的对照用到了本人凭证，但只调用了查询、字典和提交前校验接口；凭证寿命未实测。未调用提交接口，未提交任何进京证申请。

当前生产回调和 2443 业务源均通过正常 TLS 检查；另一个回调主机 `bjjj.jtgl.beijing.gov.cn` 的证书已过期，不代表当前生产入口不可用。手机号登录各环节的在线验证情况见 [办理凭证获取与核验](login-usage.md#验证范围与故障处理)，此处不再重复。
