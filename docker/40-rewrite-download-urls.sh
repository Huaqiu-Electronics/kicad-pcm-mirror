#!/bin/sh
# 运行时把清单里的构建期地址替换为 DOWNLOAD_BASE_URL。
# nginx 官方 entrypoint 会在启动 nginx 之前执行 /docker-entrypoint.d/ 下的 *.sh。
# 只改 JSON 文本，不触碰 ZIP 内容与 sha256。
set -eu

HTML_ROOT="${DOWNLOAD_HTML_ROOT:-/usr/share/nginx/html/kicad-addons}"
BAKED_BASE="https://eda.cn/kicad-addons/"
TARGET="${DOWNLOAD_BASE_URL:-}"

log() { echo "[kicad-mirror] $*"; }
die() {
    echo "[kicad-mirror] 错误: $*" >&2
    exit 1
}

if [ -z "$TARGET" ]; then
    log "未设置 DOWNLOAD_BASE_URL，保持镜像内置地址 $BAKED_BASE"
    exit 0
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
case "$TARGET" in
    http://*/ | https://*/)
        TARGET="${TARGET}kicad-addons/"
        log "DOWNLOAD_BASE_URL 未包含路径，已按 nginx 路由补全为 $TARGET"
        ;;
    */kicad-addons/) ;;
    *) log "警告: 路径不是 .../kicad-addons/，若无反向代理改写，插件下载会 404" ;;
esac

if [ "$TARGET" = "$BAKED_BASE" ]; then
    log "DOWNLOAD_BASE_URL 与镜像内置地址一致，无需替换"
    exit 0
fi

# 替换串里 & \ | 对 sed 有特殊含义，需要转义
escaped=$(printf '%s' "$TARGET" | sed 's/[\\&|]/\\&/g')

updated=0
for name in repository.v1.json repository.v2.json packages-v1.json packages.json; do
    file="$HTML_ROOT/$name"
    [ -f "$file" ] || continue
    grep -qF "$BAKED_BASE" "$file" || continue
    sed -i "s|$BAKED_BASE|$escaped|g" "$file"
    updated=$((updated + 1))
    log "已替换 $name 中的下载地址前缀"
done

if [ "$updated" -eq 0 ]; then
    log "警告: 清单中未找到 $BAKED_BASE，未做任何替换"
else
    log "运行时下载地址已切换为 $TARGET（$updated 个文件）"
fi
