# JSON、错误与安全契约

## JSON envelope

成功：

```json
{"ok":true,"data":{}}
```

失败：

```json
{"ok":false,"error":{"code":"...","message":"...","details":{}}}
```

使用 `--json` 时 stdout 必须只有一个 UTF-8 JSON；提示、日志和进度只能出现在
stderr。不要把两个 JSON、说明文字或 shell 标记混入 stdout。

## 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 成功 |
| 1 | 通用错误 |
| 2 | CLI 输入错误 |
| 3 | 业务验证错误 |
| 4 | 资源不存在 |
| 5 | 网络错误 |
| 6 | 认证、权限或需要人工验证 |
| 7 | Chrome 错误 |
| 8 | 页面解析错误 |
| 9 | 本地配置错误 |

始终同时检查退出码与 JSON envelope。`VERIFICATION_REQUIRED` 映射到退出码 6。
`BROWSER_BUSY` 映射到退出码 7。浏览器仍出现人工挑战时继续使用
`VERIFICATION_REQUIRED`，并附加安全的 browser stage 与 required action。

## 凭证与隐私

- 不读取、回显或导出 Cookie。
- 不在命令参数中传 AppSecret 或 Access Token。
- 不打印 keyring 内容、环境变量或本地状态文件中的敏感值。
- 不把凭证写入缓存、Git、测试 fixture 或 Agent 对话。
- 使用脱敏 fixture、模拟 API、临时目录和 fake credential backend 做测试。

## 写入边界

所有 Provider 都只读。允许的写操作只有两类：用户检查本地预览并明确授权后使用
`account draft import-word --confirm` 新建一个未发布草稿；或者先运行只读的
`draft backup` 与 `draft diff`，让用户检查冻结计划后，再单独明确授权
`draft update PLAN_DIR --confirm`。不得在生成计划时顺便确认，不得跳过远端指纹
复核。wechat-oa 不提供发布、群发、删除、点赞或评论命令。不要绕过这一限制调用隐藏
接口或直接请求其他写 API。
