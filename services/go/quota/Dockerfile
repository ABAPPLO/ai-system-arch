FROM golang:1.23-alpine AS builder
WORKDIR /app
RUN apk add --no-cache git
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /quota ./cmd/
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /quota /quota
EXPOSE 8004
ENTRYPOINT ["/quota"]
