# Go quota 镜像。Multi-stage：builder 编译静态二进制；runtime 用 alpine
# （而非 distroless）—— deployment.yaml 的 preStop 用 /bin/sh -c "sleep 3"，
# 且 prod PG_SSL=verify-full 需要 ca-certificates；alpine 二者皆满足，体积仍 ~15MB。
# 注意 go.mod 要求 go 1.25.0 → builder 必须用 >= 1.25。
#
# 依赖走 vendor（go mod vendor 已提交）—— 避免构建时打 proxy.golang.org，
# 离线/受限网络环境（kind、CI runner 无外网）也能构建。go.sum 仍校验完整性。
FROM golang:1.25-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
COPY vendor/ vendor/
COPY . .
RUN CGO_ENABLED=0 go build -mod=vendor -ldflags="-s -w" -o /quota ./cmd/

FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata
COPY --from=builder /quota /quota
EXPOSE 8004
ENTRYPOINT ["/quota"]
