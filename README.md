# atvloadly Skill · 让 AI 直接给 Apple TV 装应用

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![零依赖](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](.)
[![测试](https://img.shields.io/badge/tests-48%20passed-success.svg)](scripts/test_atvloadly.py)

> 配套视频：**全网首发！AI 帮我把红果装到了 Apple TV 上**（B站 / 抖音 / 小红书）
> 作者：芽芽ol（B站） · 芽芽打包酱（微信公众号）

---

## 这是什么

一个给 AI 用的操作手册 + 工具集。

装好之后，AI 就能直接驱动 [atvloadly](https://github.com/bitxeno/atvloadly) 给 Apple TV 侧载应用——
查版本、验包名、下载、签名、安装、看日志，全流程**不用你手动点网页**。

**核心原理**：atvloadly 内置了一个 MCP 服务端（7 个工具），
本 skill 把它封装成 AI 能直接调用的形式。

一句话效果：

> 你只说「帮我给 Apple TV 装一下红果」，剩下的 AI 自己干。

---

## 快速开始

### 1️⃣ 下载

从 [Releases](https://github.com/yaya918/atvloadly-skill/releases) 下载 `atvloadly.zip`
（或直接 clone 本仓库后自行打包）。

### 2️⃣ 安装

把 `atvloadly` 整个文件夹放进 `~/.workbuddy/skills/`，重启 WorkBuddy：

```
~/.workbuddy/skills/atvloadly/
├── SKILL.md
├── README.md
├── scripts/
│   ├── atvloadly.py       主 CLI
│   ├── setup_manager.py   服务地址配置 + 本地自动安装 + 加速源
│   ├── ws_client.py       纯标准库 WebSocket 客户端
│   └── test_atvloadly.py  测试
└── references/
    ├── api-reference.md
    └── troubleshooting.md
```

> 也可以直接跟 AI 说「帮我安装 atvloadly 技能」，然后把 zip 路径给它。

### 3️⃣ 配置服务地址（二选一）

**A. 我已经有 atvloadly 服务**

```bash
python3 ~/.workbuddy/skills/atvloadly/scripts/atvloadly.py setup --url http://你的地址:5533
```

地址会存到 `~/.config/atvloadly/skill.json`，以后不用重复填。

**B. 我还没有 → 自动装**

```bash
S=~/.workbuddy/skills/atvloadly/scripts/atvloadly.py
python3 $S install-server --dry-run   # 先看会装什么（平台/版本/方式/加速源）
python3 $S install-server --yes       # 确认后自动下载安装并启动
```

- macOS：优先 Homebrew，其次下载 DMG 装到「应用程序」
- Linux：下载 tar.gz 解压；Windows：下载 exe 安装
- 都没有：用 Docker（需已装 docker）
- 装完自动探测服务、写入配置
- **GitHub 下载慢或超时？** 脚本先直连，失败自动切加速源（`ghfast.top` / `ghproxy.net`，已实测可用）

> 没配置就跑别的命令会提示你先做这一步（这是正常的首次引导，不是报错）。
> 临时用一下也行：`export ATVLOADLY_URL=http://地址:5533`。

### 4️⃣ 开始用

配好之后，直接跟 AI 说人话：

> 「帮我给 Apple TV 装一下红果短剧」

AI 会自己找版本、验包名、下载、装上、看日志确认。

---

## 证书从哪来？（签名方式二选一）

装应用要有签名，两条路，按自己情况选：

**方式 A · 外部证书（签一次管很久，推荐长期用）**

- 需要 `p12` 证书 + `mobileprovision` 描述文件
- 不受 3 激活 / 7 天重签限制，稳定省心
- 购买地址（作者店铺）：**https://yayaol.diannaozy.top/**
- 拿到文件后用 `import-identity` 导入

**方式 B · 免费 Apple ID（不花钱，7 天一签）**

- 用自己已有的 Apple ID 登录即可，**不用买证书**
- 限制：10 注册名额 / 3 同时激活 / **7 天需重签一次**
- 已有 Apple ID 的直接 `login` 就行，这是完全可用的方案

> 已经有证书或 Apple ID 的，直接用对应的 `import-identity` / `login`，不必购买。
> 两种方式的差别只是「省不省心」：证书长期稳定，Apple ID 免费但要定期重签。

---

## 依赖

**只要 Python 3.8+，别的都不要。**

- 零第三方包
- 纯标准库（WebSocket 客户端是自己实现的）
- 换台电脑解压就能跑

---

## 命令速查

```bash
S=~/.workbuddy/skills/atvloadly/scripts/atvloadly.py

python3 $S health                      # 服务通不通
python3 $S config                      # 看当前配置
python3 $S devices                     # 已配对的设备
python3 $S udid                        # 取 UDID
python3 $S ipa-info --ipa app.ipa      # 读 IPA 元信息 + 查包名冲突
python3 $S plan --identity 1 --ipa /path/app.ipa --udid <UDID>   # 装前预检
python3 $S install --ipa-url <url> --device-id <id> --identity 1 # 安装
python3 $S log --app-id 3              # 看安装日志
python3 $S mcp-tools                   # 列 MCP 的 7 个工具
python3 $S mcp-call get_device_list    # 用原生 MCP 协议调用
```

完整命令表：`python3 $S --help`

---

## 它能干什么

| 能力 | 说明 |
|---|---|
| 首次配置 | 填地址即用，或未配置时自动下载安装服务（含 GitHub 加速源） |
| 探测服务 | 健康检查、版本查询 |
| 管理设备 | 扫描配对、列设备、取 UDID、截图 |
| 装应用 | 表单编码提交安装，支持 Apple ID 与外部证书两种签名 |
| 签名预检 | 装之前跑一遍，提前发现包名冲突、描述文件不匹配 |
| 导入证书 | p12 + mobileprovision 一条命令导入 |
| 读 IPA | 解析 Info.plist，**自动检测 bundle id 冲突** |
| 查账号 | 列 Apple ID 账号、证书、注册的设备 |
| 登录账号 | 走 WebSocket 完成登录（含 2FA） |
| MCP 直连 | `mcp-tools` / `mcp-call`，让 AI 用原生 MCP 协议操作 |

---

## 几个必须知道的点

**1. 所有接口都返回 HTTP 200**
成败看响应 body 里的 `code` 字段（`0` 成功，`-1` 失败）。别拿 HTTP 状态码判断。

**2. 安装接口用表单编码，不是 JSON**
发 JSON 会被静默忽略、参数全丢。

**3. 登录没有网页接口**
只有 WebSocket `/ws/login`。skill 里已经封好了。

**4. 部分应用的多个变体共用 bundle id**
装第二个会顶掉第一个，不会共存。`ipa-info` 命令会在装之前提醒你。

**5. NAS 连不上 GitHub 的话**
让 AI 在能上网的电脑上起个临时 http 服务，让 NAS 去拉。

> 更多排错见 [references/troubleshooting.md](references/troubleshooting.md)
> 完整 API 契约见 [references/api-reference.md](references/api-reference.md)

---

## 自检

```bash
cd ~/.workbuddy/skills/atvloadly
python3 scripts/test_atvloadly.py
```

会起一个内置的 mock 服务跑全套测试，不碰你的真实设备。
当前 **48 项测试**（含首次配置、地址优先级、加速源、各平台安装包选择）。

---

## 相关链接

| 项目 | 地址 |
|---|---|
| 本 skill（AI 侧载能力） | https://github.com/yaya918/atvloadly-skill |
| tvOS 短剧客户端（本仓库配套） | https://github.com/yaya918/guoapp-tvos |
| atvloadly（侧载工具本体） | https://github.com/bitxeno/atvloadly |
| 证书购买（作者店铺） | https://yayaol.diannaozy.top/ |

---

## 合规

- 侧载应用请自行承担内容与版权合规责任
- 建议用专用「小号」Apple ID 做免费账号侧载
- MCP 端点没有鉴权，不要暴露到公网
- 本 skill 仅供技术学习与个人设备使用

---

## 致谢

- [atvloadly](https://github.com/bitxeno/atvloadly) — 侧载工具本体
- [guoapp-tvos](https://github.com/yaya918/guoapp-tvos) — tvOS 客户端
