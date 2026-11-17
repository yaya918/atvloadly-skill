PROBE5
#!/usr/bin/env python3
"""setup_manager.py — atvloadly 的「服务地址配置」与「本地自动安装」管理器。

零第三方依赖，Python 3.8+。被 atvloadly.py 直接 import，也可单独调用做自检。

设计目标（对应用户需求）：
 1. 自由部署与自动安装并存：
    - 已有服务（本机桌面版 / NAS Docker / 任意地址）→ setup --url 填入即用
    - 没有服务 → install-server 自动下载安装并启动
 2. 首次运行引导：未配置任何地址时，主脚本给出明确指引（问用户地址 or 自动装）
 3. GitHub 加速源：下载安装包时先直连，失败后逐个尝试 ghfast.top / ghproxy.net 等

配置文件位置（可用 ATVLOADLY_CONFIG 覆盖，便于测试/便携）：
 macOS/Linux : ~/.config/atvloadly/skill.json
 Windows     : %APPDATA%\\atvloadly\\skill.json
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:5533"

# 默认 GitHub 加速源（顺序即尝试顺序）。可用配置里的 proxy 或环境变量覆盖。
# 说明：直连 GitHub releases 跳转常被网络环境挡住，这几个前缀在 2026-11 实测可用。
DEFAULT_GH_PROXIES = [
    "https://ghfast.top/",
    "https://ghproxy.net/",
]

REPO_OWNER = "bitxeno"
REPO_NAME = "atvloadly"
RELEASES_API = "https://api.github.com/repos/{o}/{r}/releases/latest".format(
    o=REPO_OWNER, r=REPO_NAME
)
BREW_CASK = "{o}/tap/{r}".format(o=REPO_OWNER, r=REPO_NAME)
DOCKER_IMAGE = "bitxeno/atvloadly:latest"
APP_MACOS = "atvloadly.app"


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------
def config_path():
    """返回配置文件绝对路径。ATVLOADLY_CONFIG 可整体覆盖（含文件名）。"""
    override = os.environ.get("ATVLOADLY_CONFIG")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "atvloadly", "skill.json")


def load_config():
    """读配置；文件不存在或损坏返回空字典，绝不抛异常。"""
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data):
    """原子写配置（同目录临时文件 + replace），返回写入路径。"""
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(data)
    payload["configured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    fd, tmp = tempfile.mkstemp(prefix=".skill-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def clear_config():
    """删除配置文件；不存在也算成功。"""
    path = config_path()
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def resolve_base_url(cli_url=None):
    """解析服务地址，优先级：命令行 > 环境变量 > 配置文件 > 默认值。

    命令行/环境变量由主脚本透传进来（cli_url）；这里额外再读一次环境变量，
    保证单独调用本模块时也遵守同一优先级。
    """
    if cli_url:
        return cli_url.rstrip("/")
    env = os.environ.get("ATVLOADLY_URL")
    if env:
        return env.rstrip("/")
    cfg = load_config()
    if cfg.get("base_url"):
        return str(cfg["base_url"]).rstrip("/")
    return DEFAULT_BASE_URL


def is_configured(cli_url=None):
    """是否已有任何可用的地址来源。"""
    return bool(
        cli_url
        or os.environ.get("ATVLOADLY_URL")
        or load_config().get("base_url")
    )


def get_proxies(explicit=None):
    """返回加速源列表。优先级：显式参数 > 配置 > 环境变量 > 默认值。"""
    if explicit:
        return [p.strip().rstrip("/") + "/" for p in explicit.split(",") if p.strip()]
    cfg = load_config()
    if cfg.get("proxy"):
        raw = cfg["proxy"]
    elif os.environ.get("ATVLOADLY_GH_PROXY"):
        raw = os.environ["ATVLOADLY_GH_PROXY"]
    else:
        return list(DEFAULT_GH_PROXIES)
    return [p.strip().rstrip("/") + "/" for p in str(raw).split(",") if p.strip()]


# --------------------------------------------------------------------------
# 平台探测与资产选择
# --------------------------------------------------------------------------
def detect_platform():
    """返回 (os_key, arch_key)：darwin/linux/windows × amd64/arm64。"""
    system = platform.system().lower()
    if system.startswith("darwin"):
        os_key = "darwin"
    elif system.startswith("win"):
        os_key = "windows"
    elif system.startswith("linux"):
        os_key = "linux"
    else:
        os_key = system

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64", "armv8"):
        arch = "arm64"
    else:
        arch = machine
    return os_key, arch


def version_from_tag(tag):
    """v0.8.4 -> 0.8.4"""
    return re.sub(r"^[vV]", "", tag or "").strip()


def pick_asset(assets, os_key=None, arch=None, prefer_brew=False):
    """从 release assets 里挑出匹配当前平台的资产名。

    assets: release API 的 assets 列表（含 name / browser_download_url / digest）。
    返回 (asset_dict, method)；method ∈ brew/dmg/docker/tar/exe。
    """
    os_key = os_key or detect_platform()[0]
    arch = arch or detect_platform()[1]
    names = {a["name"]: a for a in assets}

    if os_key == "darwin" and prefer_brew and "atvloadly.rb" in names:
        return names["atvloadly.rb"], "brew"

    if os_key == "darwin":
        dmg = [n for n in names if n.lower().endswith(".dmg")]
        if dmg:
            return names[sorted(dmg)[0]], "dmg"
        if "atvloadly.rb" in names:
            return names["atvloadly.rb"], "brew"
        return None, "docker"
    if os_key == "linux":
        want = "atvloadly-linux-{0}.tar.gz".format(arch)
        if want in names:
            return names[want], "tar"
        return None, "docker"
    if os_key == "windows":
        exes = [n for n in names if "-setup.exe" in n.lower() and arch in n.lower()]
        if exes:
            return names[sorted(exes)[0]], "exe"
        return None, "docker"
    return None, "docker"


def fetch_latest_release(timeout=20):
    """通过 GitHub API 取最新 release 元数据。API 通常直连可达。"""
    req = urllib.request.Request(
        RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "atvloadly-skill"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------
# 带加速源 fallback 的下载
# --------------------------------------------------------------------------
def _looks_valid(dest, expect_ext=None):
    """粗校验下载结果，避免把代理的错误页当成安装包。"""
    try:
        if os.path.getsize(dest) < 4096:
            return False
        with open(dest, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return False
    if head.lstrip().startswith((b"<", b"{")):  # HTML/JSON 错误页
        return False
    if expect_ext == ".gz" and not head.startswith(b"\x1f\x8b"):
        return False
    if expect_ext == ".dmg" and b"koly" not in head[-512:] and head[:8] not in (b"\x78\x9e\x00\x03",):
        # DMG 尾部常有 koly，头部也可能是 zlib 压缩；这里放宽，只排除明显错误页
        return True
    return True


def download_with_fallback(url, dest, proxies=None, timeout=180, expect_ext=None, log=print):
    """下载文件：先直连，再逐个加速源。返回实际使用的来源标签。

    加速源拼接格式： https://ghfast.top/ + https://github.com/...
    """
    proxies = proxies if proxies is not None else get_proxies()
    candidates = [("直连", url)] + [("加速源 " + p.rstrip("/"), p.rstrip("/") + "/" + url) for p in proxies]

    last_err = None
    for label, target in candidates:
        try:
            log("  → 尝试 {0}: {1}".format(label, target[:110]))
            req = urllib.request.Request(target, headers={"User-Agent": "atvloadly-skill"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest + ".part", "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(1 << 18)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if total and log:
                        pct = int(done * 100 / total)
                        if pct % 20 == 0:
                            log("     下载 {0}% ({1}/{2} B)".format(pct, done, total))
            if not _looks_valid(dest + ".part", expect_ext):
                raise ValueError("下载内容不像有效安装包（可能是错误页）")
            os.replace(dest + ".part", dest)
            log("  ✓ 成功：{0}".format(label))
            return label
        except Exception as exc:  # noqa: BLE001 — 下载失败要逐个源兜底
            last_err = exc
            log("  ✗ 失败：{0} — {1}".format(label, str(exc)[:160]))
            if os.path.exists(dest + ".part"):
                os.unlink(dest + ".part")
    raise RuntimeError("所有来源均下载失败，最后一个错误：{0}".format(last_err))


# --------------------------------------------------------------------------
# 安装执行
# --------------------------------------------------------------------------
def _run(cmd, capture=False, check=True):
    """执行命令，返回 CompletedProcess。capture=False 时输出直接透传。"""
    return subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        check=check,
        capture_output=capture,
        text=True,
    )


def _has(cmd):
    return shutil.which(cmd) is not None


def install_brew():
    """macOS：brew 安装 cask 并启动。"""
    if not _has("brew"):
        raise RuntimeError("未检测到 brew，无法用 Homebrew 安装")
    _run(["brew", "install", "--cask", BREW_CASK])
    _run("open -a atvloadly", check=False)
    return "brew cask 安装完成，已尝试启动 atvloadly.app"


def install_dmg(asset, workdir, proxies=None, log=print):
    """macOS：下载 DMG → 挂载 → 复制到 /Applications → 去隔离属性 → 启动。"""
    dmg = os.path.join(workdir, asset["name"])
    log("下载 macOS DMG...")
    download_with_fallback(asset["browser_download_url"], dmg, proxies, expect_ext=".dmg", log=log)
    mount = os.path.join(workdir, "dmg-mount")
    os.makedirs(mount, exist_ok=True)
    log("挂载 DMG...")
    _run(["hdiutil", "attach", "-nobrowse", "-mountpoint", mount, dmg])
    try:
        src = os.path.join(mount, APP_MACOS)
        if not os.path.exists(src):
            raise RuntimeError("DMG 内未找到 " + APP_MACOS)
        dst = "/Applications/" + APP_MACOS
        log("复制到 " + dst + "（可能需要输入系统密码）...")
        if os.path.exists(dst):
            _run(["rm", "-rf", dst])
        _run(["cp", "-R", src, dst])
    finally:
        _run(["hdiutil", "detach", mount, "-force"], check=False)
    _run("sudo xattr -dr com.apple.quarantine '/Applications/atvloadly.app'", check=False)
    _run("open -a atvloadly", check=False)
    return "DMG 安装完成，已尝试启动 atvloadly.app（如首次打开被拦截，请到 系统设置→隐私与安全性 允许）"


def install_tar(asset, workdir, proxies=None, log=print):
    """Linux：下载 tar.gz → 解压到 ~/.local/atvloadly → chmod。"""
    tar = os.path.join(workdir, asset["name"])
    log("下载 Linux 安装包...")
    download_with_fallback(asset["browser_download_url"], tar, proxies, expect_ext=".gz", log=log)
    target = os.path.join(os.path.expanduser("~"), ".local", "atvloadly")
    os.makedirs(target, exist_ok=True)
    log("解压到 " + target + "...")
    _run(["tar", "-xzf", tar, "-C", target])
    for root, _dirs, files in os.walk(target):
        for name in files:
            if "atvloadly" in name.lower() and "." not in name.split("/")[-1]:
                _run(["chmod", "+x", os.path.join(root, name)], check=False)
    binary = os.path.join(target, "atvloadly")
    hint = binary if os.path.exists(binary) else target
    return "已解压到 {0}。用 `{1} --help` 查看启动方式（服务默认监听 5533）".format(target, hint)


def install_exe(asset, workdir, proxies=None, log=print):
    """Windows：下载 setup.exe 并静默安装。"""
    exe = os.path.join(workdir, asset["name"])
    log("下载 Windows 安装包...")
    download_with_fallback(asset["browser_download_url"], exe, proxies, log=log)
    log("静默安装（可能需要管理员权限）...")
    _run([exe, "/S"])
    return "Windows 安装程序已执行，请手动启动 atvloadly（默认监听 5533）"


def install_docker(log=print):
    """通用兜底：Docker 部署。"""
    if not _has("docker"):
        raise RuntimeError("未检测到 docker，无法用 Docker 安装")
    data_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "atvloadly-data")
    os.makedirs(data_dir, exist_ok=True)
    log("拉取镜像 " + DOCKER_IMAGE + "...")
    _run(["docker", "pull", DOCKER_IMAGE])
    log("启动容器（端口 5533:80）...")
    _run([
        "docker", "run", "--security-opt", "seccomp:unconfined", "-d",
        "--name=atvloadly", "--restart=always", "-p", "5533:80",
        "-v", data_dir + ":/data",
        "-v", "/var/run/dbus:/run/dbus",
        "-v", "/var/run/avahi-daemon:/run/avahi-daemon",
        DOCKER_IMAGE,
    ], check=False)
    return "Docker 容器已启动，服务地址 http://127.0.0.1:5533"


def wait_healthy(base_url, timeout=90, interval=3, log=print):
    """轮询 /healthcheck 直到可达。返回 (ok, detail)。"""
    url = base_url.rstrip("/") + "/healthcheck"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=4) as resp:
                resp.read(256)
            return True, "服务已就绪"
        except Exception:  # noqa: BLE001
            time.sleep(interval)
    return False, "等待 {0}s 后服务仍未就绪，可能需要手动启动".format(timeout)


def plan_install(method=None, prefer_brew=True):
    """生成安装计划（只读，不发网络请求），供 --dry-run / agent 预览。"""
    os_key, arch = detect_platform()
    release = None
    asset = None
    chosen = method
    try:
        release = fetch_latest_release()
        assets = release.get("assets", [])
        asset, auto_method = pick_asset(assets, os_key, arch, prefer_brew=prefer_brew)
        if not chosen:
            chosen = auto_method
            if os_key == "darwin" and prefer_brew and not _has("brew"):
                chosen = "dmg"
    except Exception as exc:  # noqa: BLE001 — 网络失败也要给出计划
        auto_method = "docker" if os_key != "darwin" else ("brew" if _has("brew") else "dmg")
        chosen = chosen or auto_method
        release = {"tag_name": "(获取失败: {0})".format(str(exc)[:80]), "assets": []}

    return {
        "platform": "{0}/{1}".format(os_key, arch),
        "latest_version": release.get("tag_name"),
        "method": chosen,
        "asset": asset["name"] if asset else None,
        "asset_url": asset["browser_download_url"] if asset else None,
        "proxies": get_proxies(),
        "has_brew": _has("brew") if os_key == "darwin" else False,
        "has_docker": _has("docker"),
    }


def do_install(method=None, prefer_brew=True, proxies=None, log=print):
    """执行安装，返回结构化结果。真实写操作，调用方应确保已获用户授权。"""
    os_key, arch = detect_platform()
    plan = plan_install(method=method, prefer_brew=prefer_brew)
    chosen = plan["method"]
    log("平台：{0}，最新版本：{1}，安装方式：{2}".format(
        plan["platform"], plan["latest_version"], chosen))

    workdir = os.path.join(tempfile.gettempdir(), "atvloadly-skill-download")
    os.makedirs(workdir, exist_ok=True)

    # 取资产（download 需要 URL；brew/docker 不需要）
    asset = None
    try:
        release = fetch_latest_release()
        asset, _ = pick_asset(release.get("assets", []), os_key, arch, prefer_brew=(chosen == "brew"))
    except Exception:  # noqa: BLE001
        pass
    if chosen in ("dmg", "tar", "exe") and asset is None:
        raise RuntimeError("无法确定安装包下载地址（GitHub API 不可达且无本地资产信息）")

    if chosen == "brew":
        msg = install_brew()
    elif chosen == "dmg":
        msg = install_dmg(asset, workdir, proxies=proxies, log=log)
    elif chosen == "tar":
        msg = install_tar(asset, workdir, proxies=proxies, log=log)
    elif chosen == "exe":
        msg = install_exe(asset, workdir, proxies=proxies, log=log)
    elif chosen == "docker":
        msg = install_docker(log=log)
    else:
        raise RuntimeError("未知安装方式：" + str(chosen))

    base_url = DEFAULT_BASE_URL
    if chosen == "docker":
        ok, detail = wait_healthy(base_url, timeout=60, log=log)
    else:
        ok, detail = wait_healthy(base_url, timeout=45, log=log)

    if ok:
        save_config({
            "base_url": base_url,
            "source": "auto-install",
            "install_method": chosen,
            "installed_version": plan["latest_version"],
        })
    return {
        "installed": True,
        "method": chosen,
        "version": plan["latest_version"],
        "detail": msg,
        "base_url": base_url,
        "healthy": ok,
        "health_detail": detail,
        "config_path": config_path(),
    }


if __name__ == "__main__":
    print(json.dumps(plan_install(), ensure_ascii=False, indent=2))
