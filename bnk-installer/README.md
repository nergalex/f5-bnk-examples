# F5 BIG-IP Next for Kubernetes (BNK) Install / Uninstall

`bnk-install-uninstall.sh` is a Bash script that performs a non-interactive,
config-driven install (and a guided uninstall) of F5 BIG-IP Next for Kubernetes
on a Kubernetes / OpenShift cluster. All inputs are read from `config.yaml`.

---

## Prerequisites

### 1. Shell
- **Bash 4.0+** — the script uses arrays, `printf -v`, `[[ ]]`, and an `ERR`
  trap. It auto re-execs under `bash` if started with a POSIX shell (`sh`).

### 2. Required CLI tools (must be on `PATH`)
| Tool | Package (Debian/Ubuntu) | Package (RHEL/CentOS) | Used for |
|------|-------------------------|-----------------------|----------|
| `kubectl` | `kubectl` | `kubectl` | All cluster operations |
| `helm` (v3+) | `helm` | `helm` | Pull manifest, install cert-manager & FLO |
| `docker` | `docker.io` / `docker-ce` | `docker-ce` | Registry login to `repo.f5.com` |
| `tar` | `tar` | `tar` | Extract the pulled manifest tarball |
| `coreutils` | `coreutils` | `coreutils` | `base64`, `cat`, `tr`, `head`, `seq`, `mkdir`, `ls` |
| `gawk` / `awk` | `gawk` | `gawk` | FLO version fallback detection |
| `sed`, `grep` | `sed`, `grep` | `sed`, `grep` | Status parsing |
| `findutils` | `findutils` | `findutils` | `xargs` (CRD cleanup) |

### 3. YAML parser (one of these)
- **`python3` with `PyYAML`** — `pip install pyyaml` (or `apt install python3-yaml`), **or**
- **`yq`** — https://github.com/mikefarah/yq

The script prefers `yq` and falls back to `python3`. At least one is required.

### 4. Optional tools
| Tool | When needed |
|------|-------------|
| `oc` (OpenShift CLI) | Only on OpenShift — applies SCC privileged rules. Skipped automatically if absent. |

### 5. Cluster access & credentials
- A reachable **Kubernetes / OpenShift cluster** with a valid `kubeconfig`
  (cluster-admin privileges).
- The **Multus CNI** must be installed (for the `NetworkAttachmentDefinition`).
- An **F5 FAR service account JSON key** (e.g. `cne_pull_64.json`) with access
  to `repo.f5.com`.
- A valid **F5 BNK License JWT**.
- Network access to `repo.f5.com` and `https://charts.jetstack.io`.

### Quick prerequisite check
```bash
for t in bash kubectl helm docker tar base64 awk sed grep xargs; do
  command -v "$t" >/dev/null && echo "OK   $t" || echo "MISS $t"
done
command -v yq >/dev/null || command -v python3 >/dev/null \
  && echo "OK   yaml parser (yq/python3)" || echo "MISS yq or python3"
```

---

## Configuration (`config.yaml`)

| Key | Description |
|-----|-------------|
| `f5-bigip-k8s-manifest` | Manifest chart version to pull (e.g. `2.4.0`) |
| `far-account-key` | FAR service account JSON key file |
| `cert-manager-version` | cert-manager (Jetstack) chart version |
| `cert-mgr-clusterissuer-name` | ClusterIssuer name to wait for (`Ready=True`) |
| `far-secret-name` | Name of the generated image pull secret |
| `cneinstance-cr` | CNEInstance CR manifest to apply in `f5-bnk` |
| `nad-name` | NetworkAttachmentDefinition name (generated) |
| `gtm-bigip-ip` / `gtm-bigip-port` | External BIG-IP (CIS) address |
| `gtm-bigip-username` / `gtm-bigip-password` | External BIG-IP (CIS) credentials |
| `jwt` | License JWT token (may reference an env var, e.g. `"$JWT"`) |

> Security note: `gtm-bigip-password` and `jwt` are stored in plaintext. Prefer
> `jwt: "$JWT"` (env reference) and restrict file permissions (`chmod 600 config.yaml`).

The following manifests are **generated dynamically** at install time (no need
to provide them): `cluster-issuer.generated.yaml`, `far-secret.generated.yaml`,
`flo-values.generated.yaml`, `nad-ipvlan.generated.yaml`, `license-cr.generated.yaml`.

---

## Usage

```bash
# Interactive menu (install / uninstall / exit)
./bnk-install-uninstall.sh

# Install using the default config.yaml
./bnk-install-uninstall.sh install

# Install using a custom config file
./bnk-install-uninstall.sh install my-config.yaml
CONFIG_FILE=my-config.yaml ./bnk-install-uninstall.sh install

# Uninstall (prompts for confirmation)
./bnk-install-uninstall.sh uninstall

# Help
./bnk-install-uninstall.sh --help
```

If the script is not executable:
```bash
chmod +x bnk-install-uninstall.sh
```

---

## What it does

**Install** (reads `config.yaml`): FAR login → pull manifest → install
cert-manager → apply ClusterIssuer → create `f5-bnk` / `f5-utils` namespaces →
apply SCC (OpenShift) → create FAR secret → detect & install FLO → apply NAD →
create CIS secret → apply CNEInstance → wait for pods → apply License → wait
for license `Active`.

**Uninstall** (reverse order, best-effort with finalizer cleanup): deletes
EgressGateways, HTTPRoutes, Gateways, GatewaySettings, Infras, GatewayClasses,
IPAMs/IPAMRanges, Licenses (+secrets), CNEInstances → uninstall FLO &
cert-manager → delete ClusterIssuer and namespaces → optional CRD cleanup.

Each step logs its status and aborts on failure with a clear error indicating
which step failed.
