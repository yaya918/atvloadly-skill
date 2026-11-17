#!/usr/bin/env python3
"""atvloadly CLI — 通过 REST API 驱动 atvloadly 管理 Apple TV 侧载。

仅依赖 Python 标准库（Python 3.8+），无第三方依赖，便于跨机器移植。

常用场景：
    atvloadly.py health                       # 探测服务是否可达
    atvloadly.py scan                         # 扫描同网段的 Apple TV
    atvloadly.py devices                      # 列出已配对设备（含 UDID）
    atvloadly.py udid                         # 只打印 UDID，便于脚本管道使用
    atvloadly.py sources --url <repo>         # 预览 GitHub 仓库里的可装 IPA
    atvloadly.py identities                   # 列出外部证书签名身份
    atvloadly.py plan --identity 1 --ipa x.ipa --udid <UDID>
    atvloadly.py install --ipa-url <url> --device-id <id> --identity 1
    atvloadly.py apps                         # 列出已安装应用
    atvloadly.py log --app-id 3               # 读取安装日志

环境变量：
    ATVLOADLY_URL     服务地址，默认 http://127.0.0.1:5533
    ATVLOADLY_TOKEN   可选的访问令牌（若服务前置了鉴权）
"""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ws_client import WebSocket, WebSocketError, send_json  # noqa: E402
import setup_manager  # noqa: E402 — 配置持久化与本地自动安装

DEFAULT_BASE_URL = os.environ.get("ATVLOADLY_URL", "http://127.0.0.1:5533")
DEFAULT_TIMEOUT = int(os.environ.get("ATVLOADLY_TIMEOUT", "30"))

# 引导提示：未配置任何服务地址时打印，让调用方（人或 AI）知道下一步。
NEEDS_SETUP_HINT = """尚未配置 atvloadly 服务地址。请选择其一：

  【已有服务 / 自由部署】填入地址：
      python3 atvloadly.py setup --url http://你的地址:5533

  【本机还没有 / 自动安装】自动下载并安装：
      python3 atvloadly.py install-server

地址也可通过环境变量 ATVLOADLY_URL 临时指定。"""

# atvloadly 的签名错误码 → 人类可读含义。来源：internal/signing 包的 Code 常量。
SIGNING_ERROR_HINTS = {
    "device_not_provisioned": "描述文件里没有这台设备（UDID 不匹配），换带该 UDID 的描述文件",
    "profile_platform_mismatch": "描述文件平台与目标设备不符（iOS 描述文件不能装到 tvOS）",
    "custom_identifier_invalid": "自定义包名非法（不能含下划线等字符）",
    "incompatible": "IPA、描述文件与设备三者不兼容，看 issues 明细",
    "identity_referenced": "该签名身份仍被已安装应用引用，先删应用再删身份",
}


class ApiError(Exception):
    """atvloadly 返回的业务错误。"""

    def __init__(self, message, payload=None):
        super().__init__(message)
        self.payload = payload or {}


def _base_url(args):
    return setup_manager.resolve_base_url(getattr(args, "base_url", None))


def _request(args, method, path, params=None, body=None, files=None, raw=False, form=None):
    """发起一次 HTTP 请求并解析 atvloadly 的统一响应结构。

    atvloadly 几乎所有接口都返回 HTTP 200，成败看 body 里的 code 字段，
    因此这里必须先看 body 再决定是否抛错。

    body/form/files 三选一：
      body  → JSON body（用于 BodyParser 结构的接口，如 /signing/identities/:id/check）
      form  → application/x-www-form-urlencoded（用于 c.FormValue 读取的接口，如 /api/install）
      files → multipart/form-data（用于文件上传）
    """
    url = _base_url(args) + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )

    headers = {"Accept": "application/json"}
    token = args.token or os.environ.get("ATVLOADLY_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token

    data = None
    if files:
        boundary = "----atvloadly" + uuid.uuid4().hex
        buf = bytearray()
        if body:
            for key, value in body.items():
                buf += (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
        for key, (filename, content) in files.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            buf += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            buf += content + b"\r\n"
        buf += f"--{boundary}--\r\n".encode("utf-8")
        data = bytes(buf)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif form is not None:
        # c.FormValue() 只能从 urlencoded 或 multipart 读取，JSON body 会被忽略。
        data = urllib.parse.urlencode(
            {k: str(v).lower() if isinstance(v, bool) else str(v)
             for k, v in form.items() if v is not None}
        ).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if exc.code >= 500:
            raise ApiError(f"服务返回 HTTP {exc.code}: {payload[:200]!r}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(
            f"无法连接 {_base_url(args)} ({exc.reason})。"
            " 确认 atvloadly 已启动，且设备与本机在同一网段。"
        ) from exc

    if raw:
        return payload.decode("utf-8", errors="replace")

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        raise ApiError(f"响应不是合法 JSON: {payload[:200]!r}")

    # 统一响应结构: {code, msg, data}
    if isinstance(parsed, dict) and "code" in parsed:
        if parsed.get("code") == 0 or parsed.get("code") == -1:
            msg = parsed.get("msg") or ""
            code = None
            data_obj = parsed.get("data")
            if isinstance(data_obj, dict):
                code = data_obj.get("code")
                if code:
                    msg = data_obj.get("message") or msg
            if code:
                raise ApiError(msg or f"签名错误 {code}", parsed)
            if parsed.get("code") == -1:
                raise ApiError(msg or "请求失败", parsed)
            return parsed.get("data")
        return parsed.get("data")
    return parsed


def _emit(value, as_json=True):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        if isinstance(value, list):
            for item in value:
                print(item)
        else:
            print(value)


def _device_summary(devices):
    out = []
    for dev in devices or []:
        out.append(
            {
                "id": dev.get("id") or dev.get("device_id"),
                "udid": dev.get("udid"),
                "name": dev.get("name"),
                "device_class": dev.get("device_class"),
                "product_type": dev.get("product_type"),
                "product_version": dev.get("product_version"),
            }
        )
    return out


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------


def cmd_setup(args):
    """配置服务地址（自由部署路径）。--url 省略时交互式询问。"""
    cfg = setup_manager.load_config()
    url = args.url
    if not url:
        # 交互式：给出当前值作默认，回车沿用
        current = setup_manager.resolve_base_url()
        print("当前服务地址：" + current)
        try:
            typed = input("请输入 atvloadly 地址（回车沿用当前值）: ").strip()
        except EOFError:
            typed = ""
        url = typed or current
    url = url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        print("❌ 地址需以 http:// 或 https:// 开头", file=sys.stderr)
        return 1
    cfg["base_url"] = url
    cfg["source"] = "manual"
    if getattr(args, "proxy", None):
        cfg["proxy"] =.args.proxy
    path = setup_manager.save_config(cfg)
    print(json.dumps({"configured": True, "base_url": url, "config_path": path},
                     ensure_ascii=False, indent=2))
    # 配置后立即探测，给个反馈（不阻断，连不上也算配置成功）
    try:
        probe = _request(args, "GET", "/healthcheck")
        print("✓ 服务可达：" + json.dumps(probe, ensure_ascii=False)[:200])
    except Exception as exc:  # noqa: BLE001
        print("⚠ 已保存，但当前连不上该地址：{0}".format(str(exc)[:160]))
    return 0


def cmd_config(args):
    """查看/清除配置。"""
    cfg = setup_manager.load_config()
    if args.clear:
        setup_manager.clear_config()
        print(json.dumps({"cleared": True}, ensure_ascii=False))
        return 0
    safe = {k: v for k, v in cfg.items() if k != "token"}
    print(json.dumps({
        "config_path": setup_manager.config_path(),
        "resolved_base_url": setup_manager.resolve_base_url(getattr(args, "base_url", None)),
        "config": safe,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_install_server(args):
    """本地自动安装 atvloadly（自动安装路径）。"""
    if args.dry_run:
        print(json.dumps(setup_manager.plan_install(
            method=args.method, prefer_brew=not args.no_brew),
            ensure_ascii=False, indent=2))
        return 0
    if not args.yes:
        try:
            answer = input("将在本机下载并安装 atvloadly 服务，继续？[y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("已取消")
            return 1
    result = setup_manager.do_install(
        method=args.method, prefer_brew=not args.no_brew,
        proxies=args.proxy, log=lambda m: print(m, flush=True))
    print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("healthy") else 2


def cmd_health(args):
    """探测服务可达性。注意：应用全部过期时服务会返回 503，这不算服务挂了。"""
    try:
        data = _request(args, "GET", "/healthcheck")
    except ApiError as exc:
        payload = exc.payload or {}
        if "503" in str(exc):
            print(json.dumps({"reachable": True, "status": "unhealthy", "detail": str(exc)}, ensure_ascii=False, indent=2))
            return 0
        raise
    print(json.dumps({"reachable": True, "status": "healthy", "data": data}, ensure_ascii=False, indent=2))
    return 0


def cmd_scan(args):
    """扫描同网段的 Apple TV（无线发现）。"""
    data = _request(args, "GET", "/api/scan/wireless", params={"timeout": args.timeout})
    devices = data if isinstance(data, list) else (data or {}).get("devices", [])
    _emit(_device_summary(devices))
    return 0


def cmd_devices(args):
    """列出已配对设备。"""
    data = _request(args, "GET", "/api/devices")
    devices = data if isinstance(data, list) else (data or {}).get("devices", [])
    _emit(_device_summary(devices))
    return 0


def cmd_udid(args):
    """只打印 UDID，便于 shell 管道使用。"""
    data = _request(args, "GET", "/api/devices")
    devices = data if isinstance(data, list) else (data or {}).get("devices", [])
    for dev in devices:
        udid = dev.get("udid")
        if not udid:
            continue
        if args.name and dev.get("name") != args.name:
            continue
        print(udid)
        if not args.all:
            break
    return 0


def cmd_sources(args):
    """预览 GitHub 仓库 Releases 里可用于安装的 IPA。"""
    data = _request(
        args,
        "GET",
        "/api/sources/preview",
        params={
            "kind": args.kind,
            "url": args.url,
            "device_class": args.device_class,
            "prerelease": "true" if args.prerelease else None,
        },
    )
    _emit(data)
    return 0


def cmd_download(args):
    """从仓库下载指定 build 的 IPA（服务端缓存）。"""
    data = _request(
        args,
        "POST",
        "/api/sources/download",
        body={"kind": args.kind, "url": args.url, "build_id": args.build_id},
    )
    _emit(data)
    return 0


def cmd_identities(args):
    """列出已导入的外部证书签名身份。"""
    data = _request(args, "GET", "/api/signing/identities")
    items = data if isinstance(data, list) else (data or {}).get("identities", [])
    summary = []
    for it in items:
        summary.append(
            {
                "id": it.get("id"),
                "name": it.get("name"),
                "team_id": it.get("team_id"),
                "profile_name": it.get("profile_name"),
                "profile_platforms": it.get("profile_platforms"),
                "expires_at": it.get("expires_at"),
                "is_expired": it.get("is_expired"),
            }
        )
    _emit(summary)
    return 0


def cmd_import_identity(args):
    """导入 p12 + mobileprovision 组成外部签名身份。"""
    with open(args.p12, "rb") as fh:
        p12_bytes = fh.read()
    with open(args.profile, "rb") as fh:
        profile_bytes = fh.read()
    data = _request(
        args,
        "POST",
        "/api/signing/identities/import",
        body={"name": args.name, "password": args.password or ""},
        files={"p12": (os.path.basename(args.p12), p12_bytes),
               "profile": (os.path.basename(args.profile), profile_bytes)},
    )
    _emit(data)
    return 0


def cmd_plan(args):
    """在真正安装前跑一次签名兼容性预检（blocking=True 就装不上）。"""
    data = _request(
        args,
        "POST",
        f"/api/signing/identities/{args.identity}/check",
        body={
            "ipa_path": args.ipa,
            "udid": args.udid,
            "remove_extensions": args.remove_extensions,
            "allow_missing_entitlements": args.allow_missing_entitlements,
            "custom_identifier": args.custom_identifier or "",
        },
    )
    plan = (data or {}).get("plan", data)
    blocking = (data or {}).get("blocking")
    if blocking is None:
        blocking = bool(plan.get("issues")) and any(
            i.get("severity") == "error" for i in plan.get("issues", [])
        )
    issues = plan.get("issues", [])
    print(json.dumps({"blocking": blocking, "issues": issues}, ensure_ascii=False, indent=2))
    for issue in issues:
        code = issue.get("code", "")
        if code in SIGNING_ERROR_HINTS:
            print(f"  💡 {code}: {SIGNING_ERROR_HINTS[code]}", file=sys.stderr)
    return 2 if blocking else 0


def cmd_install(args):
    """安装 IPA 到指定设备。

    两种签名方式互斥：
      --account        用 Apple ID 签名（免费账号 10 注册 / 3 激活 / 7 天重签）
      --identity       用外部证书签名（需 account 为空）

    注意：/api/install 用 c.FormValue() 读参数，因此必须以
    application/x-www-form-urlencoded 提交，发 JSON 会被静默忽略。
    """
    if args.identity and args.account:
        raise ApiError(
            "--identity 与 --account 互斥：外部证书签名时不能同时传 Apple ID。"
            " 服务端会返回 'account must be empty with an external signing certificate'。"
        )
    if not args.identity and not args.account:
        raise ApiError("必须指定签名方式：--identity（外部证书）或 --account（Apple ID）")

    form = {
        "url": args.ipa_url,
        "device_id": args.device_id,
        "custom_identifier": args.custom_identifier or "",
        "custom_name": args.custom_name or "",
        "remove_extensions": args.remove_extensions,
        "allow_missing_entitlements": args.allow_missing_entitlements,
    }
    if args.identity:
        form["signing_mode"] = "external_certificate"
        form["signing_identity_id"] = args.identity
    else:
        form["signing_mode"] = "apple_id"
        form["account"] = args.account

    if args.plan:
        # 先预检再装，避免排错。
        data = _request(
            args,
            "POST",
            f"/api/signing/identities/{args.identity}/check",
            body={
                "ipa_path": args.ipa,
                "udid": args.udid,
                "remove_extensions": args.remove_extensions,
                "allow_missing_entitlements": args.allow_missing_entitlements,
                "custom_identifier": args.custom_identifier or "",
            },
        )
        plan = (data or {}).get("plan", data)
        blocking = (data or {}).get("blocking")
        if blocking:
            print(json.dumps({"stage": "plan", "blocking": True, "plan": plan}, ensure_ascii=False, indent=2))
            return 2

    data = _request(args, "POST", "/api/install", form=form)
    _emit(data)
    return 0


def cmd_apps(args):
    """列出已安装应用。"""
    data = _request(args, "GET", "/api/apps")
    items = data if isinstance(data, list) else (data or {}).get("apps", [])
    _emit(items)
    return 0


def cmd_log(args):
    """读取某个应用的安装日志。

    注意：日志端点不在 /api 分组下，路径是 /apps/:id/log。
    写成 /api/apps/:id/log 会返回 404（v0.8.0 实测）。
    """
    text = _request(args, "GET", f"/apps/{args.app_id}/log", raw=True)
    print(text)
    return 0


def cmd_status(args):
    """查看当前是否有正在进行的安装任务。"""
    data = _request(args, "GET", "/api/apps/installing")
    _emit(data)
    return 0


def cmd_delete(args):
    """删除已安装应用（会调用设备上的删除流程）。"""
    data = _request(args, "POST", f"/api/apps/{args.app_id}/delete")
    _emit(data)
    return 0


def cmd_reinstall(args):
    """重新安装已装应用（用于证书更新后重装）。"""
    data = _request(args, "POST", f"/api/apps/{args.app_id}/reinstall")
    _emit(data)
    return 0


def cmd_version(args):
    """查看 atvloadly 版本。"""
    data = _request(args, "GET", "/api/version")
    _emit(data)
    return 0


def cmd_login(args):
    """通过 WebSocket 登录 Apple ID 账号。

    atvloadly **没有** 登录用的 REST 接口，登录只走 /ws/login 的 WebSocket 通道：
      1. 发送 {"t":1,"d":"<account>\\n<password>"}  —— 注意 d 是 JSON 字符串，
         内部用换行分隔账号与密码
      2. 服务端输出登录进度；若需要 2FA，会提示输入验证码
      3. 收到 "Login Succeeded!" 即成功

    2FA 验证码可以通过 --code 传入，或用 --interactive 交互式输入。
    """
    base = _base_url(args)
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws/login"

    account = args.account
    password = args.password
    if account is None or password is None:
        # 不在命令行明文传密码：交互式读取
        import getpass

        if account is None:
            account = input("Apple ID 邮箱: ").strip()
        if password is None:
            password = getpass.getpass("Apple ID 密码: ")

    payload = json.dumps({"account": account, "password": password})
    try:
        with WebSocket(ws_url, timeout=args.timeout) as ws:
            send_json(ws, 1, payload)
            marker, output = ws.recv_until(
                ["Login Succeeded!", "Login Failed!", "ERROR", "verification code",
                 "验证码", "2FA"],
                timeout=args.login_timeout,
            )
    except WebSocketError as exc:
        raise ApiError(f"WebSocket 登录通道失败: {exc}") from exc
    except socket.timeout:
        raise ApiError("登录超时，未收到结果") from None

    print(output)

    if "login succeeded" in marker.lower() or "login succeeded" in output.lower():
        print("✅ 登录成功")
        # 登录后确认账号已登记
        try:
            data = _request(args, "GET", "/api/accounts")
            accounts = data if isinstance(data, list) else (data or {}).get("accounts", [])
            if accounts:
                print(json.dumps(accounts, ensure_ascii=False, indent=2))
        except ApiError:
            pass
        return 0

    # 需要 2FA 的分支
    if args.code:
        try:
            with WebSocket(ws_url, timeout=args.timeout) as ws:
                send_json(ws, 1, payload)
                marker, output = ws.recv_until(
                    ["verification code", "验证码", "2FA"], timeout=args.login_timeout
                )
                print(output)
                send_json(ws, 2, args.code)
                marker, output = ws.recv_until(
                    ["Login Succeeded!", "Login Failed!", "ERROR"], timeout=args.login_timeout
                )
                print(output)
                if "login succeeded" in output.lower():
                    print("✅ 登录成功")
                    return 0
                raise ApiError("2FA 验证后仍登录失败")
        except WebSocketError as exc:
            raise ApiError(f"2FA 流程失败: {exc}") from exc

    raise ApiError(
        "登录未成功。若提示需要 2FA 验证码，请用 --code <验证码> 重试。"
        f"\n服务端输出:\n{output}"
    )


def cmd_accounts(args):
    """列出已登录的 Apple ID 账号。"""
    data = _request(args, "GET", "/api/accounts")
    accounts = data if isinstance(data, list) else (data or {}).get("accounts", [])
    _emit(accounts)
    return 0


def cmd_logout(args):
    """登出指定 Apple ID 账号。"""
    data = _request(args, "POST", "/api/accounts/logout", body={"email": args.email})
    _emit(data)
    return 0


def cmd_certificates(args):
    """列出某 Apple ID 账号下的签名证书。"""
    data = _request(args, "GET", "/api/certificates", params={"email": args.email})
    _emit(data)
    return 0


def cmd_account_devices(args):
    """列出某 Apple ID 账号在 Apple 开发者门户注册的设备。"""
    data = _request(args, "GET", "/api/accounts/devices", params={"email": args.email})
    _emit(data)
    return 0


def _parse_mcp_payload(raw):
    """解析 MCP 响应。兼容 SSE（data: 前缀）与纯 JSON 两种格式。"""
    candidates = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            candidates.append(line[5:].strip())
        elif line.startswith("{"):
            candidates.append(line)
    for cand in candidates:
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
            return parsed
    return None


def _mcp_session(args):
    """建立 MCP 会话，返回 (session_id, ws_url)。"""
    base = _base_url(args)
    url = base + "/mcp"
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "atvloadly-cli", "version": "1.0"}}}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        session_id = resp.headers.get("Mcp-Session-Id")
        resp.read()
    if not session_id:
        raise ApiError("MCP 初始化未返回 Mcp-Session-Id，服务端可能不支持 MCP")
    # 发送 initialized 通知
    notify = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Mcp-Session-Id": session_id},
        method="POST",
    )
    try:
        with urllib.request.urlopen(notify, timeout=args.timeout) as resp:
            resp.read()
    except urllib.error.HTTPError:
        pass  # 通知类请求返回 202 属正常
    return session_id, url


def _mcp_call(args, tool_name, tool_args=None):
    """调用一个 MCP 工具，返回解析后的内容。"""
    session_id, url = _mcp_session(args)
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool_name, "arguments": tool_args or {}}}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Mcp-Session-Id": session_id},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    parsed = _parse_mcp_payload(raw)
    if parsed is None:
        raise ApiError(f"未收到有效的 MCP 响应: {raw[:200]}")
    if "error" in parsed:
        raise ApiError(f"MCP 错误: {parsed['error'].get('message')}", parsed)
    content = parsed.get("result", {}).get("content", [])
    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
    if texts:
        merged = "\n".join(texts)
        try:
            return json.loads(merged)
        except json.JSONDecodeError:
            return merged
    return raw


def cmd_mcp_tools(args):
    """列出 MCP 服务端暴露的工具。"""
    session_id, url = _mcp_session(args)
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Mcp-Session-Id": session_id},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    parsed = _parse_mcp_payload(raw)
    if parsed is None:
        raise ApiError(f"未收到 tools/list 响应: {raw[:200]}")
    if "error" in parsed:
        raise ApiError(f"MCP 错误: {parsed['error'].get('message')}", parsed)
    tools = parsed.get("result", {}).get("tools", [])
    summary = [
        {
            "name": t.get("name"),
            "description": (t.get("description") or "")[:120],
            "required": t.get("inputSchema", {}).get("required", []),
        }
        for t in tools
    ]
    _emit(summary)
    return 0


def cmd_mcp_call(args):
    """调用指定的 MCP 工具。

    用法: mcp-call <tool_name> [--arg key=value ...]
    """
    tool_args = {}
    for item in args.tool_arg or []:
        if "=" not in item:
            raise ApiError(f"参数格式应为 key=value，收到: {item}")
        key, value = item.split("=", 1)
        try:
            tool_args[key] = json.loads(value)
        except json.JSONDecodeError:
            tool_args[key] = value
    result = _mcp_call(args, args.tool_name, tool_args)
    _emit(result)
    return 0


def cmd_ipa_info(args):
    """读取本地 IPA 的关键元信息。

    装之前必查：不同变体可能共用同一个 bundle id，装第二个会顶掉第一个。
    """
    import plistlib
    import zipfile

    path = args.ipa
    try:
        zf = zipfile.ZipFile(path)
    except (FileNotFoundError, zipfile.BadZipFile) as exc:
        raise ApiError(f"无法打开 IPA: {exc}") from exc

    with zf:
        plists = [n for n in zf.namelist() if n.endswith(".app/Info.plist")]
        if not plists:
            raise ApiError("IPA 里找不到 Info.plist，可能不是有效的 .ipa 包")
        info = plistlib.loads(zf.read(plists[0]))
        app_dir = plists[0].split(".app/")[0] + ".app"

    supported = info.get("CFBundleSupportedPlatforms") or []
    if isinstance(supported, str):
        supported = [supported]
    platform = None
    for p in supported:
        if "TV" in str(p).upper():
            platform = "tvOS"
            break
    if platform is None and supported:
        platform = supported[0]

    output = {
        "ipa_path": path,
        "app_dir": app_dir,
        "display_name": info.get("CFBundleDisplayName"),
        "bundle_name": info.get("CFBundleName"),
        "bundle_id": info.get("CFBundleIdentifier"),
        "version": info.get("CFBundleShortVersionString"),
        "build": info.get("CFBundleVersion"),
        "min_os": info.get("MinimumOSVersion"),
        "platform": platform,
        "supported_platforms": supported,
    }
    _emit(output)

    if platform != "tvOS":
        print(
            f"\n⚠️  警告：这个 IPA 不是 tvOS 构建（platform={platform}），"
            "装到 Apple TV 上会失败。",
            file=sys.stderr,
        )

    # 与已装应用比对 bundle id，检测冲突
    if not args.no_check:
        try:
            data = _request(args, "GET", "/api/apps")
            apps = data if isinstance(data, list) else (data or {}).get("apps", [])
            for app in apps:
                if app.get("bundle_identifier") == output["bundle_id"]:
                    print(
                        f"\n⚠️  警告：bundle id 与已装应用「{app.get('ipa_name')}」"
                        f"(ID={app.get('ID')}) 相同，\n"
                        f"    安装会直接替换它，不会共存。需要共存请用 --custom-identifier 改包名。",
                        file=sys.stderr,
                    )
                    break
        except ApiError:
            pass
    return 0


def cmd_mcp(args):
    """检查 MCP 端点是否可用（atvloadly 内置 MCP server，AI 可直连）。"""
    base = _base_url(args)
    url = base + "/mcp"
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "atvloadly-cli", "version": "1.0"}}}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        print(json.dumps({"mcp_available": True, "endpoint": url, "response": text[:800]}, ensure_ascii=False, indent=2))
        return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(json.dumps({"mcp_available": exc.code == 400 or exc.code == 405,
                          "endpoint": url, "http_status": exc.code, "response": body[:400]},
                         ensure_ascii=False, indent=2))
        return 0
    except urllib.error.URLError as exc:
        raise ApiError(f"无法连接 {url}: {exc.reason}") from exc


# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="atvloadly.py",
        description="通过 REST API 驱动 atvloadly 管理 Apple TV 侧载（零第三方依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", help="atvloadly 服务地址，默认 http://127.0.0.1:5533")
    parser.add_argument("--token", help="访问令牌（也可用环境变量 ATVLOADLY_TOKEN）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP 超时秒数")
    parser.add_argument("--json", action="store_true", default=True, help="以 JSON 输出（默认开）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="配置服务地址（已有服务时用，或交互式输入）")
    p.add_argument("--url", help="服务地址，如 http://192.168.x.x:5533；省略则交互式询问")
    p.add_argument("--proxy", help="GitHub 加速源，逗号分隔（写入配置，供自动安装用）")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("config", help="查看当前配置 / 清除配置")
    p.add_argument("--clear", action="store_true", help="删除已保存的配置")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("install-server", help="本机没有服务时，自动下载安装 atvloadly")
    p.add_argument("--method", choices=["brew", "dmg", "tar", "exe", "docker"],
                   help="强制安装方式；不传则按平台自动选择")
    p.add_argument("--no-brew", action="store_true", help="macOS 上不走 Homebrew，改用 DMG")
    p.add_argument("--proxy", help="自定义 GitHub 加速源，逗号分隔，如 https://ghfast.top/")
    p.add_argument("--yes", action="store_true", help="跳过确认（适合自动化）")
    p.add_argument("--dry-run", action="store_true", help="只打印安装计划，不执行")
    p.set_defaults(func=cmd_install_server)

    p = sub.add_parser("health", help="探测服务可达性")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("scan", help="扫描同网段的 Apple TV")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("devices", help="列出已配对设备")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("udid", help="打印 UDID")
    p.add_argument("--name", help="按设备名过滤")
    p.add_argument("--all", action="store_true", help="打印全部而非第一台")
    p.set_defaults(func=cmd_udid)

    p = sub.add_parser("sources", help="预览仓库里可装的 IPA")
    p.add_argument("--url", required=True, help="GitHub 仓库地址，如 https://github.com/owner/repo")
    p.add_argument("--kind", default="github", help="来源类型，默认 github")
    p.add_argument("--device-class", default="tvOS", help="目标设备类别，默认 tvOS")
    p.add_argument("--prerelease", action="store_true", help="包含预发布版本")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("download", help="下载指定 build 的 IPA")
    p.add_argument("--url", required=True)
    p.add_argument("--build-id", required=True)
    p.add_argument("--kind", default="github")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("identities", help="列出外部证书签名身份")
    p.set_defaults(func=cmd_identities)

    p = sub.add_parser("import-identity", help="导入 p12 + 描述文件")
    p.add_argument("--p12", required=True, help="p12 文件路径")
    p.add_argument("--profile", required=True, help="mobileprovision 路径")
    p.add_argument("--name", required=True, help="身份名称")
    p.add_argument("--password", help="p12 密码，可为空")
    p.set_defaults(func=cmd_import_identity)

    p = sub.add_parser("plan", help="签名兼容性预检")
    p.add_argument("--identity", type=int, required=True)
    p.add_argument("--ipa", required=True, help="服务端已下载的 IPA 路径")
    p.add_argument("--udid", required=True)
    p.add_argument("--custom-identifier")
    p.add_argument("--remove-extensions", action="store_true")
    p.add_argument("--allow-missing-entitlements", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("install", help="安装 IPA")
    p.add_argument("--ipa-url", required=True, help="IPA 下载地址（http/https 直链）")
    p.add_argument("--device-id", required=True, help="目标设备 ID")
    p.add_argument("--identity", type=int, help="外部签名身份 ID")
    p.add_argument("--account", help="Apple ID 邮箱（用 Apple ID 签名时填）")
    p.add_argument("--udid", help="目标设备 UDID（配合 --plan 预检时必填）")
    p.add_argument("--ipa", help="服务端已下载的 IPA 路径（配合 --plan 预检时必填）")
    p.add_argument("--custom-identifier")
    p.add_argument("--custom-name")
    p.add_argument("--remove-extensions", action="store_true")
    p.add_argument("--allow-missing-entitlements", action="store_true")
    p.add_argument("--plan", action="store_true", help="安装前先跑一次预检")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("apps", help="列出已安装应用")
    p.set_defaults(func=cmd_apps)

    p = sub.add_parser("status", help="查看安装任务状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("log", help="读取安装日志")
    p.add_argument("--app-id", type=int, required=True)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("delete", help="删除已安装应用")
    p.add_argument("--app-id", type=int, required=True)
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("reinstall", help="重新安装已装应用")
    p.add_argument("--app-id", type=int, required=True)
    p.set_defaults(func=cmd_reinstall)

    p = sub.add_parser("version", help="查看 atvloadly 版本")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("mcp", help="检查 MCP 端点可用性")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("ipa-info", help="读取本地 IPA 元信息并检查 bundle id 冲突")
    p.add_argument("--ipa", required=True, help="本地 IPA 文件路径")
    p.add_argument("--no-check", action="store_true", help="跳过与已装应用的冲突检查")
    p.set_defaults(func=cmd_ipa_info)

    p = sub.add_parser("mcp-tools", help="列出 MCP 工具清单")
    p.set_defaults(func=cmd_mcp_tools)

    p = sub.add_parser("mcp-call", help="调用一个 MCP 工具")
    p.add_argument("tool_name", help="工具名，如 get_device_list")
    p.add_argument("--tool-arg", action="append", help="工具参数，格式 key=value，可重复")
    p.set_defaults(func=cmd_mcp_call)

    p = sub.add_parser("login", help="通过 WebSocket 登录 Apple ID")
    p.add_argument("--account", help="Apple ID 邮箱（不传则交互式输入）")
    p.add_argument("--password", help="Apple ID 密码（不传则交互式隐藏输入）")
    p.add_argument("--code", help="2FA 验证码")
    p.add_argument("--login-timeout", type=int, default=120, help="等待登录结果的秒数")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("accounts", help="列出已登录的 Apple ID 账号")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("logout", help="登出 Apple ID 账号")
    p.add_argument("--email", required=True)
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("certificates", help="列出 Apple ID 账号下的证书")
    p.add_argument("--email", required=True)
    p.set_defaults(func=cmd_certificates)

    p = sub.add_parser("account-devices", help="列出 Apple ID 注册的设备")
    p.add_argument("--email", required=True)
    p.set_defaults(func=cmd_account_devices)

    return parser


# 这些子命令不需要服务地址，配置未就绪时也可执行（值为 func.__name__）
LOCAL_ONLY_COMMANDS = {
    "cmd_setup", "cmd_config", "cmd_install_server", "cmd_ipa_info",
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # 首次运行引导：需要连服务的命令，若完全没配地址，给出明确指引而不是闷头连默认值。
    # 用 --base-url 或 ATVLOADLY_URL 显式指定时不拦截（兼容脚本化调用与现有测试）。
    cmd = getattr(args, "func", None).__name__ if getattr(args, "func", None) else ""
    needs_service = cmd not in LOCAL_ONLY_COMMANDS and cmd and cmd != ""
    explicit = bool(getattr(args, "base_url", None) or os.environ.get("ATVLOADLY_URL"))
    if needs_service and not explicit and not setup_manager.is_configured():
        print(NEEDS_SETUP_HINT, file=sys.stderr)
        print(json.dumps({"needs_setup": True, "config_path": setup_manager.config_path()},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    try:
        return args.func(args) or 0
    except ApiError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        if exc.payload:
            print(json.dumps(exc.payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"❌ 文件不存在: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
