# 公众号账号内容

官方账号命令会访问真实微信 API。只在用户明确要求读取自己的公众号草稿或已发布
内容时使用。不要在单元测试或普通网页读取中调用真实 API。

## 凭证状态

只报告是否已配置，不输出值：

```powershell
wechat-oa --json auth status
```

交互式录入只能由用户在可见终端中明确发起：

```powershell
wechat-oa auth configure
```

不得把 AppSecret 放入命令参数、对话、日志或 JSON。

## 只读权限检查

真实网络检查必须显式授权：

```powershell
wechat-oa --json auth test --allow-live-api
```

该命令不会强制刷新有效 Token。AppID 存普通配置；AppSecret 与 Access Token
存入系统 keyring；到期时间存本地状态文件。

## 已发布内容

```powershell
wechat-oa --json account published list --offset 0 --count 20
wechat-oa --json account published get "ARTICLE_ID"
```

`get` 必须使用精确 `article_id`。

## 草稿

```powershell
wechat-oa --json account draft list --offset 0 --count 20
wechat-oa --json account draft get "MEDIA_ID"
```

`get` 必须使用精确 `media_id`。列表和详情都保留多图文 `articles[]` 及每篇
文章的 `index`。

## 从 Word 新建未发布草稿

先只生成本地预览；这一步不联网，也不读取凭证：

```powershell
wechat-oa --json account draft import-word ".\正文.docx" --cover ".\封面.png" --output ".\草稿预览"
```

把 `preview.html` 交给用户检查。只有用户明确确认该预览可以上传后，才能执行：

```powershell
wechat-oa --json account draft import-word ".\正文.docx" --cover ".\封面.png" --confirm
```

此命令只新建一个未发布草稿，不发布、不群发，也不修改已有草稿。正文图片上传
无法回滚；上传检查点会按 SHA-256 去重并支持重试。新建完成后必须通过正文与图片
回查验证。

## 安全修改已有草稿

先做独立备份（已有文件不会被覆盖）：

```powershell
wechat-oa --json account draft backup "MEDIA_ID" --output ".\backup.json"
```

再生成只读差异与冻结计划；此命令不上传、不修改微信：

```powershell
wechat-oa --json account draft diff "MEDIA_ID" ".\正文.docx" --cover ".\封面.png" --index 0 --output ".\更新计划"
```

把 `backup.json`、`plan.json` 和 `prepared\preview.html` 交给用户检查。只有用户在
计划生成后明确授权应用这个计划，才能执行：

```powershell
wechat-oa --json account draft update ".\更新计划" --confirm
```

不得把 `--confirm` 合并到首次生成计划的动作中。执行时 wechat-oa 会重新获取远端草稿，
指纹变化时拒绝覆盖；本地准备包被改动也会拒绝。更新后必须回查正文和图片。该流程
仍不允许发布、群发、删除或修改计划之外的文章索引。
