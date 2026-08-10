# staging 联调验收清单

这份清单用于 Mac 浏览器/助手连接 `https://staging.bookthink.cloud` 的联调记录。它不代替腾讯云固定部署、证书自动续期、Windows 安装包验收或生产发布；这些事项必须单独记录状态。

## 固定信息

- BPMS PR：[ebayda-bpms#1](https://github.com/sunzhufeng12345/ebayda-bpms/pull/1)，当前为 Draft。
- 助手 PR：[ebayda-dewu-helper#1](https://github.com/sunzhufeng12345/ebayda-dewu-helper/pull/1)、[ebayda-dewu-helper#2](https://github.com/sunzhufeng12345/ebayda-dewu-helper/pull/2)。
- staging API：`https://staging.bookthink.cloud`；助手联调需设置 `EBAYDA_ALLOW_STAGING_API=1`。
- 80/443 入口和 HTTPS 已启用；不要用生产域名或 `http://IP:端口` 代替 staging API。

## 联调前

- [ ] 记录 BPMS/助手实际 commit、三个 PR 链接和本次检查时间。
- [ ] 记录 staging 部署 ref、已应用迁移编号和服务状态；服务器不直接编辑代码。
- [ ] 在浏览器打开 staging 网站和 API，确认 HTTPS 证书有效，并记录证书到期时间。
- [ ] 确认 `AUTOMATION_PUBLIC_BASE_URL` 和 `EBAYDA_API_ORIGIN` 都指向 staging；生产环境变量未被带入。
- [ ] 记录本轮单测命令和结果；临时对比文件、缓存和 `.exe` 不加入 PR。

## 任务流程

- [ ] 从 staging 网页创建一个测试任务，记录任务 ID、商品 ID 和测试商品。
- [ ] 助手通过一次性 ticket 成功领取任务；重复领取、过期 ticket 和错误用户应被拒绝。
- [ ] 产品 JSON、图片 ZIP/文件下载成功，且下载来源仍是 staging HTTPS；记录文件数量和校验结果。
- [ ] 助手回传开始、进度、成功和失败状态；网页能看到最终状态与错误原因。
- [ ] 分别验证重试、终止、超时清理和店铺绑定，不复用已消费 ticket。
- [ ] 在得物测试流程中完成允许的浏览器操作；未执行的真实步骤要写明原因，不得用“接口成功”代替。

## 记录与结论

将以下模板粘贴到相关 PR 描述或联调评论中，并附日志/截图链接：

```text
CHECKED_AT=<ISO-8601 timestamp>
BPMS_PR=https://github.com/sunzhufeng12345/ebayda-bpms/pull/1
HELPER_PR=<one-or-more-helper-pr-urls>
BPMS_COMMIT=<commit-or-tag>
HELPER_COMMIT=<commit-or-tag>
MIGRATIONS=<applied-migration-ids>
CASES=<claim,download,callback,retry,terminate,cleanup,binding>
STAGING_RESULT=PASS|FAIL|BLOCKED
EVIDENCE=<log-or-screenshot-links>
WINDOWS_E2E=PASS|FAIL|BLOCKED_NO_WINDOWS
CERT_EXPIRY=<timestamp-or-N/A>
CERT_RENEWAL=DEFERRED
PRODUCTION_RELEASE=NOT_DONE
```

`STAGING_RESULT=PASS` 只代表本清单中的 Mac staging 场景通过。当前没有 Windows 环境时，必须保留 `WINDOWS_E2E=BLOCKED_NO_WINDOWS`；安装/卸载、`ebayda://` 注册、系统 Chrome、DPAPI、SmartScreen 和升级场景不能宣称已验收。证书自动续期、固定部署和生产发布完成前，整体发布门禁仍保持未完成。
