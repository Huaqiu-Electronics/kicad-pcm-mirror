# KiCad 官方插件仓库中国镜像 —— 不可变静态快照
# 构建期不联网；启动期仅按 DOWNLOAD_BASE_URL 重写清单里的地址前缀（规格书 §5.3）
FROM nginxinc/nginx-unprivileged:alpine

COPY nginx/default.conf /etc/nginx/conf.d/default.conf

# 启动 nginx 之前，把清单中的构建期地址替换为 DOWNLOAD_BASE_URL
COPY --chmod=755 docker/40-rewrite-download-urls.sh /docker-entrypoint.d/40-rewrite-download-urls.sh

# dist/ 由 scripts/sync.py 生成，构建前必须已执行同步
# 容器以 uid 101 运行，清单需可写，供启动脚本原地替换地址
COPY --chown=101:101 dist/ /usr/share/nginx/html/kicad-addons/

# 缺少快照时在构建阶段就失败，避免产出空镜像
RUN test -f /usr/share/nginx/html/kicad-addons/repository.v1.json \
    && test -f /usr/share/nginx/html/kicad-addons/repository.v2.json

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
