#!/usr/bin/env bash
# ==============================================================================
# Script: bnk-install-uninstall.sh
# Purpose: Interactive install and uninstall for F5 BIG-IP Next for Kubernetes (BNK)
# Options: install, uninstall
# ==============================================================================

# Re-exec under bash when started by a POSIX shell (e.g. `sh script.sh`),
# since this script uses bash-only features (arrays, printf -v, [[ ]], ERR trap).
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ANSI Color Codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Helper Logging Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    CURRENT_STEP="$1"
    echo -e "\n${CYAN}${BOLD}>>> $1 <<<${NC}\n"
}

# Tracks the current step so the ERR trap can report where a failure occurred
CURRENT_STEP="Initialization"

# Global error handler: logs the failing step and aborts the script
on_error() {
    local exit_code=$?
    local line_no="${1:-?}"
    echo ""
    log_error "Operation failed during: ${CURRENT_STEP}"
    log_error "Command exited with status ${exit_code} (line ${line_no})."
    log_error "Aborting F5 BNK operation. Review the logs above to resolve the issue."
    exit "$exit_code"
}
trap 'on_error $LINENO' ERR

prompt_file_path() {
    local prompt_msg="$1"
    local default_path="$2"
    local var_name="$3"
    local input_path=""

    while true; do
        if [ -n "$default_path" ]; then
            read -r -p "$(echo -e "${BOLD}${prompt_msg}${NC} [default: ${default_path}]: ")" input_path
            input_path="${input_path:-$default_path}"
        else
            read -r -p "$(echo -e "${BOLD}${prompt_msg}:${NC} ")" input_path
        fi

        # Expand tilde if present
        input_path="${input_path/#\~/$HOME}"

        if [ -f "$input_path" ]; then
            printf -v "$var_name" '%s' "$input_path"
            break
        elif [ -f "${SCRIPT_DIR}/$input_path" ]; then
            printf -v "$var_name" '%s' "${SCRIPT_DIR}/$input_path"
            break
        else
            log_error "File not found: '$input_path'. Please enter a valid file path."
        fi
    done
}

# Configuration file (YAML) that provides non-interactive install inputs
CONFIG_FILE="${CONFIG_FILE:-config.yaml}"

# Read a top-level scalar key from the YAML config (yq preferred, python3 fallback)
cfg_get() {
    local key="$1"
    local val=""
    if command -v yq >/dev/null 2>&1; then
        val=$(yq e ".\"$key\" // \"\"" "$CONFIG_FILE" 2>/dev/null) \
            || val=$(yq -r ".\"$key\" // \"\"" "$CONFIG_FILE" 2>/dev/null) \
            || val=""
    fi
    if [ -z "$val" ] || [ "$val" = "null" ]; then
        if command -v python3 >/dev/null 2>&1; then
            val=$(CFG="$CONFIG_FILE" KEY="$key" python3 -c '
import os, yaml
try:
    with open(os.environ["CFG"]) as f:
        d = yaml.safe_load(f) or {}
    v = d.get(os.environ["KEY"], "")
    print("" if v is None else v)
except Exception:
    print("")
' 2>/dev/null || true)
        fi
    fi
    [ "$val" = "null" ] && val=""
    printf '%s' "$val"
}

# Resolve a file across common locations without prompting; sets the named var
resolve_file() {
    local in="$1"
    local var="$2"
    in="${in/#\~/$HOME}"
    if [ -f "$in" ]; then
        printf -v "$var" '%s' "$in"; return 0
    elif [ -f "${SCRIPT_DIR}/$in" ]; then
        printf -v "$var" '%s' "${SCRIPT_DIR}/$in"; return 0
    fi
    return 1
}

# ==============================================================================
# INSTALL FUNCTION
# ==============================================================================
do_install() {
    log_step "Starting F5 BNK Installation"

    # Step 0: Load and validate the non-interactive configuration file
    log_step "Step 0: Loading Configuration"
    if [ ! -f "$CONFIG_FILE" ]; then
        if [ -f "${SCRIPT_DIR}/$CONFIG_FILE" ]; then
            CONFIG_FILE="${SCRIPT_DIR}/$CONFIG_FILE"
        else
            log_error "Configuration file '$CONFIG_FILE' not found. Provide a config.yaml (see --help)."
            exit 1
        fi
    fi
    if ! command -v yq >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
        log_error "Neither 'yq' nor 'python3' is available to parse $CONFIG_FILE."
        exit 1
    fi
    log_info "Using configuration file: $CONFIG_FILE"

    # Step 1: f5-bigip-k8s-manifest version (from config)
    log_step "Step 1: Manifest Version Input"
    local manifest_version=""
    manifest_version="$(cfg_get 'f5-bigip-k8s-manifest' | xargs)"
    manifest_version="${manifest_version:-2.4.0}"
    log_info "Target Manifest Version: $manifest_version"

    # Step 2: FAR service account keypath (from config)
    log_step "Step 2: FAR Service Account Keypath Input"
    local far_key_path=""
    far_key_path="$(cfg_get 'far-account-key')"
    far_key_path="${far_key_path:-cne_pull_64.json}"
    if ! resolve_file "$far_key_path" far_key_path; then
        log_error "FAR service account key '$far_key_path' not found."
        exit 1
    fi
    log_info "FAR key path: $far_key_path"

    # Step 3: FAR login to repo.f5.com (helm)
    log_step "Step 3: Performing FAR Login to repo.f5.com (Helm)"
    # log_info "Logging in to repo.f5.com via docker..."
    # cat "$far_key_path" | docker login -u _json_key_base64 --password-stdin repo.f5.com
    log_info "Logging in to repo.f5.com via helm registry..."
    cat "$far_key_path" | helm registry login -u _json_key_base64 --password-stdin repo.f5.com
    log_success "FAR login to repo.f5.com succeeded."

    # Step 4: Helm pull f5-bigip-k8s-manifest
    log_step "Step 4: Pulling f5-bigip-k8s-manifest via Helm"
    local manifest_chart="oci://repo.f5.com/release/f5-bigip-k8s-manifest"
    log_info "Executing: helm pull $manifest_chart --version $manifest_version"
    helm pull "$manifest_chart" --version "$manifest_version"

    local manifest_tgz="f5-bigip-k8s-manifest-${manifest_version}.tgz"
    if [ ! -f "$manifest_tgz" ]; then
        # Search for any recently pulled manifest tarball
        manifest_tgz=$(ls -t f5-bigip-k8s-manifest*.tgz 2>/dev/null | head -n1)
    fi
    if [ ! -f "$manifest_tgz" ]; then
        log_error "Could not locate downloaded manifest tgz file for version $manifest_version."
        exit 1
    fi
    log_success "Downloaded manifest tarball: $manifest_tgz"

    # Step 5: Install cert-manager with jetstack (version from config)
    local cm_version=""
    cm_version="$(cfg_get 'cert-manager-version')"
    cm_version="${cm_version:-1.16.1}"
    case "$cm_version" in v*) : ;; *) cm_version="v${cm_version}" ;; esac
    log_step "Step 5: Installing cert-manager (Jetstack ${cm_version})"
    log_info "Adding & updating Jetstack Helm repository..."
    helm repo add jetstack https://charts.jetstack.io --force-update || true
    helm repo update jetstack || true

    log_info "Installing cert-manager ${cm_version}..."
    helm upgrade cert-manager jetstack/cert-manager \
        --install \
        --namespace cert-manager \
        --create-namespace \
        --version "$cm_version" \
        --set crds.enabled=true \
        --set featureGates=ServerSideApply=true
    log_success "cert-manager Helm chart deployed."

    log_info "Waiting for cert-manager deployments rollout..."
    kubectl rollout status deployment/cert-manager -n cert-manager --timeout=180s || true
    kubectl rollout status deployment/cert-manager-cainjector -n cert-manager --timeout=180s || true
    kubectl rollout status deployment/cert-manager-webhook -n cert-manager --timeout=180s || true

    log_info "Waiting for all pods in cert-manager namespace to be Ready..."
    kubectl wait --for=condition=Ready pods --all -n cert-manager --timeout=180s
    log_success "cert-manager pods and replicas are ready."

    # Step 6: Apply cluster issuer and wait for it (generated dynamically; name from config)
    log_step "Step 6: Cluster Issuer Configuration"
    local ci_name=""
    ci_name="$(cfg_get 'cert-mgr-clusterissuer-name')"
    ci_name="${ci_name:-ext-ca-cluster-issuer}"
    local cluster_issuer_file="cluster-issuer.generated.yaml"
    cat <<EOF > "$cluster_issuer_file"
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned-cluster-issuer
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: ext-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: ext-ca
  secretName: ext-ca
  issuerRef:
    name: selfsigned-cluster-issuer
    kind: ClusterIssuer
    group: cert-manager.io
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: $ci_name
spec:
  ca:
    secretName: ext-ca
EOF
    log_success "Generated $cluster_issuer_file (ClusterIssuer: $ci_name)"

    log_info "Applying ClusterIssuer from: $cluster_issuer_file"
    # Webhook may need a short period to serve certificate requests
    sleep 5
    local applied_ci=false
    for attempt in $(seq 1 10); do
        if kubectl apply -f "$cluster_issuer_file"; then
            applied_ci=true
            break
        fi
        log_warn "cert-manager webhook not yet accepting requests. Retrying in 10s (attempt $attempt/10)..."
        sleep 10
    done

    if [ "$applied_ci" = false ]; then
        log_error "Failed to apply ClusterIssuer from $cluster_issuer_file"
        exit 1
    fi
    log_success "ClusterIssuer applied."

    # Wait for cluster issuer is True
    log_info "Waiting for ClusterIssuer Status to be True"
    local ci_ok=false
    for i in $(seq 1 30); do
        if [ -n "$ci_name" ]; then
            local ci_ready
            ci_ready=$(kubectl get clusterissuer "$ci_name" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
            if [ "$ci_ready" = "True" ]; then
                log_success "ClusterIssuer '$ci_name' status is True."
                ci_ok=true
                break
            fi
            log_info "Waiting for ClusterIssuer '$ci_name' status=True (current: ${ci_ready:-unknown})... ($i/30)"
            sleep 5
            continue
        fi

        local ready_ci_count=0
        local total_ci_count=0
        local ci_statuses
        ci_statuses=$(kubectl get clusterissuers -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null || true)
        ci_statuses=$(echo "$ci_statuses" | sed '/^\s*$/d')

        if [ -n "$ci_statuses" ]; then
            total_ci_count=$(echo "$ci_statuses" | grep -c . || true)
            ready_ci_count=$(echo "$ci_statuses" | grep -cx "True" || true)
        fi
        total_ci_count=$(echo "$total_ci_count" | tr -d '[:space:]')
        total_ci_count="${total_ci_count:-0}"
        ready_ci_count=$(echo "$ready_ci_count" | tr -d '[:space:]')
        ready_ci_count="${ready_ci_count:-0}"

        if [[ "$total_ci_count" -gt 0 && "$ready_ci_count" -eq "$total_ci_count" ]]; then
            log_success "ClusterIssuer status is True ($ready_ci_count/$total_ci_count ready)."
            ci_ok=true
            break
        fi
        log_info "Waiting for ClusterIssuer status=True (Ready: $ready_ci_count/$total_ci_count)... ($i/30)"
        sleep 5
    done

    if [ "$ci_ok" != true ]; then
        log_error "ClusterIssuer did not reach Ready=True within the expected time."
        exit 1
    fi

    # Step 7: Create namespaces - f5-bnk and f5-utils
    log_step "Step 7: Creating Namespaces (f5-bnk, f5-utils)"
    kubectl create ns f5-bnk 2>/dev/null || kubectl get ns f5-bnk >/dev/null 2>&1
    kubectl create ns f5-utils 2>/dev/null || kubectl get ns f5-utils >/dev/null 2>&1
    log_success "Namespaces f5-bnk and f5-utils verified."

    # Step 8: Apply SCC rule for FLO in f5-bnk namespace
    log_step "Step 8: Applying SCC Rules"
    if command -v oc >/dev/null 2>&1; then
        log_info "Applying OpenShift SCC rules..."
        oc adm policy add-scc-to-user privileged -n f5-bnk -z flo-f5-lifecycle-operator 2>/dev/null || true
        oc adm policy add-scc-to-user privileged -n f5-bnk -z default 2>/dev/null || true
        log_success "SCC rules applied."
    else
        log_warn "oc command not found; skipping SCC configuration (standard K8s environment)."
    fi

    # Step 9: Generate the FAR image pull secret and apply it in f5-bnk and f5-utils
    log_step "Step 9: FAR Secret Input and Application"
    local far_secret_name=""
    far_secret_name="$(cfg_get 'far-secret-name')"
    far_secret_name="${far_secret_name:-far-secret}"
    local far_secret_file="far-secret.generated.yaml"
    log_info "Generating image pull secret '$far_secret_name' from FAR key '$far_key_path'..."
    local sec_key sec_b64 cfg_json
    sec_key=$(cat "$far_key_path")
    sec_b64=$(echo -n "_json_key_base64:${sec_key}" | base64 -w 0)
    cfg_json=$(echo -n "{\"auths\": {\"repo.f5.com\": {\"auth\": \"$sec_b64\"}}}" | base64 -w 0)
    cat <<EOF > "$far_secret_file"
apiVersion: v1
kind: Secret
metadata:
  name: $far_secret_name
data:
  .dockerconfigjson: $cfg_json
type: kubernetes.io/dockerconfigjson
EOF
    log_success "Generated $far_secret_file (secret: $far_secret_name)"

    log_info "Applying $far_secret_file to f5-bnk and f5-utils namespaces..."
    kubectl -n f5-bnk apply -f "$far_secret_file"
    kubectl -n f5-utils apply -f "$far_secret_file"
    log_success "FAR secret '$far_secret_name' applied to f5-bnk and f5-utils."

    
    # Step 10: Extract downloaded manifest and find f5-lifecycle-operator chart version
    log_step "Step 10: Extracting Manifest & Detecting f5-lifecycle-operator Version"
    local extract_dir="f5-bigip-k8s-manifest-${manifest_version}"
    mkdir -p "$extract_dir"
    tar -zxf "$manifest_tgz" -C "$extract_dir" --strip-components=1 2>/dev/null || tar -zxf "$manifest_tgz" -C "$extract_dir"
    log_info "Extracted manifest to directory: $extract_dir"

    local flo_version=""
    # Try finding version using python PyYAML
    if command -v python3 >/dev/null 2>&1; then
        flo_version=$(python3 -c "
import yaml, glob, sys
files = glob.glob('$extract_dir/**/*.yaml', recursive=True) + glob.glob('$extract_dir/*.yaml')
for yfile in files:
    try:
        with open(yfile, 'r') as f:
            data = yaml.safe_load(f)
            if not isinstance(data, dict): continue
            for rel in data.get('releases', []):
                for chart in rel.get('helm_charts', []):
                    if 'f5-lifecycle-operator' in chart.get('name', ''):
                        print(chart.get('version', ''))
                        sys.exit(0)
    except Exception:
        pass
" 2>/dev/null || true)
    fi

    # Fallback to awk/grep
    if [ -z "$flo_version" ]; then
        flo_version=$(awk '/charts\/f5-lifecycle-operator/{getline; print $2}' "$extract_dir"/*.yaml 2>/dev/null | head -n1 | tr -d '"'\'' ' || true)
    fi

    if [ -n "$flo_version" ]; then
        log_success "Found f5-lifecycle-operator chart version: $flo_version"
    else
        log_error "Could not automatically determine f5-lifecycle-operator chart version from manifest."
        exit 1
    fi
    log_info "Using f5-lifecycle-operator chart version: $flo_version"


    # Step 11: Helm install FLO (values generated dynamically from config)
    log_step "Step 11: Helm Install FLO (f5-lifecycle-operator)"
    local flo_values_file="flo-values.generated.yaml"
    local flo_issuer="${ci_name:-ext-ca-cluster-issuer}"
    local flo_pull_secret="${far_secret_name:-far-secret}"
    cat <<EOF > "$flo_values_file"
global:
  imagePullSecrets:
    - name: $flo_pull_secret
  certmgr:
    clusterIssuer: $flo_issuer
rbac:
  create: true
containerPlatform: IBM
crds:
  keep: false

namespace: f5-bnk
sharedComponentNamespace: f5-utils
image:
  repository: repo.f5.com/images
  pullPolicy: IfNotPresent
EOF
    log_success "Generated $flo_values_file (pullSecret: $flo_pull_secret, clusterIssuer: $flo_issuer)"

    log_info "Installing FLO chart (version: $flo_version) in namespace f5-bnk..."
    helm upgrade flo "oci://repo.f5.com/charts/f5-lifecycle-operator" \
        --version "$flo_version" \
        -n f5-bnk \
        -f "$flo_values_file" \
        --install
    log_success "FLO Helm install/upgrade executed."

    log_info "Waiting for FLO controller pod to reach Ready condition..."
    kubectl wait --for=condition=Ready pods -l app.kubernetes.io/name=f5-lifecycle-operator -n f5-bnk --timeout=300s || true

    local flo_ok=false
    for i in $(seq 1 30); do
        local flo_phase
        flo_phase=$(kubectl get pods -l app.kubernetes.io/name=f5-lifecycle-operator -n f5-bnk -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "Unknown")
        if [ "$flo_phase" = "Running" ]; then
            log_success "FLO pod is in Running state."
            flo_ok=true
            break
        fi
        log_info "Waiting for FLO pod phase=Running (current: $flo_phase)... ($i/30)"
        sleep 5
    done

    if [ "$flo_ok" != true ]; then
        log_error "FLO controller pod did not reach Running state within the expected time."
        exit 1
    fi

    # Network Attachment Definition (generated dynamically; name from config)
    local nad_name nad_ipvlan_file
    nad_name="$(cfg_get 'nad-name')"
    nad_name="${nad_name:-ens3-ipvlan-l2}"
    nad_ipvlan_file="nad-ipvlan.generated.yaml"
    cat <<EOF > "$nad_ipvlan_file"
apiVersion: "k8s.cni.cncf.io/v1"
kind: NetworkAttachmentDefinition
metadata:
  name: $nad_name
  namespace: f5-bnk
spec:
  config: '{
      "cniVersion": "0.3.1",
      "type": "ipvlan",
      "master": "ens3",
      "mode": "l2",
      "ipam": {
        "type": "static",
        "addresses": [
          {
            "address": "10.10.1.1/24"
          }
        ]
      }
    }'
EOF
    log_success "Generated $nad_ipvlan_file (NAD: $nad_name)"
    log_info "Applying $nad_ipvlan_file in namespace f5-bnk..."
    kubectl apply -f "$nad_ipvlan_file" -n f5-bnk
    log_success "Network Attachment Definition ($nad_name) applied in namespace f5-bnk."

    # Step 12: Create External BIG-IP Controller(CIS) secret from config
    log_step "Step 12: External BIG-IP Controller(CIS) Secret Input & Application in f5-bnk"
    local cis_ip cis_port cis_user cis_pass cis_url
    cis_ip="$(cfg_get 'gtm-bigip-ip')"
    cis_port="$(cfg_get 'gtm-bigip-port')"
    cis_user="$(cfg_get 'gtm-bigip-username')"
    cis_pass="$(cfg_get 'gtm-bigip-password')"
    cis_user="${cis_user:-admin}"
    if [ -z "$cis_ip" ] || [ -z "$cis_pass" ]; then
        log_error "CIS BIG-IP credentials missing in config (gtm_bigip_ip / gtm_bigip_password)."
        exit 1
    fi
    if [ -n "$cis_port" ]; then
        cis_url="${cis_ip}:${cis_port}"
    else
        cis_url="$cis_ip"
    fi

    log_info "Creating CIS secret 'f5-bigip-ctlr-login' for BIG-IP $cis_url in namespace f5-bnk..."
    kubectl delete secret f5-bigip-ctlr-login -n f5-bnk --ignore-not-found 2>/dev/null || true
    kubectl create secret generic f5-bigip-ctlr-login \
        --from-literal=username="$cis_user" \
        --from-literal=password="$cis_pass" \
        --from-literal=url="$cis_url" \
        -n f5-bnk
    log_success "External BIG-IP Controller(CIS) secret applied in namespace f5-bnk."

    # Step 13: Apply CNEInstance CR (path from config) in f5-bnk namespace
    log_step "Step 13: CNEInstance CR Input & Application"
    local cneinstance_file=""
    cneinstance_file="$(cfg_get 'cneinstance-cr')"
    cneinstance_file="${cneinstance_file:-cneinstance-cr.yaml}"
    if ! resolve_file "$cneinstance_file" cneinstance_file; then
        log_error "CNEInstance CR file '$cneinstance_file' not found."
        exit 1
    fi

    log_info "Applying $cneinstance_file in namespace f5-bnk..."
    kubectl apply -f "$cneinstance_file" -n f5-bnk
    log_success "CNEInstance CR applied."

    # Step 14: Wait for all pods to be in running state in f5-bnk and f5-utils namespace
    log_step "Step 14: Waiting for All Pods to be in Running State (f5-bnk & f5-utils)"
    log_info "Waiting for crd-installer Job in f5-utils..."
    local crd_ok=false
    for i in $(seq 1 60); do
        if kubectl get pods -n f5-utils --no-headers 2>/dev/null | grep crd-installer | grep -qE "Completed|Succeeded"; then
            log_success "crd-installer job completed."
            crd_ok=true
            break
        fi
        log_info "Waiting for crd-installer to complete... ($i/60)"
        sleep 10
    done

    if [ "$crd_ok" != true ]; then
        log_error "crd-installer job did not complete within the expected time."
        exit 1
    fi

    log_info "Waiting for pods in f5-bnk and f5-utils to reach Running state..."
    local pods_ok=false
    for i in $(seq 1 60); do
        local not_running_bnk
        not_running_bnk=$(kubectl get pods -n f5-bnk --no-headers 2>/dev/null | grep -vE "Running|Completed|Succeeded" || true)
        local not_running_utils
        not_running_utils=$(kubectl get pods -n f5-utils --no-headers 2>/dev/null | grep -vE "Running|Completed|Succeeded" || true)

        local count_bnk=0
        local count_utils=0
        local pods_bnk
        pods_bnk=$(kubectl get pods -n f5-bnk --no-headers 2>/dev/null || true)
        pods_bnk=$(echo "$pods_bnk" | sed '/^\s*$/d')
        [ -n "$pods_bnk" ] && count_bnk=$(echo "$pods_bnk" | grep -c . || true)
        count_bnk=$(echo "$count_bnk" | tr -d '[:space:]')
        count_bnk="${count_bnk:-0}"

        local pods_utils
        pods_utils=$(kubectl get pods -n f5-utils --no-headers 2>/dev/null || true)
        pods_utils=$(echo "$pods_utils" | sed '/^\s*$/d')
        [ -n "$pods_utils" ] && count_utils=$(echo "$pods_utils" | grep -c . || true)
        count_utils=$(echo "$count_utils" | tr -d '[:space:]')
        count_utils="${count_utils:-0}"

        if [[ "$count_bnk" -gt 0 && "$count_utils" -gt 0 && -z "$not_running_bnk" && -z "$not_running_utils" ]]; then
            log_success "All pods in f5-bnk and f5-utils are in Running/Completed state."
            pods_ok=true
            break
        fi
        log_info "Waiting for pods in f5-bnk and f5-utils to stabilize ($i/60)..."
        sleep 10
    done

    if [ "$pods_ok" != true ]; then
        log_error "Pods in f5-bnk/f5-utils did not reach Running state within the expected time."
        kubectl get pods -n f5-bnk 2>/dev/null || true
        kubectl get pods -n f5-utils 2>/dev/null || true
        exit 1
    fi

    # Step 15: Generate License CR from the JWT token (config) and apply in f5-utils
    log_step "Step 15: License CR Input & Application"
    local jwt_token=""
    jwt_token="$(cfg_get 'jwt')"
    # Allow referencing an environment variable (e.g. jwt: "$JWT")
    if [[ "$jwt_token" == \$* ]]; then
        local _jv="${jwt_token#\$}"; _jv="${_jv#\{}"; _jv="${_jv%\}}"
        jwt_token="${!_jv:-}"
    fi
    if [ -z "$jwt_token" ]; then
        log_error "License JWT token missing. Set 'jwt' in $CONFIG_FILE (or via referenced env var)."
        exit 1
    fi

    local license_file="license-cr.generated.yaml"
    cat <<EOF > "$license_file"
apiVersion: k8s.f5net.com/v1
kind: License
metadata:
  name: bnk-license
  namespace: f5-utils
spec:
  jwt: "$jwt_token"
  operationMode: "connected"
EOF
    log_success "Generated $license_file"

    log_info "Applying $license_file in namespace f5-utils..."
    kubectl apply -f "$license_file" -n f5-utils
    log_success "License CR applied."

    # Step 16: Wait for bnk-license to Active
    log_step "Step 16: Waiting for bnk-license to become Active"
    local lic_name
    lic_name=$(kubectl get -f "$license_file" -o jsonpath='{.metadata.name}' 2>/dev/null || echo "bnk-license")
    lic_name="${lic_name:-bnk-license}"

    local lic_ok=false
    for i in $(seq 1 30); do
        local lic_state
        lic_state=$(kubectl get licenses "$lic_name" -n f5-utils -o jsonpath='{.status.state}' 2>/dev/null || true)
        if [ "$lic_state" = "Active" ]; then
            log_success "bnk-license '$lic_name' is Active."
            lic_ok=true
            break
        fi
        log_info "Waiting for bnk-license '$lic_name' (current status: ${lic_state:-unknown})... ($i/30)"
        sleep 10
    done

    if [ "$lic_ok" != true ]; then
        log_error "bnk-license '$lic_name' did not become Active within the expected time."
        exit 1
    fi


    # Final summary display
    log_step "Installation Summary"
    echo -e "${CYAN}=== Pods in f5-bnk ===${NC}"
    kubectl get pods -n f5-bnk -o wide || true
    echo -e "\n${CYAN}=== Pods in f5-utils ===${NC}"
    kubectl get pods -n f5-utils -o wide || true
    
    log_success "F5 BNK installation completed successfully!"
}

# ==============================================================================
# UNINSTALL FUNCTION
# ==============================================================================
do_uninstall() {
    log_step "Starting F5 BNK Uninstallation"

    read -r -p "$(echo -e "${BOLD}This will cleanup BNK, Configurations and Namespaces. Do you want to proceed with Uninstallation? (y/N):${NC} ")" confirm_uninstall
    if [[ ! "$confirm_uninstall" =~ ^[Yy]$ ]]; then
        log_info "Uninstallation cancelled by user."
        exit 0
    fi

    # Helper function to clear stuck finalizers if any resource hangs
    clear_finalizers() {
        local crd_kind="$1"
        local ns="${2:-f5-bnk}"
        log_info "Checking for terminating $crd_kind in namespace $ns..."
        local items
        items=$(kubectl get "$crd_kind" -n "$ns" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
        for item in $items; do
            kubectl patch "$crd_kind" "$item" -n "$ns" --type=merge -p '{"metadata":{"finalizers":[]}}' 2>/dev/null || true
        done
    }

    # Step 1: Delete all egress gateways
    log_step "Step 1: Deleting All Egress Gateways"
    log_info "Deleting all EgressGateway CRs..."
    kubectl delete egressgateways.gateway.k8s.f5.com --all -A --timeout=60s 2>/dev/null || {
        log_warn "Timeout deleting EgressGateways. Clearing finalizers..."
        clear_finalizers egressgateways f5-bnk
        kubectl delete egressgateways.gateway.k8s.f5.com --all -A --wait=false 2>/dev/null || true
    }
    log_success "All Egress Gateways deleted."

    # Step 2: Delete all http-routes
    log_step "Step 2: Deleting All HTTP Routes"
    log_info "Deleting all HTTPRoute CRs..."
    kubectl delete httproutes.gateway.networking.k8s.io --all -A --timeout=60s 2>/dev/null || {
        kubectl delete httproutes.gateway.networking.k8s.io --all -A --wait=false 2>/dev/null || true
    }
    log_success "All HTTP Routes deleted."

    # Step 3: Delete all gateways
    log_step "Step 3: Deleting All Gateways"
    log_info "Deleting all Gateway CRs..."
    kubectl delete gateways.gateway.networking.k8s.io --all -A --timeout=60s 2>/dev/null || {
        log_warn "Timeout deleting Gateways. Clearing finalizers..."
        clear_finalizers gateways f5-bnk
        kubectl delete gateways.gateway.networking.k8s.io --all -A --wait=false 2>/dev/null || true
    }
    log_success "All Gateways deleted."

    # Step 4: Delete all gateway settings
    log_step "Step 4: Deleting All Gateway Settings"
    log_info "Deleting all GatewaySettings CRs..."
    kubectl delete gatewaysettings.gateway.k8s.f5.com --all -A --timeout=60s 2>/dev/null || {
        log_warn "Timeout deleting GatewaySettings. Clearing finalizers..."
        clear_finalizers gatewaysettings f5-bnk
        kubectl delete gatewaysettings.gateway.k8s.f5.com --all -A --wait=false 2>/dev/null || true
    }
    log_success "All Gateway Settings deleted."

    # Step 5: Delete all Infras
    log_step "Step 5: Deleting All Infras"
    log_info "Deleting all Infra CRs..."
    kubectl delete infras.gateway.k8s.f5.com --all -A --timeout=60s 2>/dev/null || {
        log_warn "Timeout deleting Infras. Clearing finalizers..."
        clear_finalizers infras f5-bnk
        kubectl delete infras.gateway.k8s.f5.com --all -A --wait=false 2>/dev/null || true
    }
    log_success "All Infras deleted."

    # Step 6: Delete all gateway class
    log_step "Step 6: Deleting All Gateway Classes"
    log_info "Deleting all GatewayClass CRs..."
    kubectl delete gatewayclasses.gateway.networking.k8s.io --all --timeout=60s 2>/dev/null || {
        kubectl delete gatewayclasses.gateway.networking.k8s.io --all --wait=false 2>/dev/null || true
    }
    log_success "All Gateway Classes deleted."

    # Step 7: Delete all ipams
    log_step "Step 7: Deleting All IPAMs"
    log_info "Deleting all IPAM CRs..."
    kubectl delete ipams.fic.f5.com --all -A --timeout=60s 2>/dev/null || {
        clear_finalizers ipams f5-bnk
        kubectl delete ipams.fic.f5.com --all -A --wait=false 2>/dev/null || true
    }
    log_success "All IPAMs deleted."

    # Step 8: Delete all ipamranges
    log_step "Step 8: Deleting All IPAM Ranges"
    log_info "Deleting all IPAMRange CRs..."
    kubectl delete ipamranges.fic.f5.com --all -A --timeout=60s 2>/dev/null || {
        clear_finalizers ipamranges f5-bnk
        kubectl delete ipamranges.fic.f5.com --all -A --wait=false 2>/dev/null || true
    }
    log_success "All IPAM Ranges deleted."

    # Step 9: Delete all licenses
    log_step "Step 9: Deleting All Licenses"
    log_info "Deleting all License CRs..."
    kubectl delete licenses.k8s.f5net.com --all -A --timeout=60s 2>/dev/null || true

    # Clean license secrets if clean script exists or manually
    log_info "Cleaning license secrets in f5-utils..."
    local secrets=(activationcontext activationmessage activationstatus csr certificate privatekey publickey certificatechain customerprovidedid telemetryreports telemetryreport digitalassetid digitalassetname digitalassetversion entitlements productname cwcstate cwcswitchstate statehistory modeofoperation jwtsecret licensekey previousreportname cpcl-config-secret cpcl-key-secret previousreportverifieddate telemetryreportsignedackresponse initialregistrationstatus licensestatus switchlicensestatus context telemetrystatus configreportsignedackresponse configreport)
    for s in "${secrets[@]}"; do
        kubectl delete secret "$s" -n f5-utils 2>/dev/null || true
    done
    log_success "All Licenses and license secrets cleaned."

    # Step 10: Delete all cneinstances
    log_step "Step 10: Deleting All CNEInstances"
    log_info "Deleting all CNEInstance CRs..."
    kubectl delete cneinstances.k8s.f5.com --all -A --timeout=60s 2>/dev/null || {
        clear_finalizers cneinstances f5-bnk
        kubectl delete cneinstances.k8s.f5.com --all -A --wait=false 2>/dev/null || true
    }

    log_info "Waiting 20s for pods to terminate..."
    sleep 20
    log_success "All CNEInstances deleted."

    # Step 11: Helm uninstall flo and delete External BIG-IP Controller(CIS) secret
    log_step "Step 11: Helm Uninstall FLO & Delete External BIG-IP Controller(CIS) Secret"
    log_info "Uninstalling FLO from namespace f5-bnk..."
    helm uninstall flo -n f5-bnk 2>/dev/null || log_warn "FLO chart not found or already uninstalled."
    log_info "Deleting External BIG-IP Controller(CIS) secret in namespace f5-bnk..."
    kubectl delete secret f5-bigip-ctlr-login -n f5-bnk --ignore-not-found 2>/dev/null || true
    log_success "FLO Helm release uninstalled and External BIG-IP Controller(CIS) secret removed."

    # Step 12: Delete cluster issuer
    log_step "Step 12: Deleting ClusterIssuer"
    log_info "Deleting ClusterIssuers..."
    if [ -f "cluster-issuer.yaml" ]; then
        kubectl delete -f cluster-issuer.yaml --timeout=60s 2>/dev/null || true
    fi
    kubectl delete clusterissuer --all --timeout=60s 2>/dev/null || true
    kubectl delete certificate ext-ca -n cert-manager 2>/dev/null || true
    log_success "ClusterIssuer deleted."

    # Step 13: Helm uninstall cert-manager
    log_step "Step 13: Helm Uninstall cert-manager"
    log_info "Uninstalling cert-manager..."
    helm uninstall cert-manager -n cert-manager 2>/dev/null || log_warn "cert-manager not found in helm."
    kubectl delete ns cert-manager --timeout=60s 2>/dev/null || true
    log_success "cert-manager uninstalled."

    # Step 14: Delete BNK Namespaces (f5-bnk, f5-utils)
    log_step "Step 14: Delete BNK Namespaces (f5-bnk & f5-utils)"
    log_info "Deleting system namespaces (f5-bnk, f5-utils)..."
    kubectl delete ns f5-utils --timeout=120s 2>/dev/null || true
    kubectl delete ns f5-bnk --timeout=120s 2>/dev/null || true
    log_success "System namespaces deleted."


    # Step 15: Optional Cleanup: F5 CRDs
    read -r -p "$(echo -e "\n${BOLD}Do you want to clean up all F5 CRDs? [y/N]:${NC} ")" cleanup_all
    if [[ "$cleanup_all" =~ ^[Yy]$ ]]; then
        log_info "Deleting F5 CRDs..."
        kubectl get crds 2>/dev/null | grep -E "k8s.f5.com|k8s.f5net.com|fic.f5.com|metrics.f5.com" | awk '{print $1}' | xargs -r kubectl delete crd || true
        log_success "CRDs cleanup completed."
    fi

    log_success "F5 BNK uninstallation completed successfully!"
}

show_usage() {
    cat <<EOF
==============================================================================
F5 BIG-IP Next for Kubernetes (BNK) Management Script
==============================================================================

Usage:
  $0 [OPTION] [CONFIG_FILE]

Options:
  install, --install      Run the non-interactive end-to-end BNK installation
  uninstall, --uninstall  Run the reverse-order BNK uninstallation
  -h, --help              Show this usage guide and exit

Configuration:
  The install reads inputs from a YAML config file (default: config.yaml).
  Override the path by passing it as the second argument or via CONFIG_FILE env:
    $0 install my-config.yaml
    CONFIG_FILE=my-config.yaml $0 install

  config.yaml keys:
    f5-bigip-k8s-manifest         Manifest chart version to pull (e.g. 2.4.0)
    far-account-key               FAR service account JSON key file
    cert-manager-version          cert-manager (Jetstack) chart version
    cert-mgr-clusterissuer-name   ClusterIssuer name to wait for Ready=True
    far-secret-name               Name of the generated image pull secret
    cneinstance-cr                CNEInstance CR manifest to apply
    nad-name                      NetworkAttachmentDefinition name (generated)
    gtm-bigip-ip / gtm-bigip-port External BIG-IP (CIS) address
    gtm-bigip-username / -password External BIG-IP (CIS) credentials
    jwt                           License JWT token (or "$VAR" env reference)

Interactive Menu:
  Run '$0' without arguments to launch the interactive selection menu.

Prerequisites:
  - Tools: kubectl, helm (v3+), docker (or podman), tar, python3 (or yq)
  - F5 Access: F5 FAR service account JSON key (e.g. cne_pull_64.json)
  - Cluster Permissions: Cluster-admin access to target Kubernetes / OpenShift cluster

Supporting Files (resolved from working directory, SCRIPT_DIR):
  - config.yaml            Non-interactive install configuration
  - cne_pull_64.json       FAR service account credentials
  - cneinstance-cr.yaml    CNEInstance Custom Resource

  Note: ClusterIssuer, FAR secret, FLO values, NetworkAttachmentDefinition, and
  License CR are generated dynamically at install time (*.generated.yaml).
==============================================================================
EOF
}

# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
main() {
    local action="${1:-}"

    # Optional second argument overrides the config file path for install
    if [ -n "${2:-}" ]; then
        CONFIG_FILE="$2"
    fi

    if [ -z "$action" ]; then
        echo -e "${CYAN}${BOLD}============================================${NC}"
        echo -e "${CYAN}${BOLD}   F5 BIG-IP Next for Kubernetes Manager    ${NC}"
        echo -e "${CYAN}${BOLD}============================================${NC}"
        echo "1) install"
        echo "2) uninstall"
        echo "3) exit"
        read -r -p "$(echo -e "${BOLD}Select an option [1-3]:${NC} ")" choice

        case "$choice" in
            1|install|Install)
                action="install"
                ;;
            2|uninstall|Uninstall)
                action="uninstall"
                ;;
            *)
                echo "Exiting."
                exit 0
                ;;
        esac
    fi

    case "$action" in
        install|--install)
            do_install
            ;;
        uninstall|--uninstall)
            do_uninstall
            ;;
        -h|--help|help)
            show_usage
            exit 0
            ;;
        *)
            log_error "Invalid option: '$action'. Valid options: install, uninstall"
            show_usage
            exit 1
            ;;
    esac
}

main "$@"
