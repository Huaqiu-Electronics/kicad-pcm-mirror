# KiCad 官方插件仓库中国镜像 —— 不可变静态快照
# 构建期不联网、启动期不修改 Manifest，只提供静态 HTTP 服务（规格书 §5.3）
FROM nginxinc/nginx-unprivileged:alpine

COPY nginx/default.conf /etc/nginx/conf.d/default.conf

# dist/ 由 scripts/sync.py 生成，构建前必须已执行同步
COPY dist/ /usr/share/nginx/html/kicad-addons/

# 缺少快照时在构建阶段就失败，避免产出空镜像
RUN test -f /usr/share/nginx/html/kicad-addons/repository.v1.json \
    && test -f /usr/share/nginx/html/kicad-addons/repository.v2.json

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
