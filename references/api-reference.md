# atvloadly REST API 参考

所有路径基于服务根地址（默认 `http://127.0.0.1:5533`）。

## 响应结构

几乎所有 `/api/*` 接口返回统一信封：

```json
{ "code": 0, "msg": "success", "data": { ... } }
```

- `code == 0` → 成功，看 `data`
- `code == -1` → 失败，看 `msg`
- **HTTP 状态码几乎恒为 200**，不要用它判断成败

签名类错误会在 `data` 里带 `{ "code": "<签名错误码>", "message": "..." }`。

## 请求体格式（重要）

| 格式 | 用于 | 原因 |
|---|---|---|
| `application/x-www-form-urlencoded` | `/api/install` | 服务端用 `c.FormValue()` 读取 |
| `application/json` | `/api/signing/identities/:id/check`、`/api/sources/*` | 服务端用 `c.BodyParser()` 读取 |
| `multipart/form-data` | `/api/signing/identities/import`、`/api/upload` | 文件上传 |

**用 JSON 调 `/api/install` 会导致参数被静默忽略**，最终报 "account is required" 之类的怪错。

---

## 健康检查

### `GET /healthcheck`
无 `/api` 前缀。

- 应用全部过期时返回 **503**（`{"status":"unhealthy"}`），这不代表服务不可用
- 正常返回 200 + `{"status":"healthy","time":<unix>}`

### `GET /api/version`
返回版本信息。

### `GET /api/version/latest`
返回最新可用版本。

---

## 设备

### `GET /api/devices`
列出已配对设备。响应 `data` 为数组，每项含：

| 字段 | 说明 |
|---|---|
| `id` | 设备 ID（安装时用） |
| `udid` | 设备唯一标识（描述文件必须含此值） |
| `name` | 设备名 |
| `device_class` | 如 `tvOS` |
| `product_type` | 如 `AppleTV6,2` |
| `product_version` | 系统版本 |

### `GET /api/devices/:id`
单设备详情。

### `GET /api/scan`
扫描并返回设备列表（阻塞式）。

### `GET /api/scan/wireless?timeout=5`
无线发现。`timeout` 取值 1-10 秒，超出按 10 秒处理。

### `POST /api/reload`
重新载入设备列表。

### `POST /api/pair`
触发配对流程。

### `POST /api/pair/import`
导入配对文件（multipart，字段 `file`，可选 `ip`/`port`/`manual`）。

### `GET /api/pair/tunnel-port?ip=<ip>`
探测手动输入 IP 的隧道端口，返回 `{port, name, model}`。

### `POST /api/devices/:id/mountimage`
挂载 Developer Disk Image。

### `POST /api/devices/:id/screenshot`
设备截图，返回 `{type:"screenshot", data:"<base64>"}`。

### `POST /api/devices/:id/check/afc`
检查 AFC 服务可用性。

### `POST /api/devices/:id/manual/delete`
删除手动添加的设备。

---

## 签名身份（外部证书）

### `GET /api/signing/identities`
列出已导入的外部签名身份。每项含：

`id`、`name`、`team_id`、`profile_name`、`profile_platforms`（如 `["tvOS"]`）、`expires_at`、`is_expired`、`revision`

### `POST /api/signing/identities/import`
导入外部签名身份。**multipart/form-data**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `p12` | 文件 | 代码签名证书（私钥） |
| `profile` | 文件 | mobileprovision 描述文件 |
| `name` | 文本 | 身份名称 |
| `password` | 文本 | p12 密码，**可以留空** |

描述文件必须包含目标设备的 UDID，平台必须是 tvOS。

### `POST /api/signing/identities/:id/profile`
替换描述文件。multipart，字段 `profile`。
**仅替换不重装不会更新设备上的应用**，必须随后调用 `/api/apps/:id/reinstall`。

### `POST /api/signing/identities/:id/delete`
删除身份。JSON body 可选 `{ "force": true }`。
若该身份仍被已安装应用引用，会返回错误码 `identity_referenced` 并带上 `app_count`。

### `POST /api/signing/identities/:id/check`
签名兼容性预检。**JSON body**：

```json
{
  "ipa_path": "/服务端的/ipa路径",
  "udid": "<目标设备 UDID>",
  "remove_extensions": false,
  "allow_missing_entitlements": false,
  "custom_identifier": ""
}
```

响应：

```json
{
  "plan": {
    "issues": [ { "code": "...", "severity": "error|warning|info", "message": "...", "bundle": "..." } ],
    "main_bundle_id": "com.example.app",
    "signed_main_bundle_id": "com.example.app",
    "bundles": [ ... ]
  },
  "blocking": false
}
```

`blocking: true` 表示存在 severity 为 `error` 的 issue，安装会失败。

---

## 安装

### `POST /api/install`
**表单编码**。参数：

| 字段 | 说明 |
|---|---|
| `url` | IPA 直链（http/https），或用 `file` 字段上传文件，二者其一必填 |
| `file` | multipart 时上传 IPA 文件 |
| `device_id` | 目标设备 ID |
| `signing_mode` | `apple_id` 或 `external_certificate` |
| `account` | Apple ID 邮箱（`signing_mode=apple_id` 时必填；用证书时必须为空） |
| `signing_identity_id` | 签名身份 ID（`signing_mode=external_certificate` 时必填） |
| `custom_identifier` | 自定义包名，不能含下划线 |
| `custom_name` | 自定义显示名 |
| `custom_icon_path` | 自定义图标路径（来自 `/api/custom-icon/upload`） |
| `remove_extensions` | `true` 时移除 App Extensions |
| `allow_missing_entitlements` | `true` 时允许缺失 entitlement（需付费开发者账号） |

成功响应：

```json
{
  "status": "installing",
  "message": "Install task queued",
  "device": "客厅 Apple TV",
  "udid": "...",
  "signing_mode": "external_certificate",
  "signing_identity_id": 1
}
```

安装是异步任务，需轮询日志或 `/api/apps/installing`。

### `GET /api/apps/installing`
列出当前正在进行的安装任务。

### `GET /api/apps`
列出已安装应用。

### `GET /apps/:id/log`
读取安装日志（纯文本）。**该端点不在 `/api` 分组下**，写 `/api/apps/:id/log` 会 404。典型结尾：

```
Signing bundle
Installing to device
Installation complete!
Installation Succeeded!
```

### `POST /api/apps/:id/delete`
删除已安装应用。

### `POST /api/apps/:id/reinstall`
重新安装（换证书后必须走这一步才生效）。

### `POST /api/apps/:id/refresh`
刷新应用（外部证书签名的不支持自动刷新）。

### `GET /api/apps/refresh`
批量刷新状态。

---

## 来源（IPA 仓库）

### `GET /api/sources/preview?kind=github&url=<repo>&device_class=tvOS&prerelease=false`
预览仓库里可安装的 IPA 构建。

### `POST /api/sources/download`
JSON body：`{ "kind": "github", "url": "<repo>", "build_id": "<id>" }`
下载并返回服务端缓存的 IPA 路径。

### `GET /api/sources/catalog?device_class=tvOS`
来源目录。

### `GET /api/sources/saved` / `POST /api/sources/saved` / `POST /api/sources/saved/:id/delete`
已保存来源的增删查。

### `POST /api/sources/check`
检查已追踪应用的来源更新。

---

## Apple ID 账号

### `GET /api/accounts`
列出已登录的 Apple ID 账号。

### `GET /api/accounts/devices?email=<email>`
该账号注册的设备。

### `POST /api/accounts/logout`
JSON body：`{ "email": "..." }`

### `POST /api/accounts/devices/delete`
JSON body：`{ "email": "...", "deviceId": "..." }`

### `GET /api/certificates?email=<email>`
该账号的签名证书。

### `POST /api/certificates/revoke`
JSON body：`{ "email": "...", "serialNumber": "..." }`

### `POST /api/certificates/export`
表单：`email`、`password`、`teamId`。返回 p12 二进制。

---

## 上传与设置

### `POST /api/upload`
multipart，字段 `files`（可多个）。返回 IPA 文件对象。

### `POST /api/custom-icon/upload`
上传自定义图标，返回可传给 `custom_icon_path` 的路径。

### `GET /api/settings` / `POST /api/settings/:key`
读取/更新设置，`key` 可为 `notification`、`network`、`task`、`update`。

### `GET /api/service/status`
服务运行状态。

---

---

## WebSocket

| 路径 | 用途 |
|---|---|
| `GET /ws/pair` | 配对流程 |
| `GET /ws/install` | 安装进度 |
| `GET /ws/login` | Apple ID 登录 |
| `GET /ws/tools/scan` | 设备扫描 |

### 消息格式

```json
{ "t": <类型>, "d": <数据字符串> }
```

| 类型 | 用途 | d 的内容 |
|---|---|---|
| `t=1` | 登录 | JSON 字符串：`{"account":"...","password":"..."}` |
| `t=2` | 2FA 验证码 | 验证码纯文本 |

配对通道复用 `t` 值：`1`=配对、`2`=确认配对、`3`=探测手动 IP 隧道端口、`4`=开始手动配对。

### 登录流程

1. 连接 `ws://<host>:5533/ws/login`
2. 发送 `{"t":1,"d":"{\"account\":\"a@b.com\",\"password\":\"xxx\"}"}`
3. 服务端持续输出登录进度；需要 2FA 时会提示输入验证码
4. 收到 `Login Succeeded!` 即成功；失败则输出 `Login Failed!` 与 `ERROR: ...`

账号密码错误时服务端返回 `Authentication SRP error -20970: Your account information was entered incorrectly.` —— 这是苹果的响应，说明通道本身正常。

2FA 流程：收到提示后发送 `{"t":2,"d":"<验证码>"}`。

**注意：`d` 是 JSON 字符串而非对象**，需要再做一层 `json.dumps` 转义。

---

## MCP 端点

### `ALL /mcp` 与 `ALL /mcp/*`
JSON-RPC over HTTP。atvloadly 内置 MCP server，暴露 7 个工具：

| 工具 | 说明 |
|---|---|
| `install_app` | 安装 IPA。参数 `ipa_url`(必填)、`device_id`、`account_id`、`signing_identity_id`、`custom_identifier`、`remove_extensions` |
| `get_device_list` | 列出可用设备 |
| `get_account_list` | 列出 Apple ID 账号 |
| `get_app_list` | 列出已安装应用 |
| `get_install_status` | 返回 `in_progress` 或 `completed` |
| `refresh_app` | 刷新应用 |
| `get_refresh_status` | 刷新任务状态 |

`install_app` 若未指定设备或签名方式，会返回 `require_device` / `require_account` 等状态并列出可选项，需再次调用并补齐参数。

`account_id` 与 `signing_identity_id` 不能同时使用。

**MCP 端点无鉴权，务必部署在可信网络。**

### 会话流程（必须按序）

1. `POST /mcp`，method=`initialize`，protocolVersion=`2024-11-05`
2. 从响应头取 **`Mcp-Session-Id`**
3. `POST /mcp`，method=`notifications/initialized`，带上 session id（返回 202）
4. `POST /mcp`，method=`tools/list` 或 `tools/call`，带上 session id

跳过第 3 步会报：`method "tools/list" is invalid during session initialization`。

响应可能是 SSE 格式（`event: message` + `data: {...}`）或纯 JSON，两者都需兼容处理。

### 实测确认（v0.8.0）

`serverInfo` 为 `{"name":"atvloadly-mcp","version":"v0.8.0"}`，7 个工具齐全，
与源码 `internal/mcp/tools/register.go` 的注册表完全一致。
