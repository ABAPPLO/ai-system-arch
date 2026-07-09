#!/usr/bin/env bash
# 装 kind / kubectl / kustomize 到 ~/.local/bin（无 sudo）。已存在则跳过。
set -euo pipefail
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
KIND_VER=v0.24.0
KCTL_VER=v1.31.0
KUST_VER=v5.5.0
ARCH=amd64

if ! command -v kind >/dev/null 2>&1; then
  curl -sSL "https://kind.sigs.k8s.io/dl/${KIND_VER}/kind-linux-${ARCH}" -o "$BIN/kind"
  chmod +x "$BIN/kind"
fi
if ! command -v kubectl >/dev/null 2>&1; then
  curl -sSL "https://dl.k8s.io/release/${KCTL_VER}/bin/linux/${ARCH}/kubectl" -o "$BIN/kubectl"
  chmod +x "$BIN/kubectl"
fi
if ! command -v kustomize >/dev/null 2>&1; then
  curl -sSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUST_VER}/kustomize_${KUST_VER}_linux_${ARCH}.tar.gz" \
    | tar -xz -C "$BIN"
fi
echo "installed:"; kind version; kubectl version --client; kustomize version
