# 本地操作与诊断

## Doctor

默认 Doctor 不执行真实网络或账号检查：

```powershell
wechat-oa --json doctor
```

只有用户明确授权真实只读检查时使用：

```powershell
wechat-oa --json doctor --allow-live-api
```

## 浏览器

```powershell
wechat-oa --json browser status
wechat-oa --json browser login
wechat-oa --json browser clear
wechat-oa --json browser policy status
wechat-oa browser policy set auto-fallback
wechat-oa browser policy set never
```

- `status` 绝不启动 Chrome，只报告 `profile_exists`、旧版迁移时间和真实文章成功读取
  产生的 `last_successful_read_at`；这些本地事实不证明远端会话当前有效。
- `login` 打开 wechat-oa 独立、可见、持久的 Chrome profile，供用户手工登录或
  验证；窗口正常结束不等于文章已经成功读取。
- `clear` 删除该独立 profile 和本地状态记录。它是破坏性本地操作，必须获得
  用户明确请求。
- profile 有跨进程锁；被占用时不要并发启动第二个 wechat-oa Chrome。
- `policy set auto-fallback` 是一次持久授权，只能在用户明确要求后运行；默认策略为
  `never`。`browser clear` 不修改策略，`--no-browser` 可以禁止单次调用。

## 缓存

```powershell
wechat-oa --json cache clear
```

只清理公共文章成功缓存。由于它会删除本地数据，必须由用户明确请求。
