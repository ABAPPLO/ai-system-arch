# APIHub Admin 前端镜像
# 多阶段构建：node 编译 → nginx serve
#
# 构建：
#   docker build -f deploy/nginx/Dockerfile -t apihub-admin:v0.1.0 .
# 推送：
#   docker tag apihub-admin:v0.1.0 registry.cn-hangzhou.aliyuncs.com/apihub/admin:v0.1.0
#   docker push registry.cn-hangzhou.aliyuncs.com/apihub/admin:v0.1.0

# ============ Stage 1: build ============
FROM node:22-alpine AS builder
WORKDIR /app

# 先拷 lockfile，利用 docker 层缓存
COPY frontend/admin/package.json frontend/admin/package-lock.json ./
RUN npm ci --no-audit --no-fund

# 再拷源码，构建
COPY frontend/admin/ ./
RUN npm run build

# ============ Stage 2: serve ============
FROM nginx:1.27-alpine

# 替换默认配置
COPY deploy/nginx/admin.conf /etc/nginx/conf.d/default.conf

# 拷贝构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# nginx 已 EXPOSE 80，无需重复
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -qO- http://localhost/healthz || exit 1
