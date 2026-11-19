# kicad-pcm-mirror

KiCad 官方插件仓库（[repository.kicad.org](https://repository.kicad.org/repository.json)）的中国镜像。

每次同步都把官方仓库的全部插件 ZIP 原样打包进一个不可变的 Docker 镜像，
镜像内由 Nginx 提供静态文件服务，对外访问地址保持稳定：

- 仓库入口：`https://eda.cn/kicad-addons/repository.json`
- 插件文件：`https://eda.cn/kicad-addons/{identifier}-{version}.zip`

插件 ZIP 保持原始字节，不重新压缩、不修改内容；仓库 JSON 只重写下载地址。

## 目录结构

```text
scripts/
  sync.py        同步入口：抓取、下载、重写、校验、输出快照
  manifest.py    解析仓库元数据，只重写 URL 字段
  download.py    并发下载、超时与有限重试
  verify.py      sha256 / 大小 / ZIP 结构校验
  validate.py    快照级校验（引用可达、sha 一致、字段零漂移）
nginx/default.conf   /kicad-addons/ 静态服务与 v1/v2 分流
tests/               离线单元测试（假上游 HTTP 服务）
Dockerfile           静态快照镜像
docker-compose.yml   本机启动
```

## 本地使用

```bash
python -m pip install -r requirements.txt
python scripts/sync.py --out dist --base-url https://eda.cn/kicad-addons/
docker compose up -d --build
curl http://localhost:8080/kicad-addons/repository.json
```

可选参数：

| 参数 | 说明 |
| --- | --- |
| `--base-url` | 对外访问的基础地址，必须以 `/` 结尾 |
| `--skip-existing` | 本地已有且 sha256 匹配的文件不重复下载 |
| `--limit N` | 仅同步前 N 个产物，用于冒烟，产物不完整、不可发布 |
| `--workers` / `--retries` / `--timeout` | 并发数与重试策略 |
| `--image-tag` | 写入 `mirror-manifest.json` 的镜像 Tag（也可用 `IMAGE_TAG`） |

## v1 / v2 兼容

KiCad 10.0+ 会在请求头中发送 `Accept: application/vnd.kicad.pcm.v2+json`，
旧版本不发送。同步程序分别抓取上游两种响应：

- `repository.v1.json` —— 旧版格式，`packages-v1.json`
- `repository.v2.json` —— 带 `schema_version: 2`，`packages.json`

Nginx 按 `Accept` 选择文件，对外仍只有一个 `/kicad-addons/repository.json`，
并返回 `Vary: Accept` 与 `Cache-Control: no-cache`，避免中间缓存混用两版内容。

## 发布

`.github/workflows/sync.yml`：

| 触发 | 行为 |
| --- | --- |
| `schedule`（每 3 天） | 全量同步、校验、构建并推送镜像 |
| `workflow_dispatch` | 手动全量同步，或 `limit>0` 冒烟；勾选 `skip_tests` 可跳过单元测试 |
| `push` 到 `main` | 只运行单元测试 |

镜像 Tag：`YYYY.MM.DD-<短提交>-r<run号>`，另推 `latest`。
生产部署请使用不可变 Tag 或 Digest，不要用 `latest`。

ACR 的 Registry 地址与用户名直接写在 workflow 中，只需配置一个密码类 Secret：

| Secret | 用途 |
| --- | --- |
| `KICAD_PCM_ACR_PASSWORD` | ACR 登录密码 |

该 Secret 可建在组织级（Organization secrets），并授权给本仓库；不要复用组织里已有的其他 ACR 凭据。

当前只推送 ACR，不再推送到 GHCR。ACR 仓库类型可按需选择公开或私有：考虑到插件本身是开源内容，
当前按公开分发，任何人可直接拉取；如需限制访问，把 ACR 仓库调整为私有即可，无需改动 workflow。

## 运行（本地 / 局域网）

镜像内的清单地址在构建期固化为 `https://eda.cn/kicad-addons/`；容器启动时
`/docker-entrypoint.d/40-rewrite-download-urls.sh` 会按 `DOWNLOAD_BASE_URL`
原地替换 `repository.v1.json`、`repository.v2.json`、`packages-v1.json`、
`packages.json` 中的该前缀，无需重新构建镜像。

```bash
docker run -d --name kicad-pcm-mirror \
  -p 8080:8080 \
  -e DOWNLOAD_BASE_URL=http://192.168.1.10:8080/kicad-addons/ \
  crpi-uk7yo7mxz74rkbcy.cn-shanghai.personal.cr.aliyuncs.com/lwj-kicad/kicad-pcm-mirror:latest
```

KiCad 中填写 `http://192.168.1.10:8080/kicad-addons/repository.json`。

- 容器内 nginx 监听 8080，`-p` 必须映射到容器的 8080（写成 `-p 8080:80` 会连接被重置）
- `DOWNLOAD_BASE_URL` 必须以 `http://` 或 `https://` 开头，结尾斜杠会自动补全；
  未设置时保留镜像内置地址；格式非法则容器启动失败并给出原因
- 修改该变量需要重建容器（`docker rm -f` 后重新 `docker run`），`docker restart` 不会重新替换
- 地址应填使用者能访问到的地址（如 `http://192.168.1.10:8080/kicad-addons/`）；
  只填到主机端口时会自动补 `/kicad-addons/`，仅本机使用时才填 `localhost`

## 校验与回滚

- 同步阶段：逐个校验 `download_sha256` 与 ZIP 结构，任一失败即中止；`download_size` 由上游作者手填、经常写错，因此只做一致性告警，不阻断发布。上游返回 404/410 的死链、以及内容与声明 sha256 不符（作者重传同版本）的版本会从镜像索引中剔除，并记录在 `mirror-manifest.json` 的 `excluded` 中。
- 快照阶段：校验每个下载地址指向镜像、文件存在且 sha256 一致、元数据除 URL 外无任何变化。
- 回滚：重新部署上一个不可变 Tag 即可，历史镜像保留在 ACR。

`dist/mirror-manifest.json` 记录同步时间、上游版本、产物清单与镜像 Tag，用于审计；
它不会通过 HTTP 对外暴露。

## 说明

- 全量快照约 1.2 GB（上游当前声明 398 个版本；失效链接会被剔除，实际发布数见 `mirror-manifest.json` 的 `published_count`）。CI 单次同步耗时取决于 GitHub 下载速度。
- 中国大陆运行时不需要访问 GitHub / GitLab，镜像内不含任何动态同步逻辑。
