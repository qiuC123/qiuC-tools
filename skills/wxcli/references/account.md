# 公众号账号内容

官方账号命令会访问真实微信 API。只在用户明确要求读取自己的公众号草稿或已发布
内容时使用。不要在单元测试或普通网页读取中调用真实 API。

## 凭证状态

只报告是否已配置，不输出值：

```powershell
wxcli --json auth status
```

交互式录入只能由用户在可见终端中明确发起：

```powershell
wxcli auth configure
```

不得把 AppSecret 放入命令参数、对话、日志或 JSON。

## 只读权限检查

真实网络检查必须显式授权：

```powershell
wxcli --json auth test --allow-live-api
```

该命令不会强制刷新有效 Token。AppID 存普通配置；AppSecret 与 Access Token
存入系统 keyring；到期时间存本地状态文件。

## 已发布内容

```powershell
wxcli --json account published list --offset 0 --count 20
wxcli --json account published get "ARTICLE_ID"
```

`get` 必须使用精确 `article_id`。

## 草稿

```powershell
wxcli --json account draft list --offset 0 --count 20
wxcli --json account draft get "MEDIA_ID"
```

`get` 必须使用精确 `media_id`。列表和详情都保留多图文 `articles[]` 及每篇
文章的 `index`。

## 从 Word 新建未发布草稿

先只生成本地预览；这一步不联网，也不读取凭证：

```powershell
wxcli --json account draft import-word ".\正文.docx" --cover ".\封面.png" --output ".\草稿预览"
```

把 `preview.html` 交给用户检查。只有用户明确确认该预览可以上传后，才能执行：

```powershell
wxcli --json account draft import-word ".\正文.docx" --cover ".\封面.png" --confirm
```

此命令只新建一个未发布草稿，不发布、不群发，也不修改已有草稿。正文图片上传
无法回滚；若最后的新建草稿请求失败，应向用户说明错误结果中的已上传图片数量。
