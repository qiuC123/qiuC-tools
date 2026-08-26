# 本地操作与诊断

## Doctor

默认 Doctor 不执行真实网络或账号检查：

```powershell
wxcli --json doctor
```

只有用户明确授权真实只读检查时使用：

```powershell
wxcli --json doctor --allow-live-api
```

## 浏览器

```powershell
wxcli --json browser status
wxcli --json browser login
wxcli --json browser clear
```

- `status` 绝不启动 Chrome，只报告 `profile_exists` 和本地记录的
  `last_verified_at`。
- `login` 打开 wxcli 独立、可见、持久的 Chrome profile，供用户手工登录或
  验证。
- `clear` 删除该独立 profile 和本地状态记录。它是破坏性本地操作，必须获得
  用户明确请求。
- profile 有跨进程锁；被占用时不要并发启动第二个 wxcli Chrome。

## 缓存

```powershell
wxcli --json cache clear
```

只清理公共文章成功缓存。由于它会删除本地数据，必须由用户明确请求。
