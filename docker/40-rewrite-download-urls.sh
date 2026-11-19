#!/bin/sh
# 运行时把清单里的构建期地址替换为 DOWNLOAD_BASE_URL。
# nginx 官方 entrypoint 会在启动 nginx 之前执行 /docker-entrypoint.d/ 下的 *.sh。
# 改写 packages 文件会改变其字节，因此必须同步更新 repository 里记录的 sha256，
# 否则 KiCad 下载后会因哈希不一致而报错。ZIP 本身不修改。
set -eu

HTML_ROOT="${DOWNLOAD_HTML_ROOT:-/usr/share/nginx/html/kicad-addons}"
DEFAULT_BASE="https://www.eda.cn/kicad-addons/"

log() { echo "[kicad-mirror] $*"; }
die() {
    echo "[kicad-mirror] 错误: $*" >&2
    exit 1
}

# 构建期地址以 manifest 为准，避免镜像用其他 --base-url 构建时替换不到
BAKED_BASE=""
MANIFEST="$HTML_ROOT/mirror-manifest.json"
if [ -f "$MANIFEST" ]; then
    BAKED_BASE=$(sed -n 's/.*"base_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST" | head -n 1)
fi
if [ -z "$BAKED_BASE" ]; then
    BAKED_BASE="$DEFAULT_BASE"
fi

if [ -n "${DOWNLOAD_BASE_URL:-}" ]; then
    TARGET="$DOWNLOAD_BASE_URL"
else
    TARGET="$DEFAULT_BASE"
    log "未设置 DOWNLOAD_BASE_URL，使用默认 $TARGET"
fi

case "$TARGET" in
    http://* | https://*) ;;
    *) die "DOWNLOAD_BASE_URL 必须以 http:// 或 https:// 开头: $TARGET" ;;
esac

case "$TARGET" in
    */) ;;
    *)
        TARGET="$TARGET/"
        log "DOWNLOAD_BASE_URL 缺少结尾斜杠，已补全为 $TARGET"
        ;;
esac

# nginx 只把 /kicad-addons/ 暴露为下载路径；只给了主机地址时补全
rest="${TARGET#*://}"
host="${TARGET%%://*}://${rest%%/*}"
path="/${rest#*/}"
if [ "$rest" = "${rest%%/*}" ]; then
    path=""
fi

case "$path" in
    "" | "/")
        TARGET="${host}/kicad-addons/"
        log "DOWNLOAD_BASE_URL 未包含路径，已按 nginx 路由补全为 $TARGET"
        ;;
    */kicad-addons/) ;;
    *) log "警告: 路径 $path 不是 /kicad-addons/，若无反向代理改写，插件下载会 404" ;;
esac

if [ "$TARGET" = "$BAKED_BASE" ]; then
    log "DOWNLOAD_BASE_URL 与镜像内置地址一致，无需替换"
    exit 0
fi

# 替换串里 & \ | 对 sed 有特殊含义，需要转义
escaped=$(printf '%s' "$TARGET" | sed 's/[\\&|]/\\&/g')

rewrite_prefix() {
    if [ ! -f "$HTML_ROOT/$1" ]; then
        return 1
    fi
    if ! grep -qF "$BAKED_BASE" "$HTML_ROOT/$1"; then
        return 1
    fi
    sed -i "s|$BAKED_BASE|$escaped|g" "$HTML_ROOT/$1"
    return 0
}

updated=0
for name in packages-v1.json packages.json repository.v1.json repository.v2.json; do
    if rewrite_prefix "$name"; then
        updated=$((updated + 1))
        log "已替换 $name 中的下载地址前缀"
    fi
done

# packages 文件内容变了，repository 里的 packages.sha256 必须跟着变
sync_packages_hash() {
    repo="$HTML_ROOT/$1"
    pkg="$HTML_ROOT/$2"
    if [ ! -f "$repo" ] || [ ! -f "$pkg" ]; then
        return 0
    fi

    sum=$(sha256sum "$pkg" | cut -d' ' -f1)
    if grep -q "$sum" "$repo"; then
        return 0
    fi

    # 只改 "packages" 对象内的 sha256，避免误改 resources 的哈希
    sed -i "/\"packages\"[[:space:]]*:[[:space:]]*{/,/^[[:space:]]*}/ s/\"sha256\"[[:space:]]*:[[:space:]]*\"[0-9a-f]*\"/\"sha256\": \"$sum\"/" "$repo"
    if ! grep -q "$sum" "$repo"; then
        die "无法更新 $1 中的 packages.sha256，请检查镜像内 repository 格式"
    fi
    log "已同步 $1 中的 packages.sha256"
}

sync_packages_hash repository.v1.json packages-v1.json
sync_packages_hash repository.v2.json packages.json

if [ "$updated" -eq 0 ]; then
    log "清单已是目标地址 $TARGET，无需替换"
else
    log "运行时下载地址已切换为 $TARGET（$updated 个文件）"
fi
