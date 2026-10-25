# 办理凭证获取与核验

核验日期：2026-10-25。续办接口使用的是北京交警 App 进京证页面查询请求里的 `Authorization` 请求头。它既不是京通登录 token，也不是 App 访问 `ssp.jtgl.beijing.gov.cn` 时带的登录态。

## 从 App 查询请求取得凭证

官方页面的业务服务在 `https://jjz.jtgl.beijing.gov.cn:2443`。从 App 取得的凭证已实测可用于配置默认的 443 源 `https://jjz.jtgl.beijing.gov.cn`。以下步骤以 iPhone 上的 Surge 为例：

1. 确认 Surge 证书已安装并完全信任，在 MITM 主机名中加入 `jjz.jtgl.beijing.gov.cn:2443`。非 443 端口必须写明端口，否则只会记录加密连接。
2. 打开北京交警 App →「进京证办理」，等车辆和证件列表加载完成。不需要提交申请。
3. 在最近请求中找到 `…:2443/pro//applyRecordController/stateList`，复制请求头 `Authorization` 的值。
4. 在 Mac 上用通用剪贴板或隔空投送取得该值，不要发到聊天或其他人。在项目目录运行：

```sh
python3 -m jjz auth-set
python3 -m jjz status
python3 -m jjz plan
```

也可以导出只包含这次查询的 HAR，放在本机 `secrets/` 下，用 `auth-import` 导入。导入器只接受目标源上 `stateList` 请求里唯一的 Authorization，先向同一服务做只读查询，核对到本人车牌后才原子替换凭证文件；失败时保留原文件。`--service-origin` 必须与 `api.base_url` 一致：

```sh
python3 -m jjz auth-import \
  --har secrets/permit-query.har \
  --service-origin https://jjz.jtgl.beijing.gov.cn
```

如果 HAR 里的请求来自 2443 端口，就把 `api.base_url` 和 `--service-origin` 都写成 `https://jjz.jtgl.beijing.gov.cn:2443`。

用完后从 MITM 主机名中删掉 2443 那一项，并删除抓包文件。Surge 配置可能同步到这台 Mac；如果 Mac 上的 Surge 也解密了 `api.base_url` 对应的主机，程序会因证书校验失败而停止，这是预期行为，不要关闭证书校验。抓包里还有 App 自身的登录态，不要长期保留，也不要提交到 Git。HAR、运行数据和凭证都已列入 `.gitignore`。

凭证有效期未知。`status` 或定时任务提示"接口拒绝凭证"时，按上述步骤重新取得一次。

## 手机号登录命令

`login-check` 只做只读预检；`login --method password|sms` 只完成京通统一认证阶段，不会产生或修改办理凭证，成功时也返回 `business_auth_verified: false` 并以退出码 2 结束。2026-10-25 实测，预检会在旧业务回调 `bjjj.jtgl.beijing.gov.cn` 的证书校验处停止，不会要求输入手机号，也不会发送短信。

登录只在本机终端接收输入，不支持把手机号、密码、验证码放进参数。密码、验证码和统一认证 Cookie 只留在进程内存，不写入配置或日志。登录加密需要可选依赖 `cryptography`（`python3 -m pip install -e '.[login]'`）。
