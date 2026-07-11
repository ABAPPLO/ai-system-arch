#!/usr/bin/env python3
"""ArgoCD GitOps 回归 smoke（kind）。

验证：
  (a) Application apihub-kind 状态 Synced + Healthy
  (b) drift selfHeal：scale mock-backend 3（overlay 规定 1）→ 等 selfHeal → 副本自动回 1

退出码：0 OK / 1 assert fail / 2 env unavailable。
"""
import subprocess
import sys
import time

NS_ARGO = "argocd"
NS_APP = "apihub-system"
APP = "apihub-kind"
DEPLOY = "mock-backend"
SELFHEAL_WAIT_S = 90  # selfHeal 周期 + 余量


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def app_status():
    # jsonpath 必须用 shell 双引号包住（python 单引号串），否则 {a.b.c} 被 brace expansion 破坏 → 空。
    sync = sh(f"kubectl --context kind-apihub -n {NS_ARGO} get application {APP} "
              '-o jsonpath="{.status.sync.status}"').stdout.strip()
    health = sh(f"kubectl --context kind-apihub -n {NS_ARGO} get application {APP} "
                '-o jsonpath="{.status.health.status}"').stdout.strip()
    return sync, health


def replicas():
    out = sh(f"kubectl --context kind-apihub -n {NS_APP} get deploy {DEPLOY} "
             '-o jsonpath="{.spec.replicas}"').stdout.strip()
    return int(out) if out.isdigit() else None


def main():
    if sh("kubectl --context kind-apihub -n argocd get deploy argocd-server").returncode != 0:
        print("FAIL: argocd 未装（先 make argocd-setup）")
        sys.exit(2)
    if sh(f"kubectl --context kind-apihub -n {NS_ARGO} get application {APP}").returncode != 0:
        print(f"FAIL: Application {APP} 不在（先 kubectl apply -f deploy/argocd/kind.yaml）")
        sys.exit(2)

    # (a) sync 状态
    sync, health = app_status()
    print(f"(a) application sync={sync} health={health}")
    if sync != "Synced" or health != "Healthy":
        print(f"FAIL: 期望 Synced/Healthy，实际 sync={sync} health={health}")
        sys.exit(1)

    # (b) drift selfHeal
    before = replicas()
    print(f"(b) mock-backend replicas before = {before}")
    if before != 1:
        print(f"WARN: baseline 不是 1（={before}），drift 测试仍继续")

    print("  scale mock-backend → 3（制造 drift）")
    sh(f"kubectl --context kind-apihub -n {NS_APP} scale deploy {DEPLOY} --replicas=3")
    if replicas() != 3:
        print(f"FAIL: scale 到 3 失败（replicas={replicas()}）")
        sys.exit(1)

    print(f"  等 selfHeal 还原（最多 {SELFHEAL_WAIT_S}s）...")
    for _ in range(SELFHEAL_WAIT_S // 5):
        time.sleep(5)
        r = replicas()
        if r == before:
            print(f"  selfHeal OK —— replicas 回到 {r}")
            sync2, health2 = app_status()
            print(f"  application 还原后 sync={sync2} health={health2}")
            if sync2 != "Synced" or health2 != "Healthy":
                print("FAIL: drift 后 application 不再 Synced/Healthy")
                sys.exit(1)
            print("ARGOCD GITOPS OK —— sync+healthy + drift selfHeal 验证通过")
            sys.exit(0)
    print(f"FAIL: {SELFHEAL_WAIT_S}s 内 replicas 未还原到 {before}（selfHeal 未生效？）")
    sys.exit(1)


if __name__ == "__main__":
    main()
