"""增量更新器（国内加速版）。

设计目标
--------
1. **只下变化的文件** —— 用 manifest 里的 sha256 对比本地，一致就跳过。
2. **两个版本不能搞串** —— manifest 带 `edition` 字段，和本地对不上就拒绝。
3. **不碰用户数据** —— 只管理「程序文件」白名单里的扩展名；
   config.json / state.json / 日志 / companion-memory 一律不动。
4. **国内能直连** —— 默认走 jsDelivr CDN（国内有节点），失败自动回退到
   其它镜像，最后才用 GitHub 直连。
5. **尽量快** —— 多文件并发下载 + 多源回退。

远端结构（每个仓库一个）
------------------------
GitHub Release（tag = v1.3）里附带：
    manifest.json      文件清单 + sha256 + edition
文件本体走 CDN：
    https://cdn.jsdelivr.net/gh/{user}/{repo}@{tag}/{path}

**为什么用 jsDelivr 而不是 raw.githubusercontent.com**：
GitHub raw 在国内经常超时/几十 KB/s，jsDelivr 有国内节点，同样的文件
通常是秒级。路径不变，只是换了域名前缀。
"""
import concurrent.futures
import gzip
import hashlib
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import zlib

DEFAULT_REPOS = {
    "dev": "",
    "portable": "",
}

# 允许被更新器下载/删除的文件类型。
MANAGED_SUFFIXES = {
    ".py", ".md", ".iss", ".isl", ".ico", ".cmd", ".ps1",
}
MANAGED_NAMES = {"stickers.json"}
MANAGED_DIRS = {"prompts"}

EXCLUDE_PREFIXES = (
    "_", "@", "dist/", "node_modules/", "__pycache__/", "build/",
)
EXCLUDE_PARTS = {"__pycache__", "node_modules", "dist", "build", ".git"}
EXCLUDE_NAMES = {
    # 私人文档，不公开
    "CONTINUITY.md", "HANDOVER.md", "这段对话.md",
    "letter_from_mama.md", "letter_from_mama_2.md",
    "给小鲸鱼的信.md", "给小鲸鱼的第二封信.md",
    # 发布脚本，只开发机用，不该出现在更新源里
    "publish.py", "sync_publish.py",
}

UA = "wechatauto-updater/1.1"
HTTP_TIMEOUT = 20

# ══════════════════════ 国内加速源 ══════════════════════
# 按「国内可达性 + 速度」排序，前面的失败才走后面的。
# 实测（2026-11-08，341KB 文件，国内直连环境）：
#   raw.githubusercontent.com  230 KB/s（但不一定有梯子）
#   gh-proxy.com              200 KB/s
#   gcore.jsdelivr.net        196 KB/s
#   fastly.jsdelivr.net       181 KB/s
#   ghproxy.net               179 KB/s
#   cdn.jsdelivr.net          133 KB/s
#   raw.gitmirror.com         SSL 错误，已移除
# 顺序：优先国内可达的 CDN，直连放最后兜底。
FILE_SOURCES = [
    "https://gcore.jsdelivr.net/gh/{repo}@{tag}/{path}",
    "https://gh-proxy.com/https://raw.githubusercontent.com/{repo}/{tag}/{path}",
    "https://fastly.jsdelivr.net/gh/{repo}@{tag}/{path}",
    "https://ghproxy.net/https://raw.githubusercontent.com/{repo}/{tag}/{path}",
    "https://cdn.jsdelivr.net/gh/{repo}@{tag}/{path}",
    "https://raw.githubusercontent.com/{repo}/{tag}/{path}",
]

# 查最新 release 的 API 源
API_SOURCES = [
    "https://ghproxy.net/https://api.github.com/repos/{repo}/releases/latest",
    "https://gh-proxy.com/https://api.github.com/repos/{repo}/releases/latest",
    "https://api.github.com/repos/{repo}/releases/latest",
]

# 更新说明的 API 源（按 tag 查）
API_TAG_SOURCES = [
    "https://ghproxy.net/https://api.github.com/repos/{repo}/releases/tags/{tag}",
    "https://gh-proxy.com/https://api.github.com/repos/{repo}/releases/tags/{tag}",
    "https://api.github.com/repos/{repo}/releases/tags/{tag}",
]

# 并发下载线程数
DOWNLOAD_WORKERS = 8


class UpdateError(RuntimeError):
    pass


# ────────────────────────────── 工具 ──
def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_managed(rel_path: str) -> bool:
    """这个相对路径是不是更新器该管的程序文件。"""
    rel = rel_path.replace("\\", "/")
    p = pathlib.PurePosixPath(rel)
    if any(part in EXCLUDE_PARTS for part in p.parts):
        return False
    if rel.startswith(EXCLUDE_PREFIXES):
        return False
    name = p.name
    if name in EXCLUDE_NAMES:
        return False
    if name in MANAGED_NAMES:
        return True
    if p.suffix.lower() not in MANAGED_SUFFIXES:
        return False
    if len(p.parts) == 1:
        return True
    return p.parts[0] in MANAGED_DIRS


def _get(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA,
                      "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            data = gzip.decompress(data)
        elif "deflate" in enc:
            data = zlib.decompress(data)
        return data


def _get_json_with_sources(templates: list, fmt: dict,
                           timeout: float = HTTP_TIMEOUT) -> dict:
    """按源列表依次尝试，返回第一个成功的 JSON。"""
    errors = []
    for tpl in templates:
        url = tpl.format(**fmt)
        try:
            return json.loads(_get(url, timeout).decode("utf-8"))
        except Exception as e:
            try:
                host = url.split("/")[2]
            except Exception:
                host = "?"
            errors.append("{}: {}".format(host, str(e)[:40]))
            continue
    raise UpdateError("所有源都失败：" + " | ".join(errors[:3]))


# ────────────────────────────── 检查更新 ──
def repo_of(cfg: dict, edition: str) -> str:
    upd = (cfg.get("update") or {})
    repos = upd.get("repos") or {}
    return (repos.get(edition) or DEFAULT_REPOS.get(edition) or "").strip()


def check_latest(cfg: dict, edition: str) -> dict | None:
    """查远端最新 release（走国内加速源）。

    返回 None 表示没有更新；否则
        {"version": "1.3", "tag": "v1.3", "repo": ..., "manifest_url": ...}
    """
    repo = repo_of(cfg, edition)
    if not repo:
        raise UpdateError("config.update.repos.{} 没配置仓库".format(edition))

    rel = _get_json_with_sources(API_SOURCES, {"repo": repo})

    tag = (rel.get("tag_name") or "").strip()
    version = tag.lstrip("vV")
    if not version:
        raise UpdateError("Release 没有 tag")

    cur = str(cfg.get("version") or "")
    if _ver_tuple(version) <= _ver_tuple(cur):
        return None

    manifest_url = None
    for a in (rel.get("assets") or []):
        if (a.get("name") or "") == "manifest.json":
            manifest_url = a.get("browser_download_url")
            break
    if not manifest_url:
        raise UpdateError("Release {} 里没有 manifest.json".format(tag))

    return {
        "version": version,
        "tag": tag,
        "repo": repo,
        "manifest_url": manifest_url,
    }


def _ver_tuple(v: str) -> tuple:
    parts = []
    for x in str(v).replace("v", "").split("."):
        try:
            parts.append(int(x))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


# ────────────────────────────── 计划 ──
def fetch_manifest(manifest_url: str) -> dict:
    """下载 manifest.json（Release assets 直链 + ghproxy 回退）。"""
    urls = [
        manifest_url,
        "https://ghproxy.net/" + manifest_url,
        "https://gh-proxy.com/" + manifest_url,
    ]
    errors = []
    for u in urls:
        try:
            m = json.loads(_get(u, 30).decode("utf-8"))
            if isinstance(m, dict) and "files" in m:
                return m
        except Exception as e:
            errors.append(str(e)[:40])
    raise UpdateError("manifest.json 下载失败：" + " | ".join(errors[:2]))


def plan(local_dir: pathlib.Path, manifest: dict,
         my_edition: str) -> dict:
    """对比本地和 manifest，返回要下载 / 要删除的清单。"""
    remote_edition = str(manifest.get("edition") or "")
    if remote_edition and my_edition and remote_edition != my_edition:
        raise UpdateError(
            "版本对不上：远端是 {}，本地是 {} —— 拒绝更新（防止开发版/阉割版搞串）"
            .format(remote_edition, my_edition))

    files = manifest.get("files") or {}
    to_download = []
    for rel, meta in files.items():
        rel = rel.replace("\\", "/")
        if not is_managed(rel):
            continue
        p = local_dir / rel
        want = str((meta or {}).get("sha256") or "").lower()
        if not p.exists():
            to_download.append(rel)
            continue
        if want and sha256_file(p).lower() != want:
            to_download.append(rel)

    to_delete = []
    for p in local_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(local_dir).as_posix()
        if rel.startswith("__pycache__/") or "/__pycache__/" in rel:
            continue
        if not is_managed(rel):
            continue
        if rel not in {k.replace("\\", "/") for k in files}:
            to_delete.append(rel)

    return {
        "version": manifest.get("version") or "",
        "edition": remote_edition,
        "to_download": sorted(to_download),
        "to_delete": sorted(to_delete),
        "total": len(files),
    }


# ────────────────────────────── 下载 ──
def _download_one(repo: str, tag: str, rel: str,
                  local_dir: pathlib.Path) -> tuple:
    """下载单个文件，多源回退。返回 (rel, 是否成功, 来源/错误)。"""
    errors = []
    for tpl in FILE_SOURCES:
        url = tpl.format(repo=repo, tag=tag, path=rel)
        try:
            data = _get(url, timeout=45)
            if not data:
                raise UpdateError("空内容")
            dst = local_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".new")
            tmp.write_bytes(data)
            tmp.replace(dst)
            try:
                src = url.split("/")[2]
            except Exception:
                src = "?"
            return rel, True, src
        except Exception as e:
            errors.append(str(e)[:40])
            continue
    return rel, False, " | ".join(errors[:2])


def apply_update(local_dir: pathlib.Path, repo: str, tag: str,
                 plan_result: dict, progress=None,
                 workers: int = DOWNLOAD_WORKERS) -> dict:
    """按计划并发下载 / 删除。"""
    downloaded, deleted, failed = [], [], []
    source_count = {}
    todo = plan_result["to_download"]
    n = len(todo)
    done_count = 0

    if n:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(workers, max(1, n))) as ex:
            futures = {
                ex.submit(_download_one, repo, tag, rel, local_dir): rel
                for rel in todo
            }
            for fut in concurrent.futures.as_completed(futures):
                rel, ok, info = fut.result()
                done_count += 1
                if ok:
                    downloaded.append(rel)
                    source_count[info] = source_count.get(info, 0) + 1
                else:
                    failed.append((rel, info))
                if progress:
                    progress(done_count, n, rel)

    for rel in plan_result["to_delete"]:
        p = local_dir / rel
        try:
            if p.exists():
                p.unlink()
                deleted.append(rel)
        except Exception as e:
            failed.append((rel, "删除失败 " + str(e)[:60]))

    return {
        "downloaded": downloaded,
        "deleted": deleted,
        "failed": failed,
        "sources": source_count,
    }


# ────────────────────────────── 生成 manifest（发布用）──
def build_manifest(local_dir: pathlib.Path, version: str,
                   edition: str) -> dict:
    files = {}
    for p in local_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(local_dir).as_posix()
        if "__pycache__" in rel:
            continue
        if not is_managed(rel):
            continue
        files[rel] = {
            "sha256": sha256_file(p),
            "size": p.stat().st_size,
        }
    return {
        "version": version,
        "edition": edition,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": dict(sorted(files.items())),
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) >= 2 and sys.argv[1] == "manifest":
        d = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else ".")
        ver = sys.argv[3] if len(sys.argv) > 3 else "0.0"
        ed = sys.argv[4] if len(sys.argv) > 4 else "dev"
        m = build_manifest(d, ver, ed)
        out = d / "manifest.json"
        out.write_text(json.dumps(m, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print("已生成 {}：{} 个文件".format(out, len(m["files"])))
    else:
        print("用法: python updater.py manifest <目录> <版本> <edition>")
