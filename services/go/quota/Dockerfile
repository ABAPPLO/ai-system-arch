# Go quota 镜像。Multi-stage：builder 编译静态二进制；runtime 用 alpine
# （而非 distroless）—— deployment.yaml 的 preStop 用 /bin/sh -c "sleep 3"，
# 且 prod PG_SSL=verify-full 需要 ca-certificates；alpine 二者皆满足，体积仍 ~15MB。
# 注意 go.mod 要求 go 1.25.0 → builder 必须用 >= 1.25。
FROM golang:1.25-alpine AS builder
WORKDIR /app
RUN apk add --no-cache git
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /quota ./cmd/

FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata
COPY --from=builder /quota /quota
EXPOSE 8004
ENTRYPOINT ["/quota"]
