TESTPROBE# atvloadly 排错手册

## 首次配置 / 自动安装

**命令返回退出码 2 + `needs_setup`**：还没配服务地址，先二选一：
- 有服务：`python3 scripts/atvloadly.py setup --url http://地址:5533`
- 没服务：`python3 scripts/atvloadly.py install-server --yes`
- 临时：`export ATVLOADLY_URL=http://地址:5533`
用 `config` 看当前配置，`config --clear` 清空重来。

**地址配了但连不上**：
- `setup` 时提示"连不上"只是警告，地址仍已保存；用 `health` 复测
- 确认服务真的在跑（桌面版已打开 / Docker 容器 up）、与本机同网段
- 端口对不对：桌面版与 Docker 映射都是 `5533`（Docker 内部 80 → 宿主 5533）

**自动安装失败 / 下载超时**：
- 先看计划：`install-server --dry-run`（确认平台识别、版本、方式正确）
- GitHub 直连超时会自动切加速源；若都失败，自定义：`setup --proxy https://ghfast.top/`
  或 `export ATVLOADLY_GH_PROXY=https://ghfast.top/,https://ghproxy.net/`
- macOS 无 brew 又不用 DMG 时，装 Docker 后 `install-server --method docker`
- DMG 安装后首次打开被系统拦截：到 系统设置 → 隐私与安全性 → 允许；脚本已尝试 `xattr -dr` 去隔离

**配置写哪了 / 想 portable**：
- 默认 `~/.config/atvloadly/skill.json`（Windows `%APPDATA%\atvloadly\skill.json`）
- `ATVLOADLY_CONFIG=/path/to/skill.json` 可整体改位置（也便于测试隔离）

---

## 排错决策树

```
安装失败
├── 看响应 body 的 code / msg
│   ├── code=-1 且 msg 含 "account is required"
│   │   → 用了 apple_id 模式但没传 account
│   ├── code=-1 且 msg 含 "signing_identity_id is required"
│   │   → 用了 external_certificate 模式但没传身份 ID
│   ├── code=-1 且 msg 含 "account must be empty"
│   │   → 同时传了 account 和签名身份，两者互斥
│   ├── code=-1 且 msg 含 "either file upload or url parameter is required"
│   │   → 没给 IPA 地址
│   ├── code=-1 且 msg 含 "install task was not queued"
│   │   → 队列满或重复安装，查 /api/apps/installing
│   └── data.code 是签名错误码 → 查下方「签名错误码对照表」
└── 装上了但打不开
    ├── 看 /api/apps/:id/log
    ├── 检查描述文件是否含该设备 UDID
    ├── 检查描述文件平台是否 tvOS
    └── 检查 IPA 是否真的是 tvOS 版本
```

---

## 签名错误码对照表

| 错误码 | 原因 | 解决 |
|---|---|---|
| `device_not_provisioned` | 描述文件里没有这台设备 | 换一份包含该 Apple TV UDID 的描述文件，重新导入并重装 |
| `profile_platform_mismatch` | 描述文件平台与目标设备不符 | iOS 描述文件不能装到 tvOS 设备，需重新签发 tvOS 描述文件 |
| `custom_identifier_invalid` | 自定义包名非法 | 包名不能含下划线；建议直接用点分格式如 `com.example.myapp` |
| `incompatible` | IPA、描述文件、设备三者不兼容 | 看 issues 明细逐条处理 |
| `identity_referenced` | 签名身份仍被已装应用引用 | 先删除引用它的应用，再删身份；或用 `{"force": true}` |
| `upload_too_large` | 上传文件超限 | 换更小的 IPA |

---

## 常见场景

### 1. `scan` 扫不到 Apple TV

按顺序排查：
1. 关掉 VPN（会阻断 mDNS 发现）
2. 确认 Mac / Apple TV 在同一非访客 WiFi
3. 检查路由器是否开启了 AP 隔离 / 客户端隔离
4. 重启 Apple TV，再进配对模式重试
5. Docker 部署时确认共享了 avahi：
   `-v /var/run/avahi-daemon:/run/avahi-daemon`，必要时加 `--privileged`
6. 桌面版无需 avahi，检查 macOS 防火墙是否拦截 mDNS

**替代路径**：手动输入 Apple TV 的 IP 配对。
Apple TV → 设置 → 网络 → 查看 IP 地址，然后：
```bash
python3 scripts/atvloadly.py scan          # 配对后
```

### 2. 取不到 UDID

主路径（任何配对方式都可用）：
```bash
curl -s http://<host>:5533/api/devices | python3 -m json.tool
```
或直接用脚本 `python3 scripts/atvloadly.py udid`。

备用路径（仅手动输入 IP 配对时生成配对记录）：
```bash
cat ~/.config/PlumeImpactor/pairing_files/*.json | python3 -m json.tool
```
注意：mDNS 自动发现并配对的设备不生成此文件。

### 3. 装上了但应用打不开

| 现象 | 排查 |
|---|---|
| 图标灰的、点不开 | 签名与描述文件不匹配，或描述文件已过期 |
| 装的是 iOS 版 | IPA 必须是 tvOS 构建，改后缀名无效 |
| 用了免费 Apple ID | 7 天后未重签会失效；或超过 3 个激活数导致旧应用失效 |

用 `plan` 预检确认：
```bash
python3 scripts/atvloadly.py plan --identity 1 --ipa <ipa路径> --udid <UDID>
```

### 3.1 同一个应用被"顶掉"了（bundle id 冲突）

**实测坑点**：有些项目会发布多个变体，但**共用同一个 bundle id**。
以 `yaya918/guoapp-tvos` v2.59 为例：

| 资产名 | CFBundleDisplayName | CFBundleIdentifier |
|---|---|---|
| `hongguojian-2.59-tvos-unsigned.ipa` | 火龙果短剧 | `com.duanju.guoapp.tv` |
| `zhenguojian-2.59-tvos-unsigned.ipa` | 真果鉴 | `com.duanju.guoapp.tv` |

两者 bundle id **完全相同**，所以装第二个会**直接替换**第一个，
atvloadly 里也只保留一条记录（`ipa_name` 变成后装的那个）——装之前那个等于白装。

**装之前先比对 bundle id**：

```bash
python3 - <<'PY'
import zipfile, plistlib, sys
z = zipfile.ZipFile(sys.argv[1])
app = [n for n in z.namelist() if n.endswith('.app/Info.plist')][0]
info = plistlib.loads(z.read(app))
print('显示名    :', info.get('CFBundleDisplayName'))
print('bundle id :', info.get('CFBundleIdentifier'))
print('版本      :', info.get('CFBundleShortVersionString'))
PY
```

如果想让两个变体共存，必须给其中一个用 `custom_identifier` 改包名，
且描述文件的 application-identifier 要能覆盖新包名。

### 3.2 服务端下载 GitHub 超时

```
failed to fetch GitHub releases: ... TLS handshake timeout
```

说明 **atvloadly 所在机器（NAS）到 GitHub 不通**——常见于国内网络或 NAS 没配代理。

**解法：让 AI 在能上网的机器上把 IPA 拉下来，再喂给服务端。**
服务端支持从任意 http 直链取 IPA（`/api/install` 的 `url` 参数），
所以在能上网的电脑上起一个临时服务即可：

```bash
# 在能上网的电脑上（与 atvloadly 同一网段）
cd /path/to/ipa && python3 -m http.server 8899 --bind 0.0.0.0
```

然后让服务端拉 `http://<本机IP>:8899/xxx.ipa`。

**用完记得关掉**：`pkill -f "http.server 8899"`

### 4. 证书到期了怎么办

外部证书签的应用到期时间 = min(证书到期, 描述文件到期)。

续期步骤：
1. 换一份新的描述文件（必须含目标设备 UDID）
2. 替换：`POST /api/signing/identities/:id/profile`
3. **必须重装**：否则设备上的旧应用不会被更新
   ```bash
   python3 scripts/atvloadly.py reinstall --app-id <id>
   ```

若证书先到期，把新证书作为新身份导入，再重装。

**用同一套证书重装不会延长有效期。**

### 5. 免费 Apple ID 的三个限制

| 限制 | 数值 | 后果 |
|---|---|---|
| 注册名额 | 10 个 | 注册满后无法再注册新应用 |
| 同时激活 | 3 个 | 装第 4 个时，前面的应用会变成不可用 |
| 重签周期 | 7 天 | 过期后需重新签名安装 |

对策：
- 用专用的"小号" Apple ID，不要用主账号
- 不想 7 天一签、想长期稳定 → 改走外部证书路线，购买地址（作者店铺）：
  https://yayaol.diannaozy.top/ ；导入方式见 SKILL.md 第 3 步
- 也可以不买，继续用 Apple ID 每 7 天重签一次（免费，只是要手动维护）
- 账号证书满时，atvloadly 会提示可吊销某个证书；它不会自动吊销

### 6. 换证书后应用没变化

按顺序确认：
1. 描述文件是否已替换成功（`identities` 命令看 `expires_at` 是否更新）
2. 是否执行了重装（仅替换文件不重装不生效）
3. 重装时用的还是不是新的签名身份

### 7. 接口返回 200 但操作没生效

atvloadly 所有接口恒返 HTTP 200。必须解析 body 的 `code` 字段：

```bash
curl -s http://<host>:5533/api/devices | python3 -m json.tool
```

`code: 0` 才是成功。

---

## 脚本自身错误

| 报错 | 原因 |
|---|---|
| `无法连接 <url>` | 服务未启动、端口不对、或不在同一网段 |
| `参数缺失` / `参数错误` | 检查 `--device-id`、`--ipa-url` 是否传了 |
| `互斥` | 同时传了 `--identity` 和 `--account` |
| `必须指定签名方式` | 两者都没传 |
| `未收到 tools/list 响应` | MCP 会话未完成初始化：需先 initialize 取 session id，再发 notifications/initialized |
| `method "tools/list" is invalid during session initialization` | 跳过了 `notifications/initialized` 通知 |
| `MCP 初始化未返回 Mcp-Session-Id` | 服务端版本过低不支持 MCP，或走了反向代理改写了响应头 |

---

## 登录相关

| 现象 | 含义 |
|---|---|
| `Logging in...` 后无下文 | 正在与苹果认证服务器通信，等一会儿 |
| `Authentication SRP error -20970` | **账号或密码错误**。通道是通的，纯粹是凭据问题 |
| `Login Failed!` | 登录未通过，看前面 `ERROR:` 的具体原因 |
| 提示输入验证码 | 账号开启了 2FA，用 `--code <验证码>` 走二次流程 |
| `Login Succeeded!` | 成功。随后 `accounts` 命令能列出该账号 |

登录日志里出现 `Logging in...` 重复两次是正常的——atvloadly 会先尝试一次再重试。

---

## 证书导入相关

| 错误码 | 原因 | 解决 |
|---|---|---|
| `identity_duplicate` | 同一套证书+描述文件已导入过 | 说明已有对应身份，用 `identities` 查看；确需另建可换描述文件 |
| `a signing identity with the same certificate and provisioning profile already exists` | 同上，v0.8.0 的文案 | 同上 |
| `device_not_provisioned` | 描述文件未包含目标设备 | 重新签发含该 UDID 的描述文件 |
| `profile_platform_mismatch` | 平台不匹配 | 需要 tvOS 描述文件，不能用 iOS 的 |

导入成功后身份列表会显示 `revocation_not_checked`（info 级），这是正常状态。

---

## 实测记录（v0.8.0 · 2026-11-12）

在真实实例上验证过的结论：

- `GET /healthcheck` 正常返回 healthy
- `GET /api/devices` 返回已配对的 Apple TV，字段含 `id`、`udid`、`name`、`device_class`
- `GET /api/signing/identities` 返回身份，含 `team_id`、`profile_platforms`、`expires_at`、`app_count`
- `POST /api/signing/identities/:id/check` 对已装应用预检返回 `blocking: false`，说明描述文件确实包含该设备 UDID
- `POST /api/install` 缺签名方式时返回 `account is required`（HTTP 仍为 200）
- `GET /ws/login` WebSocket 通道可用，能收到苹果的 -20970 响应
- `POST /mcp` initialize 返回 `Mcp-Session-Id`，7 个工具齐全，`get_device_list` 调用成功
- 重复导入同一套证书返回 `identity_duplicate`

---

## 合规提醒

- 侧载应用需自行承担内容与版权合规责任
- 不要安装来源不明的破解包
- 免费 Apple ID 建议用专用账号，避免主账号证书名额被占满
- MCP 端点无鉴权，不要暴露到公网
