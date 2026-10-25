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
- 车辆字段取自查询结果里的车辆对象：`vId`、`hphm`、`hpzl`、`ylzsfkb`、`elzsfkb`、`ylzmc`、`elzmc`、`ylzqyms`、`elzqyms`、`cllx`。其中 `elzsfkb`/`ylzsfkb` 是服务端给出的"六环外/六环内当前可办"标志，选证页据此决定能否选择。
- 驾驶人信息取自 `getJsrxx`（`jsrxm`、`jszh`、`dabh`），不是取自证件记录。
- 页面新增目的地区县名 `area`、目的地坐标 `jjdzgdjd`/`jjdzgdwd`，以及 `jingState`（固定为空串）。`sfzj=1` 时发在京地址 `zjxxdz` 及坐标，并清空道路字段；`sfzj=0` 时发道路 `jjlk`、`jjlkmc` 及坐标 `jjlkgdjd`/`jjlkgdwd`，并清空在京地址字段。
- `sqdzgdjd`/`sqdzgdwd` 是提交时手机的当前定位，不是目的地坐标。`jjmd`/`jjmdmc` 可多选，用逗号拼接。
- 提交前依次调用：`applyVehicleCheck`（`data` 为 `"200"` 才继续）、`getJsrxx`、`getConfigRecordInfo`（取同行人开关 `txrkg`）、`applyCheckNum`、`checkHandle`（返回可选进京日期 `jjrqs`，格式 `YYYY-MM-DD`）、`checkInputRoadInfo`（`data` 不为 `"0"` 时要求填写行驶路线 `xslx`）。
- 区县、进京目的、进京道路的代码来自 `/pro/ucDicController/queryDic`，`type` 分别为 `qjxz`、`jjmd`、`jjlk`（`jjlk` 需带 `vId`）。

## AUTH 来源与有效期

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
