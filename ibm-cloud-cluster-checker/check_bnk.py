#!/usr/bin/env python3
"""
IBM Cloud & F5 BIG-IP Next for Kubernetes (BNK) Cluster Checker

A lightweight diagnostic tool with ZERO third-party Python dependencies.
Used to:
  1. List all IBM Cloud ROKS/IKS clusters (when no cluster is specified).
  2. Perform BNK pre-flight / configuration checks for a given cluster:
     - Check if BNK Infra CR (gateway.k8s.f5.com) is applied and its status.
     - Detect configured VXLAN ports (or verify a user-specified VXLAN port).
     - Inspect IBM Cloud VPC Security Groups to verify inbound UDP traffic is permitted.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple


# Terminal colors for readable output
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def colorize(text: str, color: str, use_color: bool = True) -> str:
    return f"{color}{text}{Colors.RESET}" if use_color else text


def run_cmd(
    cmd: List[str],
    check: bool = True,
    extra_env: Optional[Dict[str, str]] = None,
    timeout: int = 120,
    retries: int = 2,
) -> Tuple[int, str, str]:
    """Execute a local command and return (returncode, stdout, stderr). Automatically retries transient network errors."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    for attempt in range(retries + 1):
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                env=env,
            )
            is_transient = any(err in (proc.stderr + proc.stdout) for err in ["TLS handshake timeout", "Client.Timeout exceeded", "connection reset by peer"])
            if proc.returncode != 0 and is_transient and attempt < retries:
                time.sleep(1.5)
                continue
            if check and proc.returncode != 0:
                err_msg = proc.stderr.strip() or proc.stdout.strip() or f"exited with code {proc.returncode}"
                raise RuntimeError(f"Command '{' '.join(cmd)}' failed: {err_msg}")
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            if check and attempt < retries:
                time.sleep(1.5)
                continue
            if check:
                raise RuntimeError(f"Command '{' '.join(cmd)}' timed out after {timeout} seconds.")
            return 124, "", f"Command '{' '.join(cmd)}' timed out after {timeout} seconds."
        except FileNotFoundError:
            raise RuntimeError(f"Executable not found on PATH: {cmd[0]}")
    return 1, "", "Command failed after retries"


def get_installed_plugins() -> List[str]:
    """Return a list of installed IBM Cloud plugin names and aliases."""
    if not shutil.which("ibmcloud"):
        return []
    rc, out, _ = run_cmd(["ibmcloud", "plugin", "list", "--output", "json"], check=False)
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            names: List[str] = []
            for p in data:
                if isinstance(p, dict):
                    name = p.get("Name") or p.get("name")
                    if name:
                        names.append(name.lower())
                    for alias in p.get("Aliases") or []:
                        names.append(alias.lower())
            return names
        except json.JSONDecodeError:
            pass
    # Fallback to plain text parsing
    rc, out, _ = run_cmd(["ibmcloud", "plugin", "list"], check=False)
    plugins: List[str] = []
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("Listing") or line.startswith("Plugin Name"):
                continue
            parts = line.split()
            if parts:
                pname = parts[0].split("[")[0].strip().lower()
                plugins.append(pname)
    return plugins


def get_kube_cli() -> Optional[str]:
    """Return 'kubectl' or 'oc' if installed, otherwise None."""
    if shutil.which("kubectl"):
        return "kubectl"
    if shutil.which("oc"):
        return "oc"
    return None


def short_name(val: Optional[str], max_len: int = 15) -> str:
    """Format long Kubernetes resource names to fit terminal columns."""
    if not val:
        return "-"
    if len(val) <= max_len:
        return val
    return f"...{val[-(max_len - 3):]}"



def check_prerequisites(
    cluster_mode: bool = False,
    api_key: Optional[str] = None,
    use_color: bool = True,
    quiet_if_pass: bool = False,
) -> bool:
    """
    Verify all required CLI tools, plugins, and credentials.
    Prints a detailed diagnostic table and clear remediation commands if any item is missing.
    Returns True if all required prerequisites pass, False otherwise.
    """
    checks: List[Dict[str, Any]] = []
    remediations: List[str] = []

    # 1. Python version >= 3.8
    py_ver = sys.version_info
    py_ver_str = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"
    if py_ver >= (3, 8):
        checks.append({
            "name": "Python Environment",
            "status": "PASS",
            "detail": f"Python {py_ver_str} (>= 3.8 required)",
        })
    else:
        checks.append({
            "name": "Python Environment",
            "status": "FAIL",
            "detail": f"Python {py_ver_str} is unsupported. Python 3.8 or newer is required.",
        })
        remediations.append(
            "Upgrade Python:\n"
            "   • macOS: brew install python\n"
            "   • Linux: sudo apt update && sudo apt install -y python3\n"
            "   • Download: https://www.python.org/downloads/"
        )

    # 2. IBM Cloud CLI ('ibmcloud')
    ibmcloud_path = shutil.which("ibmcloud")
    if ibmcloud_path:
        checks.append({
            "name": "IBM Cloud CLI ('ibmcloud')",
            "status": "PASS",
            "detail": f"Found at {ibmcloud_path}",
        })
    else:
        checks.append({
            "name": "IBM Cloud CLI ('ibmcloud')",
            "status": "FAIL",
            "detail": "Binary 'ibmcloud' not found in PATH",
        })
        remediations.append(
            "Install IBM Cloud CLI:\n"
            "   • macOS (Homebrew): brew install ibmcloud-cli\n"
            "   • macOS (Installer): curl -fsSL https://clis.cloud.ibm.com/install/osx | sh\n"
            "   • Linux: curl -fsSL https://clis.cloud.ibm.com/install/linux | sh\n"
            "   • Docs: https://cloud.ibm.com/docs/cli?topic=cli-getting-started"
        )

    # 3. IBM Cloud Plugins (only checked if ibmcloud is present)
    if ibmcloud_path:
        installed_plugins = get_installed_plugins()

        # container-service (ks)
        has_ks = any(p in installed_plugins for p in ("container-service", "kubernetes-service", "ks"))
        if has_ks:
            checks.append({
                "name": "Plugin: 'container-service' (ks)",
                "status": "PASS",
                "detail": "Installed",
            })
        else:
            checks.append({
                "name": "Plugin: 'container-service' (ks)",
                "status": "FAIL",
                "detail": "Not installed (required to manage/inspect ROKS/IKS clusters)",
            })
            remediations.append(
                "Install IBM Cloud Kubernetes Service plugin:\n"
                "   • Run: ibmcloud plugin install container-service"
            )

        # vpc-infrastructure (is)
        has_is = any(p in installed_plugins for p in ("vpc-infrastructure", "infrastructure-service", "is"))
        if has_is:
            checks.append({
                "name": "Plugin: 'vpc-infrastructure' (is)",
                "status": "PASS",
                "detail": "Installed",
            })
        else:
            checks.append({
                "name": "Plugin: 'vpc-infrastructure' (is)",
                "status": "FAIL",
                "detail": "Not installed (required to query VPC Security Groups)",
            })
            remediations.append(
                "Install IBM Cloud VPC Infrastructure plugin:\n"
                "   • Run: ibmcloud plugin install vpc-infrastructure"
            )
    else:
        checks.append({
            "name": "Plugin: 'container-service' (ks)",
            "status": "FAIL",
            "detail": "Cannot check (ibmcloud CLI is missing)",
        })
        checks.append({
            "name": "Plugin: 'vpc-infrastructure' (is)",
            "status": "FAIL",
            "detail": "Cannot check (ibmcloud CLI is missing)",
        })

    # 4. Kubernetes CLI ('kubectl' or 'oc')
    kube_tool = get_kube_cli()
    if kube_tool:
        tool_path = shutil.which(kube_tool)
        checks.append({
            "name": f"Kubernetes CLI ('{kube_tool}')",
            "status": "PASS",
            "detail": f"Found at {tool_path}",
        })
    else:
        status_code = "FAIL" if cluster_mode else "WARN"
        checks.append({
            "name": "Kubernetes CLI ('kubectl' or 'oc')",
            "status": status_code,
            "detail": "Neither 'kubectl' nor 'oc' found in PATH" + (" (required for BNK checks)" if cluster_mode else " (optional for listing clusters)"),
        })
        if cluster_mode:
            remediations.append(
                "Install Kubernetes CLI ('kubectl' or 'oc'):\n"
                "   • macOS: brew install kubectl   (or: brew install openshift-cli)\n"
                "   • Linux (kubectl):\n"
                '       curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"\n'
                "       chmod +x kubectl && sudo mv kubectl /usr/local/bin/\n"
                "   • OpenShift CLI (oc): https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/"
            )

    # 5. IBM Cloud Authentication
    auth_passed = False
    auth_detail = ""
    resolved_key = api_key or os.getenv("IBMCLOUD_API_KEY")

    if resolved_key:
        auth_passed = True
        auth_detail = "API Key provided (via CLI flag or IBMCLOUD_API_KEY env var)"
    elif ibmcloud_path:
        rc, out, _ = run_cmd(["ibmcloud", "account", "show", "--output", "json"], check=False)
        if rc == 0:
            try:
                acct = json.loads(out)
                acct_name = acct.get("name") or acct.get("Name") or "Active"
                auth_passed = True
                auth_detail = f"Active session found (Account: {acct_name})"
            except Exception:
                auth_passed = True
                auth_detail = "Active session found"
        else:
            # Fallback to local session target check (handles transient network/DNS issues on account endpoint)
            rc_tgt, out_tgt, _ = run_cmd(["ibmcloud", "target"], check=False)
            if rc_tgt == 0 and "Account:" in out_tgt and "No account targeted" not in out_tgt:
                auth_passed = True
                auth_detail = "Active session found (verified via ibmcloud target)"
            else:
                auth_detail = "No active login session found"
    else:
        auth_detail = "Cannot verify (ibmcloud CLI is missing)"

    if auth_passed:
        checks.append({
            "name": "IBM Cloud Authentication",
            "status": "PASS",
            "detail": auth_detail,
        })
    else:
        checks.append({
            "name": "IBM Cloud Authentication",
            "status": "FAIL",
            "detail": auth_detail,
        })
        remediations.append(
            "Authenticate with IBM Cloud:\n"
            "   • Run interactive SSO login: ibmcloud login -sso\n"
            "   • Or provide an API Key:\n"
            "       python3 check_bnk.py --api-key <YOUR_API_KEY>\n"
            "     or:\n"
            "       export IBMCLOUD_API_KEY=\"<YOUR_API_KEY>\""
        )

    # Overall evaluation
    has_failures = any(c["status"] == "FAIL" for c in checks)
    all_ok = not has_failures

    if quiet_if_pass and all_ok:
        return True

    # Display prerequisite report
    print("\n" + "=" * 80)
    print(colorize("PRE-REQUISITE SYSTEM CHECK", Colors.BOLD, use_color))
    print("=" * 80)

    for c in checks:
        if c["status"] == "PASS":
            tag = colorize("[PASS]", Colors.GREEN, use_color)
        elif c["status"] == "WARN":
            tag = colorize("[WARN]", Colors.YELLOW, use_color)
        else:
            tag = colorize("[FAIL]", Colors.RED, use_color)
        if len(c['name']) + len(c['detail']) + 12 > 80:
            print(f"  {tag} {c['name']}")
            print(f"         Detail: {c['detail']}")
        else:
            print(f"  {tag} {c['name']:<32} : {c['detail']}")

    if remediations:
        print("\n" + "-" * 80)
        print(colorize("MISSING COMPONENTS & REMEDIATION STEPS:", Colors.RED, use_color))
        print("-" * 80)
        for idx, step in enumerate(remediations, 1):
            print(f"\n{idx}. {step}")
        print("\n" + "=" * 80 + "\n")
    else:
        print("\n" + colorize("All required components and credentials are operational.", Colors.GREEN, use_color))
        print("=" * 80 + "\n")

    return all_ok


def ensure_ibmcloud_login(api_key: Optional[str] = None) -> None:
    """Ensure active IBM Cloud login session; log in if API key is provided."""
    if not shutil.which("ibmcloud"):
        raise RuntimeError("IBM Cloud CLI ('ibmcloud') is not installed or not in PATH.")
    # Check if already authenticated
    rc, _, _ = run_cmd(["ibmcloud", "account", "show", "--output", "json"], check=False)
    if rc == 0:
        return

    # Attempt login if API key is available
    resolved_key = api_key or os.getenv("IBMCLOUD_API_KEY")
    if not resolved_key:
        raise RuntimeError(
            "IBM Cloud is not logged in, and no API key was provided.\n"
            "Please either run 'ibmcloud login' first, or provide an API key via --api-key / IBMCLOUD_API_KEY."
        )

    print("Authenticating with IBM Cloud using provided API key...")
    region = os.getenv("IBMCLOUD_REGION", "us-south")
    login_cmd = ["ibmcloud", "login", "-a", "https://cloud.ibm.com", "-r", region]
    run_cmd(login_cmd, extra_env={"IBMCLOUD_API_KEY": resolved_key})


def list_clusters() -> List[Dict[str, Any]]:
    """Query IBM Cloud for all clusters across VPC Gen2 and Classic providers."""
    ensure_ibmcloud_login()
    clusters: List[Dict[str, Any]] = []
    seen: set = set()

    for provider in ("vpc-gen2", "classic"):
        cmd = ["ibmcloud", "ks", "cluster", "ls", "-q", "--provider", provider, "--output", "json"]
        rc, out, _ = run_cmd(cmd, check=False)
        if rc != 0 or not out.strip():
            continue
        try:
            items = json.loads(out) or []
            for c in items:
                cid = c.get("id") or c.get("name")
                if cid and cid not in seen:
                    seen.add(cid)
                    clusters.append(c)
        except json.JSONDecodeError:
            pass

    return clusters


def display_clusters(clusters: List[Dict[str, Any]], use_color: bool = True) -> None:
    """Print a formatted table of all clusters."""
    if not clusters:
        print("No clusters found in this IBM Cloud account.")
        return

    print("\n" + colorize("Found Clusters in IBM Cloud:", Colors.BOLD, use_color))
    print("=" * 82)
    header = f"{'NAME':<24} {'ID':<21} {'STATE':<9} {'LOCATION':<10} {'VERSION':<7} {'NODES':<5}"
    print(colorize(header, Colors.CYAN, use_color))
    print("-" * 82)

    for c in clusters:
        name = (c.get("name") or "unknown")[:23]
        cid = (c.get("id") or "unknown")[:20]
        state = (c.get("state") or "unknown")[:8]
        loc = (c.get("location") or c.get("region") or "unknown")[:10]
        ver = (c.get("masterKubeVersion") or "unknown").split("_")[0][:7]
        workers = str(c.get("workerCount") or "-")[:5]

        if state.lower() == "normal":
            state_str = colorize(f"{state:<9}", Colors.GREEN, use_color)
        elif state.lower() == "warning":
            state_str = colorize(f"{state:<9}", Colors.YELLOW, use_color)
        else:
            state_str = colorize(f"{state:<9}", Colors.RED, use_color)

        print(f"{name:<24} {cid:<21} {state_str} {loc:<10} {ver:<7} {workers:<5}")

    print("=" * 82)
    print("\nTo inspect a specific cluster for BNK configuration, run:")
    print(colorize("  python3 check_bnk.py --cluster <cluster_name_or_id>\n", Colors.BOLD, use_color))


def get_cluster_details(cluster_name_or_id: str) -> Dict[str, Any]:
    """Retrieve full cluster details from IBM Cloud."""
    cmd = ["ibmcloud", "ks", "cluster", "get", "--cluster", cluster_name_or_id, "--output", "json"]
    rc, out, err = run_cmd(cmd, check=False)
    if rc != 0:
        raise RuntimeError(f"Failed to fetch cluster '{cluster_name_or_id}': {err or out}")
    return json.loads(out)


def ensure_kubeconfig(cluster_name_or_id: str, kube_cli: str = "kubectl", cluster_region: Optional[str] = None) -> None:
    """Ensure kubeconfig is downloaded and points to the target cluster."""
    if cluster_region:
        run_cmd(["ibmcloud", "target", "-r", cluster_region], check=False)

    # Test if current context can reach the cluster and if it matches
    rc, out, _ = run_cmd([kube_cli, "config", "current-context"], check=False)
    current_context = out.strip() if rc == 0 else ""

    needs_fetch = False
    if cluster_name_or_id not in current_context:
        # Check if context already exists in local kubeconfig
        rc_ctx, out_ctx, _ = run_cmd([kube_cli, "config", "get-contexts", "-o", "name"], check=False)
        matched_ctx = None
        if rc_ctx == 0:
            for ctx_line in out_ctx.splitlines():
                if cluster_name_or_id in ctx_line:
                    matched_ctx = ctx_line.strip()
                    break
        if matched_ctx:
            run_cmd([kube_cli, "config", "use-context", matched_ctx], check=False)
            rc2, _, _ = run_cmd([kube_cli, "cluster-info"], check=False, timeout=10)
            if rc2 == 0:
                return
        needs_fetch = True
    else:
        # Check cluster connectivity
        rc2, _, _ = run_cmd([kube_cli, "cluster-info"], check=False, timeout=15)
        if rc2 != 0:
            needs_fetch = True

    if needs_fetch:
        print(f"Fetching cluster kubeconfig context for '{cluster_name_or_id}' via 'ibmcloud ks cluster config'...")
        cmd = ["ibmcloud", "ks", "cluster", "config", "-q", "--cluster", cluster_name_or_id, "--admin"]
        rc, _, err = run_cmd(cmd, check=False)
        if rc != 0:
            # Fall back to user credentials if admin download fails
            cmd_fallback = ["ibmcloud", "ks", "cluster", "config", "-q", "--cluster", cluster_name_or_id]
            run_cmd(cmd_fallback)


def check_bnk_cr(use_color: bool = True, kube_cli: str = "kubectl") -> Dict[str, Any]:
    """Inspect BNK Infra Custom Resources and extract VXLAN configurations."""
    results: Dict[str, Any] = {
        "crd_installed": False,
        "infra_found": False,
        "infras": [],
        "infra_name": None,
        "infra_namespace": None,
        "conditions": {},
        "vxlan_ports": [],
        "port_sources": [],
        "egress_defaults_port": None,
        "network_vxlan_ports": [],
        "details": [],
    }

    # 1. Check if Infra CRD exists
    rc, out, _ = run_cmd([kube_cli, "get", "crd", "infras.gateway.k8s.f5.com"], check=False)
    if rc != 0:
        results["crd_installed"] = False
        return results
    results["crd_installed"] = True

    # 2. Get Infra CR instances
    rc, out, _ = run_cmd([kube_cli, "get", "infras.gateway.k8s.f5.com", "-A", "-o", "json"], check=False)
    if rc != 0 or not out.strip():
        return results

    try:
        data = json.loads(out)
        items = data.get("items", [])
    except json.JSONDecodeError:
        items = []

    if not items:
        return results

    results["infra_found"] = True

    # Inspect all Infra CR instances (typically singleton per BNK deployment)
    for infra in items:
        meta = infra.get("metadata", {})
        spec = infra.get("spec", {})
        status = infra.get("status", {})
        infra_name = meta.get("name")
        infra_namespace = meta.get("namespace")

        infra_entry: Dict[str, Any] = {
            "name": infra_name,
            "namespace": infra_namespace,
            "conditions": {},
            "vxlan_ports": [],
            "port_sources": [],
        }

        # Status conditions
        for cond in status.get("conditions", []):
            ctype = cond.get("type")
            cstatus = cond.get("status")
            cmsg = cond.get("message", "")
            infra_entry["conditions"][ctype] = {"status": cstatus, "message": cmsg}

        # 1. Retrieve VXLAN port from spec.networks[] where type == "vxlan"
        networks = spec.get("networks", [])
        for net in networks:
            ntype = net.get("type", "").lower()
            if ntype == "vxlan":
                vxlan_cfg = net.get("vxlan", {})
                port = vxlan_cfg.get("port")
                vni = vxlan_cfg.get("vni")
                net_name = net.get("name", "unnamed")
                if port is None:
                    # In BNK Infra CR specification, default port for vxlan networks is 4789
                    port = 4789
                    src_desc = f"Infra '{infra_name}' -> spec.networks['{net_name}'].vxlan.port (default: 4789, vni: {vni})"
                else:
                    src_desc = f"Infra '{infra_name}' -> spec.networks['{net_name}'].vxlan.port ({port}, vni: {vni})"

                results["network_vxlan_ports"].append({
                    "infra": infra_name,
                    "namespace": infra_namespace,
                    "name": net_name,
                    "port": port,
                    "vni": vni,
                    "source": src_desc,
                })
                infra_entry["vxlan_ports"].append(port)
                infra_entry["port_sources"].append(src_desc)
                results["vxlan_ports"].append(port)
                results["port_sources"].append({"port": port, "source": src_desc})

        # 2. Retrieve VXLAN port from spec.egressDefaults.port
        egress_defaults = spec.get("egressDefaults", {})
        egress_port = egress_defaults.get("port")
        if egress_port:
            src_desc = f"Infra '{infra_name}' -> spec.egressDefaults.port ({egress_port})"
            infra_entry["vxlan_ports"].append(egress_port)
            infra_entry["port_sources"].append(src_desc)
            results["vxlan_ports"].append(egress_port)
            results["port_sources"].append({"port": egress_port, "source": src_desc})
            results["egress_defaults_port"] = egress_port

        results["infras"].append(infra_entry)

    # Use first/primary Infra CR for top-level summary
    if results["infras"]:
        results["infra_name"] = results["infras"][0]["name"]
        results["infra_namespace"] = results["infras"][0]["namespace"]
        results["conditions"] = results["infras"][0]["conditions"]

    # Deduplicate ports while preserving order
    results["vxlan_ports"] = sorted(list(set(results["vxlan_ports"])))
    return results


def check_security_group_for_port(
    cluster_name_or_id: str,
    target_ports: List[int],
    cluster_region: str,
) -> Dict[str, Any]:
    """Inspect IBM Cloud VPC Security Group rules for inbound UDP access on the target port(s)."""
    results: Dict[str, Any] = {
        "cluster_sg_id": None,
        "cluster_sg_name": None,
        "security_groups": [],
        "rules_checked": 0,
        "port_verdicts": {},
    }

    # Switch IBM Cloud target to cluster's region so VPC commands query the right region
    run_cmd(["ibmcloud", "target", "-r", cluster_region], check=False)

    # List cluster security groups
    sg_cmd = ["ibmcloud", "ks", "security-group", "ls", "--cluster", cluster_name_or_id, "--output", "json"]
    rc, out, err = run_cmd(sg_cmd, check=False)
    if rc != 0:
        raise RuntimeError(f"Failed to list security groups for cluster '{cluster_name_or_id}': {err or out}")

    sgs = json.loads(out)
    results["security_groups"] = sgs

    # Find the primary cluster SG (type == "cluster", name usually kube-<cluster_id>)
    cluster_sg = next((s for s in sgs if s.get("type") == "cluster"), None)
    if not cluster_sg and sgs:
        cluster_sg = sgs[0]

    if not cluster_sg:
        raise RuntimeError("No security groups found associated with this cluster.")

    sg_id = cluster_sg.get("id")
    sg_name = cluster_sg.get("name")
    results["cluster_sg_id"] = sg_id
    results["cluster_sg_name"] = sg_name

    # Fetch security group rules
    rules_cmd = ["ibmcloud", "is", "security-group-rules", sg_id, "--output", "json"]
    rc, out, err = run_cmd(rules_cmd, check=False)
    if rc != 0:
        raise RuntimeError(f"Failed to fetch rules for security group '{sg_name}' ({sg_id}): {err or out}")

    rules = json.loads(out)
    results["rules_checked"] = len(rules)

    for port in target_ports:
        port_info: Dict[str, Any] = {
            "port": port,
            "open": False,
            "matching_rules": [],
            "remediation_cmd": f"ibmcloud is security-group-rule-add {sg_id} inbound udp --port-min {port} --port-max {port} --remote 0.0.0.0/0",
        }

        for r in rules:
            if r.get("direction") != "inbound":
                continue

            proto = (r.get("protocol") or "").lower()
            if proto not in ("udp", "all", "icmp_tcp_udp"):
                continue

            # Check port matching:
            # If port_min / port_max not specified, it covers all ports (1-65535)
            p_min = r.get("port_min", 1)
            p_max = r.get("port_max", 65535)

            if p_min <= port <= p_max:
                remote = r.get("remote", {})
                remote_str = remote.get("cidr_block") or remote.get("name") or remote.get("id") or str(remote)
                rule_summary = {
                    "rule_id": r.get("id"),
                    "rule_name": r.get("name"),
                    "protocol": proto,
                    "port_range": f"{p_min}-{p_max}" if p_min != p_max else str(p_min),
                    "remote": remote_str,
                }
                port_info["matching_rules"].append(rule_summary)
                port_info["open"] = True

        results["port_verdicts"][port] = port_info

    return results


def check_security_group_for_icmp(
    cluster_name_or_id: str,
    cluster_region: str,
) -> Dict[str, Any]:
    """Inspect IBM Cloud VPC Security Group rules to check if inbound ICMP ping is permitted."""
    results: Dict[str, Any] = {
        "cluster_sg_id": None,
        "cluster_sg_name": None,
        "icmp_allowed": False,
        "matching_rules": [],
    }
    if not cluster_name_or_id:
        return results

    try:
        if cluster_region:
            run_cmd(["ibmcloud", "target", "-r", cluster_region], check=False)

        sg_cmd = ["ibmcloud", "ks", "security-group", "ls", "--cluster", cluster_name_or_id, "--output", "json"]
        rc, out, _ = run_cmd(sg_cmd, check=False)
        if rc != 0 or not out.strip():
            return results

        sgs = json.loads(out)
        cluster_sg = next((s for s in sgs if s.get("type") == "cluster"), None)
        if not cluster_sg and sgs:
            cluster_sg = sgs[0]

        if not cluster_sg:
            return results

        sg_id = cluster_sg.get("id")
        sg_name = cluster_sg.get("name")
        results["cluster_sg_id"] = sg_id
        results["cluster_sg_name"] = sg_name

        rules_cmd = ["ibmcloud", "is", "security-group-rules", sg_id, "--output", "json"]
        rc, out, _ = run_cmd(rules_cmd, check=False)
        if rc != 0 or not out.strip():
            return results

        rules = json.loads(out)
        for r in rules:
            if r.get("direction") != "inbound":
                continue
            proto = (r.get("protocol") or "").lower()
            if proto in ("icmp", "all", "icmp_tcp_udp"):
                results["icmp_allowed"] = True
                results["matching_rules"].append(r)
    except Exception:
        pass

    return results


def fetch_vpc_routing_tables(vpc_id: str, cluster_region: str) -> Dict[str, Any]:
    """
    Fetch all routing tables for the given VPC and their respective routes.
    Returns a dictionary with routing_tables, routes_by_rt, and table_count.
    """
    if cluster_region:
        run_cmd(["ibmcloud", "target", "-r", cluster_region], check=False)

    rc, out, err = run_cmd(["ibmcloud", "is", "vpc-routing-tables", vpc_id, "--output", "json"], check=False)
    if rc != 0:
        raise RuntimeError(f"Failed to fetch routing tables for VPC '{vpc_id}': {err or out}")

    try:
        tables = json.loads(out)
    except json.JSONDecodeError:
        tables = []

    res: Dict[str, Any] = {
        "vpc_id": vpc_id,
        "region": cluster_region,
        "table_count": len(tables),
        "tables": tables,
        "routes_by_table": {},
    }

    for rt in tables:
        rt_id = rt.get("id")
        if not rt_id:
            continue
        rc_r, out_r, _ = run_cmd(["ibmcloud", "is", "vpc-routing-table-routes", vpc_id, rt_id, "--output", "json"], check=False)
        if rc_r == 0:
            try:
                res["routes_by_table"][rt_id] = json.loads(out_r)
            except json.JSONDecodeError:
                res["routes_by_table"][rt_id] = []
        else:
            res["routes_by_table"][rt_id] = []

    return res


def is_ip_covered_in_routes(target: str, routes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Check if an IP address or CIDR is covered by any route in the given route list.
    Returns the matching route dictionary or None.
    """
    try:
        if "/" in target:
            target_net = ipaddress.ip_network(target, strict=False)
            for r in routes:
                dest = r.get("destination")
                if dest:
                    try:
                        route_net = ipaddress.ip_network(dest, strict=False)
                        # Match if target_net is within route_net, or route_net is within target_net
                        if target_net.subnet_of(route_net) or route_net.subnet_of(target_net):
                            return r
                    except ValueError:
                        continue
        else:
            ip_obj = ipaddress.ip_address(target)
            for r in routes:
                dest = r.get("destination")
                if dest:
                    try:
                        route_net = ipaddress.ip_network(dest, strict=False)
                        if ip_obj in route_net:
                            return r
                    except ValueError:
                        continue
    except ValueError:
        pass
    return None


def get_tmm_node_ip_mapping(kube_cli: str, infra_ns: Optional[str] = "f5-bnk") -> Dict[str, Any]:
    """
    Build a comprehensive mapping of worker nodes, node Internal IPs, TMM pods,
    and assigned dataplane/VLAN/VIP IPs on each TMM pod.
    """
    # 1. Map worker nodeName -> internal IP
    rc_n, out_n, _ = run_cmd([kube_cli, "get", "nodes", "-o", "json"], check=False)
    node_ip_map: Dict[str, str] = {}
    if rc_n == 0 and out_n.strip():
        try:
            for n in json.loads(out_n).get("items", []):
                n_name = n.get("metadata", {}).get("name")
                for a in n.get("status", {}).get("addresses", []):
                    if a.get("type") == "InternalIP":
                        node_ip_map[n_name] = a.get("address")
        except json.JSONDecodeError:
            pass

    # 2. Get running TMM pods
    ns_args = ["-n", infra_ns] if infra_ns else ["-A"]
    rc_t, out_t, _ = run_cmd([kube_cli, "get", "pods"] + ns_args + ["-l", "app=f5-tmm", "-o", "json"], check=False)
    tmm_pods = []
    if rc_t == 0 and out_t.strip():
        try:
            for p in json.loads(out_t).get("items", []):
                if p.get("status", {}).get("phase") == "Running":
                    tmm_pods.append(p)
        except json.JSONDecodeError:
            pass

    # 3. Query assigned IPs and configview objects on each TMM pod concurrently
    def inspect_tmm(p: Dict[str, Any]) -> Dict[str, Any]:
        p_name = p.get("metadata", {}).get("name")
        p_ns = p.get("metadata", {}).get("namespace") or infra_ns or "f5-bnk"
        n_name = p.get("spec", {}).get("nodeName")
        n_ip = node_ip_map.get(n_name)
        pod_ip = p.get("status", {}).get("podIP")

        net1_ips: List[str] = []
        self_ips: List[str] = []
        vxlan_self_ips: List[str] = []
        vs_vips: List[str] = []
        trans_addrs: List[str] = []
        assigned_ips: List[str] = []
        cv_static_routes: List[Dict[str, Any]] = []
        bdt_routes: List[Dict[str, Any]] = []
        bdt_arp: Dict[str, Dict[str, str]] = {}

        # Run multi-command check in debug container:
        # 1. net1 interface IP assignments
        # 2. configview show self_ip
        # 3. configview show virtual_server
        # 4. configview show translation_address
        # 5. configview static_route
        # 6. bdt_cli route
        # 7. bdt_cli arp
        inspect_script = (
            'echo "===NET1==="; '
            'ip -o -4 addr show net1 2>/dev/null; '
            'echo "===SELF_IP==="; '
            'configview show self_ip 2>/dev/null; '
            'echo "===VIRTUAL_SERVER==="; '
            'configview show virtual_server 2>/dev/null; '
            'echo "===TRANSLATION_ADDRESS==="; '
            'configview show translation_address 2>/dev/null; '
            'echo "===STATIC_ROUTE==="; '
            'configview static_route 2>/dev/null || '
            'configview show static_route 2>/dev/null; '
            'echo "===BDT_ROUTE==="; '
            'bdt_cli route 2>/dev/null; '
            'echo "===BDT_ARP==="; '
            'bdt_cli arp 2>/dev/null'
        )
        rc_insp, out_insp, _ = run_cmd(
            [kube_cli, "exec", "-n", p_ns, p_name, "-c", "debug", "--", "sh", "-c", inspect_script],
            check=False,
            timeout=15,
        )

        if rc_insp == 0 and out_insp.strip():
            cur_sec = None
            for raw_line in out_insp.splitlines():
                line = raw_line.strip()
                if line.startswith("===") and line.endswith("==="):
                    cur_sec = line[3:-3]
                    continue
                if cur_sec == "NET1":
                    m = re.search(r'inet\s+([0-9.]+)/\d+', line)
                    if m:
                        ip_val = m.group(1)
                        if not ip_val.startswith("127.") and not ip_val.startswith("169.254.") and ip_val != pod_ip:
                            if ip_val not in net1_ips:
                                net1_ips.append(ip_val)
                            if ip_val not in assigned_ips:
                                assigned_ips.append(ip_val)
                elif cur_sec == "SELF_IP":
                    is_vxlan = ("bnkvxlan" in line or "vxlan" in line)
                    for ip_val in re.findall(r'addr:\s*([0-9.]+)', line):
                        if not ip_val.startswith("127.") and not ip_val.startswith("169.254.") and ip_val != pod_ip:
                            if is_vxlan:
                                if ip_val not in vxlan_self_ips:
                                    vxlan_self_ips.append(ip_val)
                            else:
                                if ip_val not in self_ips:
                                    self_ips.append(ip_val)
                            if ip_val not in assigned_ips:
                                assigned_ips.append(ip_val)
                elif cur_sec == "VIRTUAL_SERVER":
                    for ip_val in re.findall(r'([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)', line):
                        if not ip_val.startswith("127.") and not ip_val.startswith("169.254.") and ip_val != pod_ip:
                            if ip_val not in vs_vips:
                                vs_vips.append(ip_val)
                            if ip_val not in assigned_ips:
                                assigned_ips.append(ip_val)
                elif cur_sec == "TRANSLATION_ADDRESS":
                    for ip_val in re.findall(r'([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)', line):
                        if not ip_val.startswith("127.") and not ip_val.startswith("169.254.") and ip_val != pod_ip:
                            if ip_val not in trans_addrs:
                                trans_addrs.append(ip_val)
                            if ip_val not in assigned_ips:
                                assigned_ips.append(ip_val)
                elif cur_sec == "STATIC_ROUTE":
                    m_id = re.search(r'id:"([^"]+)"', line)
                    m_pfx = re.search(
                        r'ip_prefix:\{ip:\{addr:([0-9.]+)[^}]*\}\s+pl:(\d+)\}',
                        line,
                    )
                    m_gw = re.search(
                        r'ip_address:\{ip:\{addr:([0-9.]+)[^}]*\}\}',
                        line,
                    )
                    m_st = re.search(r'state:(\d+)', line)
                    m_typ = re.search(r'type:"([^"]+)"', line)
                    if m_id and m_pfx and m_gw:
                        cv_static_routes.append({
                            "id": m_id.group(1),
                            "dest": f"{m_pfx.group(1)}/{m_pfx.group(2)}",
                            "gw": m_gw.group(1),
                            "type": m_typ.group(1) if m_typ else "ROUTE_TYPE_GATEWAY",
                            "state": m_st.group(1) if m_st else "unknown",
                        })
                elif cur_sec == "BDT_ROUTE":
                    m_dst = re.search(
                        r'destNet:\{ip:\{addr:([0-9.]+)[^}]*\}\s+pl:(\d+)\}',
                        line,
                    )
                    m_gw = re.search(
                        r'gw:\{ip:\{addr:([0-9.]+)[^}]*\}\}',
                        line,
                    )
                    m_iface = re.search(r'interface:([^\s]+)', line)
                    m_unres = re.search(r'unresolved:(true|false)', line)
                    if m_dst and m_gw:
                        bdt_routes.append({
                            "dest": f"{m_dst.group(1)}/{m_dst.group(2)}",
                            "gw": m_gw.group(1),
                            "interface": m_iface.group(1) if m_iface else "n/a",
                            "resolved": (m_unres.group(1) == "false") if m_unres else False,
                        })
                elif cur_sec == "BDT_ARP":
                    m_ip = re.search(r'ipAddr:([0-9.]+)', line)
                    m_mac = re.search(r'macAddr:([0-9a-fA-F:]+)', line)
                    m_vlan = re.search(r'vlan:([^\s]+)', line)
                    m_stat = re.search(r'status:([^\s]+)', line)
                    if m_ip and m_mac:
                        bdt_arp[m_ip.group(1)] = {
                            "mac": m_mac.group(1),
                            "vlan": m_vlan.group(1) if m_vlan else "n/a",
                            "status": m_stat.group(1) if m_stat else "unknown",
                        }

        if not assigned_ips:
            # Fallback to general ip addr show if combined check had no results
            rc_fb, out_fb, _ = run_cmd(
                [kube_cli, "exec", "-n", p_ns, p_name, "-c", "debug", "--", "ip", "-o", "-4", "addr", "show"],
                check=False,
                timeout=10,
            )
            if rc_fb == 0:
                for line in out_fb.splitlines():
                    parts = line.split()
                    if len(parts) >= 4 and "/" in parts[3]:
                        ip_val = parts[3].split("/")[0]
                        if not ip_val.startswith("127.") and not ip_val.startswith("169.254.") and ip_val != pod_ip:
                            if ip_val not in assigned_ips:
                                assigned_ips.append(ip_val)

        return {
            "pod_name": p_name,
            "namespace": p_ns,
            "node_name": n_name,
            "node_ip": n_ip,
            "pod_ip": pod_ip,
            "assigned_ips": assigned_ips,
            "net1_ips": net1_ips,
            "self_ips": self_ips,
            "vxlan_self_ips": vxlan_self_ips,
            "vs_vips": vs_vips,
            "trans_addrs": trans_addrs,
            "static_routes": cv_static_routes,
            "bdt_routes": bdt_routes,
            "bdt_arp": bdt_arp,
        }

    tmm_details = []
    ip_to_tmm: Dict[str, Dict[str, Any]] = {}
    if tmm_pods:
        with ThreadPoolExecutor(max_workers=min(len(tmm_pods), 8)) as ex:
            tmm_details = list(ex.map(inspect_tmm, tmm_pods))
        for td in tmm_details:
            for a_ip in td["assigned_ips"]:
                ip_to_tmm[a_ip] = td
            for s_ip in td["self_ips"]:
                ip_to_tmm[s_ip] = td
            for vx_ip in td.get("vxlan_self_ips", []):
                ip_to_tmm[vx_ip] = td
            for v_ip in td["vs_vips"]:
                ip_to_tmm[v_ip] = td
            for t_ip in td["trans_addrs"]:
                ip_to_tmm[t_ip] = td
            for n1_ip in td["net1_ips"]:
                ip_to_tmm[n1_ip] = td

    all_tmm_node_ips = {td["node_ip"] for td in tmm_details if td.get("node_ip")}

    return {
        "node_ip_map": node_ip_map,
        "tmm_details": tmm_details,
        "ip_to_tmm": ip_to_tmm,
        "all_tmm_node_ips": all_tmm_node_ips,
    }


def check_infra_routes_and_ipam(
    kube_cli: str,
    rts_info: Dict[str, Any],
    tmm_mapping: Dict[str, Any],
    use_color: bool = True,
) -> Dict[str, Any]:
    """
    Read Infra CRs, figure address used as VLAN by TMM, verify that assigned addresses
    match what is reported by f5-ipam CR (ipams.fic.f5.com), and check if routes covering
    these addresses are added to BOTH VPC routing tables with next-hop pointing to the
    hosting worker node IP.
    """
    results: Dict[str, Any] = {
        "infra_found": False,
        "vlan_networks": [],
        "ipam_matched": True,
        "all_routes_covered": True,
        "all_next_hops_matched": True,
    }

    rc, out, _ = run_cmd([kube_cli, "get", "infras.gateway.k8s.f5.com", "-A", "-o", "json"], check=False)
    if rc != 0 or not out.strip():
        return results

    try:
        infra_list = json.loads(out).get("items", [])
    except json.JSONDecodeError:
        infra_list = []

    if not infra_list:
        return results

    results["infra_found"] = True
    tables = rts_info.get("tables", [])
    routes_by_table = rts_info.get("routes_by_table", {})
    all_tmm_node_ips = tmm_mapping.get("all_tmm_node_ips", set())

    for infra in infra_list:
        infra_name = infra.get("metadata", {}).get("name")
        infra_ns = infra.get("metadata", {}).get("namespace", "f5-bnk")
        spec = infra.get("spec", {})

        networks = spec.get("networks", [])
        vlan_nets = [n for n in networks if n.get("type", "").lower() == "vlan" or "vlan" in n.get("name", "").lower()]

        for vnet in vlan_nets:
            vlan_name = vnet.get("name", "external-vlan")
            vlan_entry: Dict[str, Any] = {
                "infra_name": infra_name,
                "namespace": infra_ns,
                "vlan_name": vlan_name,
                "ipam_cr_name": None,
                "ipam_ips": [],
                "tmm_pod_ips": {},
                "ipam_match": True,
                "route_verdicts": {},
            }

            # 1. Look for matching IPAM CR in namespace
            rc_ipam, out_ipam, _ = run_cmd([kube_cli, "get", "ipams.fic.f5.com", "-n", infra_ns, "-o", "json"], check=False)
            if rc_ipam == 0 and out_ipam.strip():
                try:
                    ipam_items = json.loads(out_ipam).get("items", [])
                    target_ipam = None
                    for cr in ipam_items:
                        m_name = cr.get("metadata", {}).get("name", "")
                        ann = cr.get("metadata", {}).get("annotations", {})
                        if ann.get("infra-name") == infra_name and ann.get("vlan-name") == vlan_name:
                            target_ipam = cr
                            break
                        if m_name == f"vlan-{infra_ns}-{vlan_name}.{infra_name}" or (vlan_name in m_name and infra_name in m_name):
                            target_ipam = cr
                            break

                    if target_ipam:
                        vlan_entry["ipam_cr_name"] = target_ipam.get("metadata", {}).get("name")
                        ip_statuses = target_ipam.get("status", {}).get("IPStatus", [])
                        for ips in ip_statuses:
                            if ips.get("status") == "Ok" and ips.get("ip"):
                                ip_val = ips.get("ip")
                                vlan_entry["ipam_ips"].append(ip_val)
                                vlan_entry.setdefault("ipam_status_entries", {})[ip_val] = ips
                                if ips.get("cidrBlock"):
                                    vlan_entry.setdefault("cidr_blocks", []).append(ips["cidrBlock"])
                                if "reserved" in ips.get("key", "") or not ips.get("deviceID"):
                                    vlan_entry.setdefault("reserved_gw_ips", set()).add(ip_val)
                except json.JSONDecodeError:
                    pass

            # 2. Retrieve actual IPs assigned to TMM pods on this VLAN interface
            for td in tmm_mapping.get("tmm_details", []):
                p_n = td["pod_name"]
                cand_ips = td.get("self_ips") or td.get("net1_ips") or td.get("assigned_ips", [])
                matched_ip = None
                if vlan_entry["ipam_ips"]:
                    for c_ip in cand_ips:
                        if c_ip in vlan_entry["ipam_ips"]:
                            matched_ip = c_ip
                            break
                if not matched_ip and cand_ips:
                    matched_ip = cand_ips[0]
                if matched_ip:
                    vlan_entry["tmm_pod_ips"][p_n] = matched_ip

            # Fallback if tmm_details was empty
            if not vlan_entry["tmm_pod_ips"]:
                rc_pods, out_pods, _ = run_cmd([kube_cli, "get", "pods", "-n", infra_ns, "-l", "app=f5-tmm", "-o", "json"], check=False)
                tmm_pod_names = []
                if rc_pods == 0 and out_pods.strip():
                    try:
                        p_items = json.loads(out_pods).get("items", [])
                        tmm_pod_names = [p.get("metadata", {}).get("name") for p in p_items if p.get("status", {}).get("phase") == "Running"]
                    except json.JSONDecodeError:
                        pass

                def query_tmm_vlan_ip(pod_name: str) -> Tuple[str, Optional[str]]:
                    cmd_ip = [kube_cli, "exec", "-n", infra_ns, pod_name, "-c", "debug", "--", "ip", "-o", "-4", "addr", "show", "net1"]
                    r_code, out_str, _ = run_cmd(cmd_ip, check=False, timeout=10)
                    if r_code == 0:
                        for l in out_str.splitlines():
                            parts = l.split()
                            if len(parts) >= 4 and "/" in parts[3]:
                                return pod_name, parts[3].split("/")[0]
                    return pod_name, None

                if tmm_pod_names:
                    with ThreadPoolExecutor(max_workers=min(len(tmm_pod_names), 8)) as ex:
                        pod_results = list(ex.map(query_tmm_vlan_ip, tmm_pod_names))
                    for pod_n, pod_ip in pod_results:
                        if pod_ip:
                            vlan_entry["tmm_pod_ips"][pod_n] = pod_ip

            # 3. Confirm addresses assigned to TMM match what is reported by f5-ipam CR
            ipam_ip_set = set(vlan_entry["ipam_ips"])
            tmm_ip_set = set(vlan_entry["tmm_pod_ips"].values())
            if tmm_ip_set and not tmm_ip_set.issubset(ipam_ip_set):
                vlan_entry["ipam_match"] = False
                results["ipam_matched"] = False

            # Identify special reserved default gateway addresses (e.g. ending in .1 or first usable host in subnet)
            # These are reserved by the network as default gateways, not owned by TMM pods, so route checking is skipped on them.
            reserved_gws: Set[str] = set(vlan_entry.get("reserved_gw_ips", set()))
            cidr_blocks = vlan_entry.get("cidr_blocks", [])
            for ip_val in ipam_ip_set:
                if ip_val.endswith(".1"):
                    reserved_gws.add(ip_val)
                elif cidr_blocks:
                    try:
                        ip_obj = ipaddress.ip_address(ip_val)
                        for cb in cidr_blocks:
                            try:
                                net = ipaddress.ip_network(cb, strict=False)
                                if ip_obj in net and ip_obj == next(net.hosts(), None):
                                    reserved_gws.add(ip_val)
                                    break
                            except ValueError:
                                pass
                    except ValueError:
                        pass

            # Exclude any address that is actually assigned to a TMM pod
            skipped_gw_addrs = sorted([ip for ip in reserved_gws if ip not in tmm_ip_set])
            vlan_entry["skipped_gw_addrs"] = skipped_gw_addrs

            # 4. Check if route to this address is added to BOTH route tables & verify next-hop
            all_addrs = ipam_ip_set.union(tmm_ip_set)
            addresses_to_verify = sorted([ip for ip in all_addrs if ip not in skipped_gw_addrs])
            for ip_addr in addresses_to_verify:
                tmm_info = tmm_mapping.get("ip_to_tmm", {}).get(ip_addr)
                if not tmm_info:
                    for p_n, p_ip in vlan_entry["tmm_pod_ips"].items():
                        if p_ip == ip_addr:
                            tmm_info = next((td for td in tmm_mapping.get("tmm_details", []) if td["pod_name"] == p_n), None)
                            break
                expected_nh = tmm_info.get("node_ip") if tmm_info else None
                tmm_pod = tmm_info.get("pod_name") if tmm_info else None
                node_name = tmm_info.get("node_name") if tmm_info else None
                tmm_owned_self = (
                    (ip_addr in tmm_info.get("self_ips", []))
                    if tmm_info else False
                )
                tmm_owned_net1 = (
                    (ip_addr in tmm_info.get("net1_ips", []))
                    if tmm_info else False
                )

                # Determine associated parent /24 subnet & default gateway (.1)
                ipam_entries = vlan_entry.get("ipam_status_entries", {})
                curr_ips = ipam_entries.get(ip_addr, {})
                subnet_cidr = curr_ips.get("cidrBlock")
                # Subnet default gateway is always .1 of the parent /24 subnet
                gw_ip = (
                    (ip_addr.rsplit(".", 1)[0] + ".1")
                    if "." in ip_addr else None
                )
                if gw_ip == ip_addr:
                    gw_ip = None

                gw_reserved = False
                if gw_ip and gw_ip in ipam_entries:
                    gw_entry = ipam_entries[gw_ip]
                    if gw_entry.get("status") == "Ok":
                        gw_reserved = True
                elif (
                    gw_ip
                    and gw_ip in vlan_entry.get("reserved_gw_ips", set())
                ):
                    gw_reserved = True

                addr_verdict = {
                    "covered_in_all": True,
                    "all_next_hops_match": True,
                    "expected_next_hop": expected_nh,
                    "tmm_pod": tmm_pod,
                    "node_name": node_name,
                    "owned_by_self_ip": tmm_owned_self,
                    "assigned_to_net1": tmm_owned_net1,
                    "subnet_cidr": subnet_cidr,
                    "subnet_gw_ip": gw_ip,
                    "subnet_gw_reserved": gw_reserved,
                    "table_coverage": {},
                }

                for rt in tables:
                    rt_id = rt.get("id")
                    rt_name = rt.get("name", rt_id)
                    routes = routes_by_table.get(rt_id, [])
                    matching_route = is_ip_covered_in_routes(ip_addr, routes)
                    if matching_route:
                        nh_addr = matching_route.get("next_hop", {}).get("address", "n/a")
                        nh_match = False
                        if expected_nh:
                            nh_match = (nh_addr == expected_nh)
                        elif nh_addr in all_tmm_node_ips:
                            nh_match = True

                        if not nh_match:
                            addr_verdict["all_next_hops_match"] = False
                            results["all_next_hops_matched"] = False

                        addr_verdict["table_coverage"][rt_name] = {
                            "covered": True,
                            "route_destination": matching_route.get("destination"),
                            "next_hop": nh_addr,
                            "route_name": matching_route.get("name"),
                            "route_id": matching_route.get("id"),
                            "zone": matching_route.get("zone", {}).get("name", "n/a"),
                            "next_hop_match": nh_match,
                            "expected_next_hop": expected_nh,
                        }
                    else:
                        addr_verdict["table_coverage"][rt_name] = {"covered": False}
                        addr_verdict["covered_in_all"] = False
                        results["all_routes_covered"] = False

                vlan_entry["route_verdicts"][ip_addr] = addr_verdict

            results["vlan_networks"].append(vlan_entry)

    return results


def check_infra_static_routes(
    kube_cli: str,
    tmm_mapping: Dict[str, Any],
    use_color: bool = True,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Verify staticRoutes configured in the Infra CR (infras.gateway.k8s.f5.com)
    against TMM debug container outputs:
    - configview static_route: configured in TMM declTmm
    - bdt_cli route: associated dataplane route active and resolved
    - bdt_cli arp: associated ARP entries based on gateway used by route
    """
    results: Dict[str, Any] = {
        "infra_found": False,
        "has_static_routes": False,
        "all_passed": True,
        "routes": [],
    }

    rc, out, _ = run_cmd(
        [kube_cli, "get", "infras.gateway.k8s.f5.com", "-A", "-o", "json"],
        check=False,
    )
    if rc != 0 or not out.strip():
        return results

    try:
        infra_list = json.loads(out).get("items", [])
    except json.JSONDecodeError:
        infra_list = []

    if not infra_list:
        return results

    results["infra_found"] = True

    # Check if any infra CR has staticRoutes defined
    has_any = any(
        bool(infra.get("spec", {}).get("staticRoutes"))
        for infra in infra_list
    )
    if not has_any:
        return results

    results["has_static_routes"] = True
    tmm_details = tmm_mapping.get("tmm_details", [])

    def find_hosting_pod(gw: Optional[str]) -> Optional[Dict[str, Any]]:
        if not gw or "." not in gw or not tmm_details:
            return tmm_details[0] if tmm_details else None
        gw_prefix = gw.rsplit(".", 1)[0]
        for td in tmm_details:
            all_ips = (
                td.get("net1_ips", [])
                + td.get("self_ips", [])
                + td.get("assigned_ips", [])
            )
            for ip in all_ips:
                if "." in ip and ip.rsplit(".", 1)[0] == gw_prefix:
                    return td
        return tmm_details[0] if tmm_details else None

    for infra in infra_list:
        infra_name = infra.get("metadata", {}).get("name", "unknown")
        infra_ns = infra.get("metadata", {}).get("namespace", "f5-bnk")
        spec = infra.get("spec", {})
        static_routes = spec.get("staticRoutes", [])
        if not static_routes:
            continue

        print(
            f"    • Infra '{infra_name}' (namespace: {infra_ns}): "
            f"{len(static_routes)} static route(s) configured"
        )

        for sr in static_routes:
            sr_name = sr.get("name", "unnamed-route")
            destinations = sr.get("destinations", [])
            if not destinations and sr.get("destination"):
                destinations = [sr.get("destination")]
            next_hop = sr.get("nextHop")

            hosting_pod = find_hosting_pod(next_hop)
            hosting_pod_name = (
                hosting_pod.get("pod_name", "unknown")
                if hosting_pod
                else "unknown"
            )

            print(f"      - Static Route: '{sr_name}'")

            for dest in destinations:
                dest_str = f"Destination: {dest}, Next-Hop: {next_hop}"
                if hosting_pod_name != "unknown":
                    print(f"        {dest_str} (Hosting Pod: {hosting_pod_name})")
                else:
                    print(f"        {dest_str}")

                # 1. Check configview static_route across TMM pods
                cv_entry = None
                check_pods = [hosting_pod] if hosting_pod else []
                check_pods += [td for td in tmm_details if td != hosting_pod]
                for td in check_pods:
                    if not td:
                        continue
                    for r in td.get("static_routes", []):
                        if r.get("dest") == dest and (
                            r.get("gw") == next_hop or sr_name in r.get("id", "")
                        ):
                            cv_entry = r
                            break
                    if cv_entry:
                        break

                cv_ok = cv_entry is not None
                if cv_ok:
                    cv_badge = colorize("PASS", Colors.GREEN, use_color)
                    print(
                        f"        [{cv_badge}] configview static_route: "
                        f"Configured in TMM"
                    )
                    if verbose:
                        cv_id = cv_entry.get("id", "")
                        cv_st = cv_entry.get("state", "")
                        cv_typ = cv_entry.get("type", "n/a")
                        print(f"               id: {cv_id}")
                        print(f"               state: {cv_st}, type: {cv_typ}")
                else:
                    cv_badge = colorize("FAIL", Colors.RED, use_color)
                    print(
                        f"        [{cv_badge}] configview static_route: "
                        f"Not found in TMM configuration"
                    )
                    results["all_passed"] = False

                # 2. Check bdt_cli route on hosting pod
                bdt_entry = None
                for td in check_pods:
                    if not td:
                        continue
                    for r in td.get("bdt_routes", []):
                        if r.get("dest") == dest and r.get("gw") == next_hop:
                            bdt_entry = r
                            if r.get("resolved"):
                                break
                    if bdt_entry and bdt_entry.get("resolved"):
                        break

                bdt_ok = bdt_entry is not None and bdt_entry.get("resolved", False)
                if bdt_ok:
                    bdt_badge = colorize("PASS", Colors.GREEN, use_color)
                    bdt_iface = bdt_entry.get("interface", "n/a")
                    print(
                        f"        [{bdt_badge}] bdt_cli route         : "
                        f"Active via {next_hop} (vlan: {bdt_iface})"
                    )
                elif bdt_entry:
                    bdt_badge = colorize("FAIL", Colors.RED, use_color)
                    print(
                        f"        [{bdt_badge}] bdt_cli route         : "
                        f"Unresolved in dataplane (unresolved: true)"
                    )
                    results["all_passed"] = False
                else:
                    bdt_badge = colorize("FAIL", Colors.RED, use_color)
                    print(
                        f"        [{bdt_badge}] bdt_cli route         : "
                        f"Route not found in dataplane"
                    )
                    results["all_passed"] = False

                # 3. Check bdt_cli arp for gateway used by route
                arp_entry = None
                for td in check_pods:
                    if not td:
                        continue
                    arp_map = td.get("bdt_arp", {})
                    if next_hop in arp_map:
                        arp_entry = arp_map[next_hop]
                        break

                arp_ok = arp_entry is not None and bool(arp_entry.get("mac"))
                if arp_ok:
                    arp_badge = colorize("PASS", Colors.GREEN, use_color)
                    arp_mac = arp_entry.get("mac")
                    arp_vlan = arp_entry.get("vlan", "n/a")
                    arp_stat = arp_entry.get("status", "resolved")
                    print(
                        f"        [{arp_badge}] bdt_cli arp           : "
                        f"Gateway {next_hop} -> {arp_mac}"
                    )
                    print(
                        f"               (vlan: {arp_vlan}, status: {arp_stat})"
                    )
                elif arp_entry:
                    arp_badge = colorize("WARN", Colors.YELLOW, use_color)
                    arp_stat = arp_entry.get("status", "unknown")
                    print(
                        f"        [{arp_badge}] bdt_cli arp           : "
                        f"Gateway {next_hop} entry status '{arp_stat}'"
                    )
                    results["all_passed"] = False
                else:
                    arp_badge = colorize("FAIL", Colors.RED, use_color)
                    print(
                        f"        [{arp_badge}] bdt_cli arp           : "
                        f"Gateway {next_hop} not found in ARP table"
                    )
                    results["all_passed"] = False

                route_res = {
                    "infra_name": infra_name,
                    "namespace": infra_ns,
                    "route_name": sr_name,
                    "destination": dest,
                    "next_hop": next_hop,
                    "hosting_pod": hosting_pod_name,
                    "cv_ok": cv_ok,
                    "bdt_ok": bdt_ok,
                    "arp_ok": arp_ok,
                    "passed": cv_ok and bdt_ok and arp_ok,
                }
                results["routes"].append(route_res)

    return results


def check_gateway_routes_and_ipam(
    kube_cli: str,
    rts_info: Dict[str, Any],
    tmm_mapping: Dict[str, Any],
    use_color: bool = True,
) -> Dict[str, Any]:
    """
    Read Gateway CRs (gateway.networking.k8s.io), confirm VIP addresses match what
    is reported by f5-ipam CRs, and check if routes covering each VIP are added to BOTH
    VPC routing tables with next-hop pointing to the worker node IP hosting TMM.
    """
    results: Dict[str, Any] = {
        "gateway_count": 0,
        "gateways": [],
        "ipam_matched": True,
        "all_routes_covered": True,
        "all_next_hops_matched": True,
    }

    rc, out, _ = run_cmd([kube_cli, "get", "gateways.gateway.networking.k8s.io", "-A", "-o", "json"], check=False)
    if rc != 0 or not out.strip():
        return results

    try:
        gw_list = json.loads(out).get("items", [])
    except json.JSONDecodeError:
        gw_list = []

    results["gateway_count"] = len(gw_list)
    tables = rts_info.get("tables", [])
    routes_by_table = rts_info.get("routes_by_table", {})
    all_tmm_node_ips = tmm_mapping.get("all_tmm_node_ips", set())

    for gw in gw_list:
        gw_name = gw.get("metadata", {}).get("name")
        gw_ns = gw.get("metadata", {}).get("namespace", "default")
        addrs = [a.get("value") for a in gw.get("status", {}).get("addresses", []) if a.get("value")]
        if not addrs:
            addrs = [a.get("value") for a in gw.get("spec", {}).get("addresses", []) if a.get("value")]

        gw_entry: Dict[str, Any] = {
            "name": gw_name,
            "namespace": gw_ns,
            "vips": addrs,
            "ipam_cr_name": f"gw-{gw_ns}-{gw_name}",
            "ipam_ips": [],
            "ipam_match": True,
            "route_verdicts": {},
        }

        rc_i, out_i, _ = run_cmd([kube_cli, "get", "ipams.fic.f5.com", "-n", gw_ns, gw_entry["ipam_cr_name"], "-o", "json"], check=False)
        if rc_i == 0 and out_i.strip():
            try:
                ipam_obj = json.loads(out_i)
                for ips in ipam_obj.get("status", {}).get("IPStatus", []):
                    if ips.get("status") == "Ok" and ips.get("ip"):
                        gw_entry["ipam_ips"].append(ips.get("ip"))
            except json.JSONDecodeError:
                pass

        if addrs and gw_entry["ipam_ips"]:
            if not set(addrs).issubset(set(gw_entry["ipam_ips"])):
                gw_entry["ipam_match"] = False
                results["ipam_matched"] = False

        for vip in addrs:
            tmm_info = tmm_mapping.get("ip_to_tmm", {}).get(vip)
            expected_nh = tmm_info.get("node_ip") if tmm_info else None
            tmm_pod = tmm_info.get("pod_name") if tmm_info else None
            node_name = tmm_info.get("node_name") if tmm_info else None
            tmm_owned_vs = (vip in tmm_info.get("vs_vips", [])) if tmm_info else False
            tmm_owned_net1 = (vip in tmm_info.get("net1_ips", [])) if tmm_info else False

            vip_verdict = {
                "covered_in_all": True,
                "all_next_hops_match": True,
                "expected_next_hop": expected_nh,
                "tmm_pod": tmm_pod,
                "node_name": node_name,
                "owned_by_vs": tmm_owned_vs,
                "assigned_to_net1": tmm_owned_net1,
                "table_coverage": {},
            }

            for rt in tables:
                rt_id = rt.get("id")
                rt_name = rt.get("name", rt_id)
                routes = routes_by_table.get(rt_id, [])
                matching_route = is_ip_covered_in_routes(vip, routes)
                if matching_route:
                    nh_addr = matching_route.get("next_hop", {}).get("address", "n/a")
                    nh_match = False
                    if expected_nh:
                        nh_match = (nh_addr == expected_nh)
                    elif nh_addr in all_tmm_node_ips:
                        nh_match = True

                    if not nh_match:
                        vip_verdict["all_next_hops_match"] = False
                        results["all_next_hops_matched"] = False

                    vip_verdict["table_coverage"][rt_name] = {
                        "covered": True,
                        "route_destination": matching_route.get("destination"),
                        "next_hop": nh_addr,
                        "route_name": matching_route.get("name"),
                        "route_id": matching_route.get("id"),
                        "zone": matching_route.get("zone", {}).get("name", "n/a"),
                        "next_hop_match": nh_match,
                        "expected_next_hop": expected_nh,
                    }
                else:
                    vip_verdict["table_coverage"][rt_name] = {"covered": False}
                    vip_verdict["covered_in_all"] = False
                    results["all_routes_covered"] = False

            gw_entry["route_verdicts"][vip] = vip_verdict

        results["gateways"].append(gw_entry)

    return results


def check_egress_gateway_routes_and_ipam(
    kube_cli: str,
    rts_info: Dict[str, Any],
    tmm_mapping: Dict[str, Any],
    use_color: bool = True,
) -> Dict[str, Any]:
    """
    Read EgressGateway CRs (gateway.k8s.f5.com) and follow each to its associated
    GatewaySettings to determine the SNAT configuration:
    - UseIngressAddress: Egress uses the VIP from the Gateway CR in the same namespace;
      skip test (covered via Gateway CR in Step 6.3).
    - Automap: Egress uses the TMM VLAN address; skip test (covered in Step 6.1).
    - Pool / SnatPool: Follow sourceNATPoolRef to the IPAM pool and verify covering
      routes and next-hops in both VPC route tables.
    """
    results: Dict[str, Any] = {
        "egress_gateway_count": 0,
        "egress_gateways": [],
        "has_gateway_settings": False,
        "gateway_settings_snat": [],
        "all_routes_covered": True,
        "all_next_hops_matched": True,
    }

    tables = rts_info.get("tables", [])
    routes_by_table = rts_info.get("routes_by_table", {})
    all_tmm_node_ips = tmm_mapping.get("all_tmm_node_ips", set())

    # Pre-fetch all GatewaySettings across all namespaces for fast lookup
    gs_dict: Dict[Tuple[str, str], Dict[str, Any]] = {}
    rc_gs, out_gs, _ = run_cmd([kube_cli, "get", "gatewaysettings.gateway.k8s.f5.com", "-A", "-o", "json"], check=False)
    if rc_gs == 0 and out_gs.strip():
        try:
            for item in json.loads(out_gs).get("items", []):
                m = item.get("metadata", {})
                gs_dict[(m.get("namespace", ""), m.get("name", ""))] = item
            if gs_dict:
                results["has_gateway_settings"] = True
        except json.JSONDecodeError:
            pass

    # Pre-fetch all Infras across all namespaces for IPAM lookup
    all_infras: List[Dict[str, Any]] = []
    rc_inf, out_inf, _ = run_cmd([kube_cli, "get", "infras.gateway.k8s.f5.com", "-A", "-o", "json"], check=False)
    if rc_inf == 0 and out_inf.strip():
        try:
            all_infras = json.loads(out_inf).get("items", [])
        except json.JSONDecodeError:
            pass

    rc, out, _ = run_cmd([kube_cli, "get", "egressgateways.gateway.k8s.f5.com", "-A", "-o", "json"], check=False)
    if rc == 0 and out.strip():
        try:
            egw_list = json.loads(out).get("items", [])
            results["egress_gateway_count"] = len(egw_list)
            for egw in egw_list:
                egw_name = egw.get("metadata", {}).get("name", "n/a")
                egw_ns = egw.get("metadata", {}).get("namespace", "f5-bnk")
                params_ref = egw.get("spec", {}).get("infrastructure", {}).get("parametersRef", {})
                gs_name = params_ref.get("name")
                section_name = params_ref.get("sectionName")

                egw_entry: Dict[str, Any] = {
                    "name": egw_name,
                    "namespace": egw_ns,
                    "gs_name": gs_name,
                    "section_name": section_name,
                }

                if not gs_name:
                    egw_entry["status"] = "WARN"
                    egw_entry["message"] = "No infrastructure.parametersRef.name configured in EgressGateway"
                    results["egress_gateways"].append(egw_entry)
                    continue

                gs = gs_dict.get((egw_ns, gs_name))
                if not gs:
                    for (ns, n), candidate in gs_dict.items():
                        if n == gs_name:
                            gs = candidate
                            break

                if not gs:
                    egw_entry["status"] = "FAIL"
                    egw_entry["message"] = f"GatewaySettings '{gs_name}' not found in namespace '{egw_ns}'"
                    results["all_routes_covered"] = False
                    results["egress_gateways"].append(egw_entry)
                    continue

                egress_configs = gs.get("spec", {}).get("egressConfigs", [])
                matched_cfg = None
                if section_name:
                    for c in egress_configs:
                        if c.get("name") == section_name:
                            matched_cfg = c
                            break
                elif len(egress_configs) == 1:
                    matched_cfg = egress_configs[0]

                if not matched_cfg:
                    egw_entry["status"] = "FAIL"
                    sec_str = f"'{section_name}'" if section_name else "<unspecified>"
                    egw_entry["message"] = f"Egress config section {sec_str} not found in GatewaySettings '{gs_name}'"
                    results["all_routes_covered"] = False
                    results["egress_gateways"].append(egw_entry)
                    continue

                source_nat_cfg = matched_cfg.get("sourceNATConfig", {})
                snat_type = source_nat_cfg.get("type", "Automap")
                snat_type_lower = snat_type.lower()
                egw_entry["snat_type"] = snat_type

                if snat_type_lower == "useingressaddress":
                    egw_entry["status"] = "SKIP"
                    egw_entry["skip_reason"] = (
                        f"SNAT Type: UseIngressAddress (uses same VIP as Gateway CR in namespace '{egw_ns}'; "
                        f"verified in Step 6.3)"
                    )
                elif snat_type_lower == "automap":
                    egw_entry["status"] = "SKIP"
                    egw_entry["skip_reason"] = (
                        "SNAT Type: Automap (uses TMM VLAN self-IP address; verified in Step 6.1)"
                    )
                elif snat_type_lower == "none":
                    egw_entry["status"] = "SKIP"
                    egw_entry["skip_reason"] = (
                        "SNAT Type: None (SNAT is disabled for this EgressGateway)"
                    )
                elif snat_type_lower in ("pool", "snatpool"):
                    pool_ref_name = source_nat_cfg.get("sourceNATPoolRef", {}).get("name")
                    egw_entry["pool_name"] = pool_ref_name
                    matched_pools = [
                        p for p in gs.get("spec", {}).get("sourceNATPools", [])
                        if p.get("name") == pool_ref_name
                    ]
                    if not matched_pools:
                        egw_entry["status"] = "FAIL"
                        egw_entry["message"] = (
                            f"sourceNATPoolRef '{pool_ref_name}' not defined in "
                            f"GatewaySettings '{gs_name}'.spec.sourceNATPools"
                        )
                        results["all_routes_covered"] = False
                    else:
                        sp = matched_pools[0]
                        ipam_refs = [r.get("name") for r in sp.get("ipamRefs", []) if r.get("name")]
                        egw_entry["ipam_refs"] = ipam_refs
                        pool_cidrs = []

                        # Prioritize Infras in the same namespace
                        for inf in all_infras:
                            if inf.get("metadata", {}).get("namespace") == egw_ns:
                                for ipam_def in inf.get("spec", {}).get("ipams", []):
                                    if ipam_def.get("name") in ipam_refs:
                                        for pool in ipam_def.get("ipPools", []):
                                            if pool.get("cidr") and pool.get("cidr") not in pool_cidrs:
                                                pool_cidrs.append(pool.get("cidr"))
                        if not pool_cidrs:
                            for inf in all_infras:
                                for ipam_def in inf.get("spec", {}).get("ipams", []):
                                    if ipam_def.get("name") in ipam_refs:
                                        for pool in ipam_def.get("ipPools", []):
                                            if pool.get("cidr") and pool.get("cidr") not in pool_cidrs:
                                                pool_cidrs.append(pool.get("cidr"))

                        egw_entry["pool_cidrs"] = pool_cidrs
                        if not pool_cidrs:
                            egw_entry["status"] = "WARN"
                            egw_entry["message"] = f"No IPAM CIDRs found for IPAM ref(s): {', '.join(ipam_refs)}"
                        else:
                            egw_entry["status"] = "POOL"
                            route_verdicts: Dict[str, Any] = {}
                            for cidr in pool_cidrs:
                                tmm_owned_trans = False
                                tmm_owned_net1 = False
                                for td in tmm_mapping.get("tmm_details", []):
                                    if any(c in td.get("trans_addrs", []) for c in [cidr, cidr.split('/')[0]]):
                                        tmm_owned_trans = True
                                    if any(c in td.get("net1_ips", []) for c in [cidr, cidr.split('/')[0]]):
                                        tmm_owned_net1 = True
                                c_verdict = {
                                    "covered_in_all": True,
                                    "all_next_hops_match": True,
                                    "owned_by_trans": tmm_owned_trans,
                                    "assigned_to_net1": tmm_owned_net1,
                                    "table_coverage": {},
                                }
                                for rt in tables:
                                    rt_id = rt.get("id")
                                    rt_name = rt.get("name", rt_id)
                                    routes = routes_by_table.get(rt_id, [])
                                    matching_route = is_ip_covered_in_routes(cidr, routes)
                                    if matching_route:
                                        nh_addr = matching_route.get("next_hop", {}).get("address", "n/a")
                                        nh_match = (nh_addr in all_tmm_node_ips) if all_tmm_node_ips else True
                                        if not nh_match:
                                            c_verdict["all_next_hops_match"] = False
                                            results["all_next_hops_matched"] = False

                                        c_verdict["table_coverage"][rt_name] = {
                                            "covered": True,
                                            "route_destination": matching_route.get("destination"),
                                            "next_hop": nh_addr,
                                            "route_name": matching_route.get("name"),
                                            "route_id": matching_route.get("id"),
                                            "zone": matching_route.get("zone", {}).get("name", "n/a"),
                                            "next_hop_match": nh_match,
                                        }
                                    else:
                                        c_verdict["table_coverage"][rt_name] = {"covered": False}
                                        c_verdict["covered_in_all"] = False
                                        results["all_routes_covered"] = False

                                route_verdicts[cidr] = c_verdict
                            egw_entry["route_verdicts"] = route_verdicts
                else:
                    egw_entry["status"] = "WARN"
                    egw_entry["message"] = f"Unrecognized SNAT type '{snat_type}' in GatewaySettings '{gs_name}'"

                results["egress_gateways"].append(egw_entry)
        except json.JSONDecodeError:
            pass

    return results


def run_ping_vxlan(
    kube_cli: str,
    infra_ns: Optional[str] = "f5-bnk",
    tmm_mapping: Optional[Dict[str, Any]] = None,
    verbose: bool = False,
    use_color: bool = True,
    cluster_name_or_id: Optional[str] = None,
    cluster_region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send ICMP pings from all worker nodes (via f5-spk-csrc) to all TMM pods.
    - Overlay Ping: When VXLAN is active, attempts overlay ping first across the VXLAN
      tunnel interface (-I <vxlan_dev>) to TMM overlay address.
    - If overlay ping succeeds, underlay ping is skipped for that pair (dataplane is active).
    - If overlay ping fails or VXLAN is not active, tests underlay ping to TMM external
      VLAN address using worker node physical IP (-I <node_ip>).
    - If underlay ping fails, checks if cluster security group allows inbound ICMP.
      If missing, reports as a warning instead of a hard failure.
    - Executes pings concurrently.
    """
    results: Dict[str, Any] = {
        "success": False,
        "all_passed": False,
        "node_count": 0,
        "tmm_count": 0,
        "vxlan_detected": False,
        "vxlan_devices": [],
        "total_pairs": 0,
        "passed_count": 0,
        "warning_count": 0,
        "failed_count": 0,
        "skipped_underlay_count": 0,
        "avg_rtt": None,
        "min_rtt": None,
        "max_rtt": None,
        "infra_ns": infra_ns,
        "results": [],
        "failed_pairs": [],
        "warning_pairs": [],
        "error": None,
    }

    # 1. Find running CSRC pods across all namespaces
    rc_c, out_c, _ = run_cmd([kube_cli, "get", "pods", "-A", "-l", "run=f5-spk-csrc", "-o", "json"], check=False)
    if rc_c != 0 or not out_c.strip():
        rc_c, out_c, _ = run_cmd([kube_cli, "get", "pods", "-A", "-l", "app=f5-spk-csrc", "-o", "json"], check=False)

    if rc_c != 0 or not out_c.strip():
        results["error"] = "No f5-spk-csrc pods found in cluster (checked all namespaces)."
        return results

    try:
        csrc_items = json.loads(out_c).get("items", [])
    except json.JSONDecodeError:
        csrc_items = []

    running_csrc = [p for p in csrc_items if p.get("status", {}).get("phase") == "Running"]
    if not running_csrc:
        results["error"] = "No running f5-spk-csrc pods found in cluster."
        return results

    # 2. Get TMM details / mapping
    if tmm_mapping is None:
        tmm_mapping = get_tmm_node_ip_mapping(kube_cli, infra_ns=infra_ns)

    tmm_details = tmm_mapping.get("tmm_details", [])
    if not tmm_details:
        results["error"] = f"No running f5-tmm pods found in namespace '{infra_ns}'." if infra_ns else "No running f5-tmm pods found in cluster."
        return results

    results["node_count"] = len(running_csrc)
    results["tmm_count"] = len(tmm_details)

    # 3. Check if VXLAN is created on the worker nodes
    first_csrc = running_csrc[0]
    fc_ns = first_csrc.get("metadata", {}).get("namespace", "f5-utils")
    fc_name = first_csrc.get("metadata", {}).get("name")
    rc_vx, out_vx, _ = run_cmd(
        [kube_cli, "exec", "-n", fc_ns, fc_name, "-c", "spk-csrc", "--", "ip", "-d", "link", "show", "type", "vxlan"],
        check=False,
        timeout=10,
    )
    vxlan_detected = (rc_vx == 0 and ("vxlan id" in out_vx or "bnkvxlan" in out_vx))
    results["vxlan_detected"] = vxlan_detected

    node_vxlan_info: Dict[str, List[Dict[str, Any]]] = {}
    if vxlan_detected:
        def inspect_node_vxlan(c_pod: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
            pod_name = c_pod.get("metadata", {}).get("name")
            pod_ns = c_pod.get("metadata", {}).get("namespace", "f5-utils")
            node_key = c_pod.get("spec", {}).get("nodeName")
            rc_l, out_l, _ = run_cmd(
                [kube_cli, "exec", "-n", pod_ns, pod_name, "-c", "spk-csrc", "--", "ip", "-d", "link", "show", "type", "vxlan"],
                check=False,
                timeout=10,
            )
            devs = []
            if rc_l == 0:
                for line in out_l.splitlines():
                    line = line.strip()
                    parts = line.split(":")
                    if len(parts) >= 2 and parts[0].isdigit():
                        d_name = parts[1].strip().split("@")[0]
                        if d_name and d_name not in [d["dev"] for d in devs]:
                            devs.append({"dev": d_name})

            for d in devs:
                d_name = d["dev"]
                rc_a, out_a, _ = run_cmd(
                    [kube_cli, "exec", "-n", pod_ns, pod_name, "-c", "spk-csrc", "--", "ip", "-4", "addr", "show", "dev", d_name],
                    check=False,
                    timeout=10,
                )
                m_a = re.search(r"inet\s+([0-9.]+)/\d+", out_a) if rc_a == 0 else None
                d["overlay_ip"] = m_a.group(1) if m_a else None

                rc_r, out_r, _ = run_cmd(
                    [kube_cli, "exec", "-n", pod_ns, pod_name, "-c", "spk-csrc", "--", "ip", "-4", "route", "show", "dev", d_name],
                    check=False,
                    timeout=10,
                )
                routes = []
                if rc_r == 0:
                    for rl in out_r.splitlines():
                        rp = rl.split()
                        if rp and not rp[0].startswith("default") and "/" not in rp[0]:
                            routes.append(rp[0])
                d["remote_overlay_ips"] = routes
            return node_key, devs

        with ThreadPoolExecutor(max_workers=min(len(running_csrc), 8)) as ex:
            for n_key, d_list in ex.map(inspect_node_vxlan, running_csrc):
                node_vxlan_info[n_key] = d_list
                for d in d_list:
                    if d["dev"] not in results["vxlan_devices"]:
                        results["vxlan_devices"].append(d["dev"])

    # 4. Build primary ping tasks
    # Strategy:
    # - If VXLAN overlay is detected and an overlay route exists on this node for this TMM pod,
    #   we test overlay FIRST.
    # - If overlay passes, we skip the underlay ping for this pair.
    # - If overlay fails, we fall back to test underlay for this pair.
    # - If VXLAN overlay is not detected or no overlay route exists for this TMM pod,
    #   we test underlay directly.
    primary_tasks = []
    for c_pod in running_csrc:
        c_ns = c_pod.get("metadata", {}).get("namespace", "f5-utils")
        c_name = c_pod.get("metadata", {}).get("name")
        c_node = c_pod.get("spec", {}).get("nodeName")
        c_ip = c_pod.get("status", {}).get("podIP")
        dev_list = node_vxlan_info.get(c_node, []) if vxlan_detected else []

        for td in tmm_details:
            t_name = td.get("pod_name")
            t_node = td.get("node_name")
            is_colocated = (c_node == t_node)
            colocated_desc = "co-located" if is_colocated else "cross-node"

            # Underlay target IP (external VLAN)
            t_ext_ip = None
            if td.get("net1_ips"):
                t_ext_ip = td["net1_ips"][0]
            elif td.get("self_ips"):
                t_ext_ip = td["self_ips"][0]
            else:
                t_ext_ip = td.get("pod_ip")

            cmd_underlay = [
                kube_cli, "exec", "-n", c_ns, c_name, "-c", "spk-csrc", "--",
                "ping", "-c", "1", "-W", "2",
            ]
            if c_ip:
                cmd_underlay.extend(["-I", c_ip])
            cmd_underlay.append(t_ext_ip)

            underlay_task_spec = {
                "csrc_namespace": c_ns,
                "csrc_pod": c_name,
                "node_name": c_node,
                "node_ip": c_ip,
                "tmm_pod": t_name,
                "tmm_node": t_node,
                "dst_ip": t_ext_ip,
                "src_ip": c_ip or "default",
                "ping_type": "underlay",
                "is_colocated": is_colocated,
                "tunnel_desc": f"Node underlay -> TMM external VLAN ({colocated_desc})",
                "cmd": cmd_underlay,
            }

            overlay_task = None
            if vxlan_detected:
                for d in dev_list:
                    dev_name = d["dev"]
                    node_ov_ip = d.get("overlay_ip") or "vxlan"
                    for rem_ip in d.get("remote_overlay_ips", []):
                        if rem_ip in td.get("vxlan_self_ips", []) or rem_ip in td.get("self_ips", []):
                            cmd_overlay = [
                                kube_cli, "exec", "-n", c_ns, c_name, "-c", "spk-csrc", "--",
                                "ping", "-c", "1", "-W", "2", "-I", dev_name, rem_ip,
                            ]
                            overlay_task = {
                                "csrc_namespace": c_ns,
                                "csrc_pod": c_name,
                                "node_name": c_node,
                                "node_ip": c_ip,
                                "tmm_pod": t_name,
                                "tmm_node": t_node,
                                "dst_ip": rem_ip,
                                "src_ip": node_ov_ip,
                                "src_iface": dev_name,
                                "ping_type": "overlay",
                                "is_colocated": is_colocated,
                                "tunnel_desc": f"Node VXLAN ({dev_name}) -> TMM overlay ({colocated_desc})",
                                "cmd": cmd_overlay,
                                "fallback_underlay": underlay_task_spec,
                            }
                            break
                    if overlay_task:
                        break

            if overlay_task:
                primary_tasks.append(overlay_task)
            else:
                primary_tasks.append(underlay_task_spec)

    # 5. Concurrently execute ping from worker nodes to TMM pods
    def ping_pair(task: Dict[str, Any]) -> Dict[str, Any]:
        res = task.copy()
        rc_p, out_p, err_p = run_cmd(task["cmd"], check=False, timeout=10)
        res["returncode"] = rc_p
        if rc_p == 0:
            res["success"] = True
            res["rtt_ms"] = None
            m = re.search(r"rtt min/avg/max/mdev = ([0-9.]+)/([0-9.]+)/([0-9.]+)/", out_p)
            if m:
                res["rtt_ms"] = float(m.group(2))
        else:
            res["success"] = False
            res["rtt_ms"] = None
            res["error"] = err_p.strip() or out_p.strip() or f"Exited with code {rc_p}"
            # If underlay ping fails, run "ip route get <dst_ip>" to confirm path
            if task.get("ping_type") == "underlay" and task.get("dst_ip"):
                c_ns = task.get("csrc_namespace", "f5-utils")
                c_pod = task.get("csrc_pod")
                route_cmd = [
                    kube_cli, "exec", "-n", c_ns, c_pod, "-c", "spk-csrc", "--",
                    "ip", "route", "get", task["dst_ip"],
                ]
                rc_r, out_r, err_r = run_cmd(route_cmd, check=False, timeout=10)
                raw_out = (out_r.strip() or err_r.strip())
                if raw_out:
                    first_line = raw_out.splitlines()[0].strip()
                    first_line = re.sub(r"\s+uid\s+\d+", "", first_line)
                    res["route_path"] = first_line
        return res

    with ThreadPoolExecutor(max_workers=min(len(primary_tasks), 24)) as ex:
        primary_results = list(ex.map(ping_pair, primary_tasks))

    # Evaluate results:
    # If overlay passed, skip underlay.
    # If overlay failed, execute fallback underlay ping.
    fallback_tasks = []
    skipped_underlay_count = 0
    final_results = []

    for r in primary_results:
        if r.get("ping_type") == "overlay":
            if r["success"]:
                skipped_underlay_count += 1
                final_results.append(r)
            else:
                final_results.append(r)
                if r.get("fallback_underlay"):
                    fallback_tasks.append(r["fallback_underlay"])
        else:
            final_results.append(r)

    if fallback_tasks:
        with ThreadPoolExecutor(max_workers=min(len(fallback_tasks), 12)) as ex:
            fallback_results = list(ex.map(ping_pair, fallback_tasks))
            final_results.extend(fallback_results)

    # Check Security Group for any failed underlay checks
    failed_underlays = [r for r in final_results if not r["success"] and r.get("ping_type") == "underlay"]
    if failed_underlays and cluster_name_or_id:
        sg_icmp = check_security_group_for_icmp(cluster_name_or_id, cluster_region or "")
        if not sg_icmp.get("icmp_allowed", False):
            sg_id = sg_icmp.get("cluster_sg_id")
            sg_name = sg_icmp.get("cluster_sg_name") or sg_id or "Cluster Security Group"
            for r in failed_underlays:
                r["is_warning"] = True
                r["warn_reason"] = f"Inbound ICMP not allowed in cluster security group '{sg_name}' ({sg_id})"

    final_results.sort(key=lambda x: (x.get("node_name", ""), x.get("tmm_pod", ""), x.get("ping_type", "")))

    passed = [p for p in final_results if p["success"]]
    warnings = [p for p in final_results if not p["success"] and p.get("is_warning", False)]
    failed = [p for p in final_results if not p["success"] and not p.get("is_warning", False)]

    results["results"] = final_results
    results["total_pairs"] = len(final_results)
    results["passed_count"] = len(passed)
    results["warning_count"] = len(warnings)
    results["failed_count"] = len(failed)
    results["skipped_underlay_count"] = skipped_underlay_count
    results["failed_pairs"] = failed
    results["warning_pairs"] = warnings

    rtts = [p["rtt_ms"] for p in passed if p["rtt_ms"] is not None]
    if rtts:
        results["avg_rtt"] = sum(rtts) / len(rtts)
        results["min_rtt"] = min(rtts)
        results["max_rtt"] = max(rtts)

    results["all_passed"] = (len(failed) == 0 and len(passed) > 0)
    results["success"] = results["all_passed"]

    return results


def run_diagnostics(
    cluster_name_or_id: str,
    custom_port: Optional[int] = None,
    check_routes: bool = False,
    ping_vxlan: bool = False,
    verbose: bool = False,
    use_color: bool = True,
) -> int:
    """Run full BNK diagnostic checks against the specified cluster."""
    start_time = time.perf_counter()
    ensure_ibmcloud_login()

    print("\n" + "=" * 80)
    print(colorize(f"BNK DIAGNOSTICS FOR CLUSTER: {cluster_name_or_id}", Colors.BOLD, use_color))
    print("=" * 80)

    # 1. Cluster Information
    print("\n" + colorize("1. Fetching IBM Cloud Cluster Information...", Colors.CYAN, use_color))
    c_info = get_cluster_details(cluster_name_or_id)
    c_id = c_info.get("id")
    c_name = c_info.get("name")
    c_region = c_info.get("region")
    c_state = c_info.get("state")
    c_ver = c_info.get("masterKubeVersion", "").split("_")[0]
    c_vpcs = c_info.get("vpcs", [])
    vpc_id = c_vpcs[0] if c_vpcs else "unknown"

    print(f"  • Cluster Name:    {c_name}")
    print(f"  • Cluster ID:      {c_id}")
    print(f"  • Region:          {c_region}")
    print(f"  • VPC ID:          {vpc_id}")
    print(f"  • State:           {c_state}")
    print(f"  • OpenShift Ver:   {c_ver}")

    # 2. Kubeconfig and Cluster Access
    print("\n" + colorize("2. Verifying Kubernetes / OpenShift Connectivity...", Colors.CYAN, use_color))
    kube_cli = get_kube_cli() or "kubectl"
    ensure_kubeconfig(c_id or c_name, kube_cli=kube_cli, cluster_region=c_region)
    rc, out, _ = run_cmd([kube_cli, "get", "nodes", "--no-headers"], check=False)
    if rc == 0:
        node_count = len(out.strip().splitlines())
        print(f"  [{colorize('PASS', Colors.GREEN, use_color)}] Connected to cluster API via '{kube_cli}'. Worker nodes ready: {node_count}")
    else:
        print(f"  [{colorize('WARN', Colors.YELLOW, use_color)}] Unable to retrieve nodes via '{kube_cli}'. Will attempt CR checks.")

    # 3. BNK CR & Infra Check
    print("\n" + colorize("3. Checking F5 BIG-IP Next for Kubernetes (BNK) CRDs & Infra...", Colors.CYAN, use_color))
    bnk_res = check_bnk_cr(use_color=use_color, kube_cli=kube_cli)

    if not bnk_res["crd_installed"]:
        print(f"  [{colorize('FAIL', Colors.RED, use_color)}] BNK Infra CRD (infras.gateway.k8s.f5.com) is NOT installed on this cluster.")
        print("         Make sure F5 Lifecycle Operator (FLO) or BNK CRDs are deployed.")
    else:
        print(f"  [{colorize('PASS', Colors.GREEN, use_color)}] BNK Infra CRD (infras.gateway.k8s.f5.com) is installed.")

    if not bnk_res["infra_found"]:
        print(f"  [{colorize('WARN', Colors.YELLOW, use_color)}] No Infra CR found on this cluster.")
    else:
        print(f"  [{colorize('PASS', Colors.GREEN, use_color)}] Infra CR found: '{bnk_res['infra_name']}' in namespace '{bnk_res['infra_namespace']}'")
        for ctype, cdata in bnk_res["conditions"].items():
            status_symbol = Colors.GREEN if cdata["status"] == "True" else Colors.YELLOW
            status_text = colorize(cdata['status'], status_symbol, use_color)
            msg = cdata.get("message")
            line_str = f"    • Condition {ctype:<13}: {status_text}"
            if msg:
                if len(line_str) + len(msg) + 3 <= 80:
                    print(f"{line_str} ({msg})")
                else:
                    print(line_str)
                    print(f"      Message: {msg}")
            else:
                print(line_str)

    # Determine VXLAN ports to check
    ports_to_check: List[int] = []
    if custom_port:
        ports_to_check.append(custom_port)
        print(f"  • VXLAN Port to check (user-override via --vxlan-port): {colorize(str(custom_port), Colors.BOLD, use_color)}")
        if bnk_res["vxlan_ports"]:
            print(f"    (Note: Port defined in Infra CR '{bnk_res['infra_name']}' is {', '.join(map(str, bnk_res['vxlan_ports']))})")
    elif bnk_res["infra_found"] and bnk_res["vxlan_ports"]:
        ports_to_check = bnk_res["vxlan_ports"]
        print(f"  • VXLAN Port(s) retrieved from Infra CR '{bnk_res['infra_name']}': {colorize(', '.join(map(str, ports_to_check)), Colors.BOLD, use_color)}")
        seen_src = set()
        for p_src in bnk_res["port_sources"]:
            src_str = p_src["source"]
            if src_str not in seen_src:
                seen_src.add(src_str)
                print(f"    - {src_str}")
    elif bnk_res["infra_found"]:
        ports_to_check = [4789]
        print(f"  • Infra CR '{bnk_res['infra_name']}' found, but does not define an explicit VXLAN port.")
        print(f"    Using BNK standard default VXLAN port: {colorize('4789', Colors.BOLD, use_color)}")
    else:
        ports_to_check = [4789]
        print(f"  • [{colorize('WARN', Colors.YELLOW, use_color)}] No Infra CR found to retrieve VXLAN port from.")
        print(f"    Defaulting check to standard VXLAN port: {colorize('4789', Colors.BOLD, use_color)}")

    # 4. Security Group Check
    print("\n" + colorize("4. Checking IBM Cloud VPC Security Group Inbound Rules...", Colors.CYAN, use_color))
    sg_res = check_security_group_for_port(c_id or c_name, ports_to_check, c_region)
    print(f"  • Security Group:  {sg_res['cluster_sg_name']}")
    print(f"    ID:              {sg_res['cluster_sg_id']}")
    print(f"  • Rules Inspected: {sg_res['rules_checked']}")

    all_ports_open = True
    for port, p_info in sg_res["port_verdicts"].items():
        if p_info["open"]:
            print(f"\n  [{colorize('PASS', Colors.GREEN, use_color)}] Inbound UDP Port {colorize(str(port), Colors.BOLD, use_color)} is OPEN in cluster security group.")
            print("         Matching Inbound Rule(s):")
            for r in p_info["matching_rules"]:
                print(f"         - Rule ID:   {r['rule_id']}")
                print(f"           Name:      {r['rule_name']}")
                print(f"           Protocol:  {r['protocol']}")
                print(f"           Ports:     {r['port_range']}")
                print(f"           Remote:    {r['remote']}")
        else:
            all_ports_open = False
            print(f"\n  [{colorize('FAIL', Colors.RED, use_color)}] Inbound UDP Port {colorize(str(port), Colors.BOLD, use_color)} is NOT OPEN in cluster security group.")
            print("         VXLAN encapsulation traffic across cluster nodes will be blocked!")
            print(f"  [{colorize('RECOMMENDATION', Colors.YELLOW, use_color)}] Add inbound security group rule to allow port {port}/udp:")
            sg_id = sg_res['cluster_sg_id']
            print(f"         ibmcloud is security-group-rule-add {sg_id} \\")
            print(f"           inbound udp --port-min {port} --port-max {port} --remote 0.0.0.0/0")
            print("         (Tip: Replace 0.0.0.0/0 with cluster VPC or worker subnet CIDR)")

    # 5. Optional Ping VXLAN Check (all nodes to all TMMs)
    ping_passed = True
    ping_warned = False
    tmm_ns = bnk_res.get("infra_namespace", "f5-bnk")
    tmm_mapping = None
    if ping_vxlan or check_routes:
        tmm_mapping = get_tmm_node_ip_mapping(kube_cli, infra_ns=tmm_ns)

    if ping_vxlan:
        print("\n" + colorize("5. Testing VXLAN / Dataplane Ping (All Nodes -> All TMMs)...", Colors.CYAN, use_color))
        p_res = run_ping_vxlan(
            kube_cli=kube_cli,
            infra_ns=tmm_ns,
            tmm_mapping=tmm_mapping,
            verbose=verbose,
            use_color=use_color,
            cluster_name_or_id=c_id or c_name,
            cluster_region=c_region,
        )
        if p_res.get("error"):
            ping_passed = False
            print(f"  [{colorize('FAIL', Colors.RED, use_color)}] Ping test setup failed: {p_res['error']}")
        else:
            print(f"  • Worker Nodes (CSRC pods):  {p_res['node_count']}")
            print(f"  • TMM Pods (namespace: {tmm_ns}): {p_res['tmm_count']}")
            if p_res.get("vxlan_detected"):
                vx_devs = ", ".join(p_res.get("vxlan_devices", []))
                print(f"  • VXLAN Tunnels:             Detected ({vx_devs} - testing Overlay first, Underlay skipped on Overlay pass)")
            else:
                print(f"  • VXLAN Tunnels:             Not created on nodes (testing Underlay to TMM external VLAN VTEPs only)")
            if p_res.get("skipped_underlay_count", 0) > 0:
                print(f"  • Total Ping Checks Tested:  {p_res['total_pairs']} ({p_res['skipped_underlay_count']} underlay check(s) skipped as overlay passed)")
            else:
                print(f"  • Total Ping Checks Tested:  {p_res['total_pairs']}")

            if verbose and p_res.get("results"):
                print("\n  " + colorize("Individual Node -> TMM Ping Results:", Colors.BOLD, use_color))
                last_node = None
                for r in p_res["results"]:
                    cur_node = r.get("node_name")
                    if last_node is not None and cur_node != last_node:
                        print("    --")
                    last_node = cur_node

                    if r.get("is_warning"):
                        status_str = colorize("WARN", Colors.YELLOW, use_color)
                    elif r["success"]:
                        status_str = colorize("PASS", Colors.GREEN, use_color)
                    else:
                        status_str = colorize("FAIL", Colors.RED, use_color)

                    rtt_val = f"RTT: {r['rtt_ms']:.2f} ms" if r["rtt_ms"] is not None else (r.get("error") or "timeout")
                    s_node = short_name(r['node_name'], 12)
                    s_tmm = short_name(r['tmm_pod'], 9)
                    dst_ip = r['dst_ip']
                    p_type = r['ping_type']
                    is_colo = r['is_colocated']

                    if p_res.get("vxlan_detected"):
                        type_tag = f"[{'und' if p_type == 'underlay' else 'ovr'}/{'local' if is_colo else 'cross'}]"
                        print(f"    [{status_str}] {s_node:<12} -> {s_tmm:<9} ({dst_ip:<15}) {type_tag:<11} {rtt_val}")
                    else:
                        colocated_tag = "[co-located]" if is_colo else "[cross-node]"
                        print(f"    [{status_str}] {s_node:<12} -> {s_tmm:<9} ({dst_ip:<15}) {colocated_tag:<12} {rtt_val}")

                    if r.get("warn_reason"):
                        reason_label = colorize("Reason:", Colors.YELLOW, use_color)
                        print(f"           {reason_label} {r['warn_reason']}")

                    if not r["success"] and r.get("route_path"):
                        path_label = colorize("Path:", Colors.CYAN, use_color)
                        print(f"           {path_label} {r['route_path']}")

            avg_str = f"{p_res['avg_rtt']:.2f} ms" if p_res['avg_rtt'] is not None else "N/A"
            min_str = f"{p_res['min_rtt']:.2f} ms" if p_res['min_rtt'] is not None else "N/A"
            max_str = f"{p_res['max_rtt']:.2f} ms" if p_res['max_rtt'] is not None else "N/A"

            if p_res["all_passed"]:
                if p_res.get("warning_count", 0) > 0:
                    ping_warned = True
                    print(f"\n  [{colorize('WARN', Colors.YELLOW, use_color)}] {p_res['passed_count']} ping check(s) passed, {p_res['warning_count']} underlay check(s) had warnings (0 hard failures).")
                else:
                    print(f"\n  [{colorize('PASS', Colors.GREEN, use_color)}] All {p_res['total_pairs']} dataplane ping check(s) passed! (0% packet loss)")
                if p_res.get("skipped_underlay_count", 0) > 0:
                    print(f"         ({p_res['skipped_underlay_count']} underlay check(s) skipped as overlay passed)")
                print(f"         RTT Stats: Avg: {colorize(avg_str, Colors.BOLD, use_color)} | Min: {min_str} | Max: {max_str}")
            else:
                ping_passed = False
                print(f"\n  [{colorize('FAIL', Colors.RED, use_color)}] {p_res['failed_count']} out of {p_res['total_pairs']} ping check(s) failed!")
                if p_res.get("warning_count", 0) > 0:
                    print(f"         ({p_res['warning_count']} check(s) flagged as warnings)")
                if not verbose:
                    print("         Failed checks:")
                    for f in p_res["failed_pairs"]:
                        s_fnode = short_name(f['node_name'], 12)
                        s_ftmm = short_name(f['tmm_pod'], 9)
                        p_type = f['ping_type']
                        is_colo = f['is_colocated']
                        type_tag = f"[{'und' if p_type == 'underlay' else 'ovr'}/{'local' if is_colo else 'cross'}]" if p_res.get("vxlan_detected") else ("[co-located]" if is_colo else "[cross-node]")
                        print(f"         - {s_fnode} -> {s_ftmm} ({f['dst_ip']}) {type_tag}: {f.get('error', 'timeout')}")
                        if f.get("route_path"):
                            path_label = colorize("Path:", Colors.CYAN, use_color)
                            print(f"           {path_label} {f['route_path']}")
                    for w in p_res.get("warning_pairs", []):
                        s_wnode = short_name(w['node_name'], 12)
                        s_wtmm = short_name(w['tmm_pod'], 9)
                        p_type = w['ping_type']
                        is_colo = w['is_colocated']
                        type_tag = f"[{'und' if p_type == 'underlay' else 'ovr'}/{'local' if is_colo else 'cross'}]" if p_res.get("vxlan_detected") else ("[co-located]" if is_colo else "[cross-node]")
                        print(f"         - [WARN] {s_wnode} -> {s_wtmm} ({w['dst_ip']}) {type_tag}: {w.get('warn_reason', 'warning')}")
                    print("         (Tip: Re-run with -v/--verbose to inspect all individual pairs)")
                if not p_res.get("vxlan_detected"):
                    has_colocated_fail = any(f.get("is_colocated") for f in p_res["failed_pairs"])
                    if has_colocated_fail:
                        print("         Note: On co-located nodes without VXLAN tunnel, local traffic to TMM external VLAN IP")
                        print("               routes via shim0/internal-vlan where cross-VLAN ICMP echo is rejected by TMM.")

    # 6. Optional VPC Route Tables & IPAM Propagation Check
    routes_passed = True
    next_hops_passed = True
    if check_routes:
        print("\n" + colorize("6. Checking VPC Routing Tables & BNK Route Propagation...", Colors.CYAN, use_color))
        rts_info = fetch_vpc_routing_tables(vpc_id, c_region)
        table_count = rts_info["table_count"]
        tables = rts_info["tables"]
        print(f"  • Cluster VPC ID:        {vpc_id}")
        print(f"  • VPC Region:            {c_region}")
        print(f"  • Routing Tables Found:  {table_count} (expected: 2)")

        if table_count == 2:
            print(f"  [{colorize('PASS', Colors.GREEN, use_color)}] Exactly 2 routing tables found associated with VPC.")
        elif table_count > 2:
            print(f"  [{colorize('WARN', Colors.YELLOW, use_color)}] Found {table_count} routing tables associated with VPC (expected 2: one default table and one cluster-specific table).")
        else:
            print(f"  [{colorize('WARN', Colors.YELLOW, use_color)}] Found {table_count} routing table(s) associated with VPC (expected 2).")

        for idx, rt in enumerate(tables, start=1):
            rt_id = rt.get("id")
            rt_name = rt.get("name", rt_id)
            is_def = rt.get("is_default", False)
            routes = rts_info["routes_by_table"].get(rt_id, [])
            def_badge = "Default VPC Table" if is_def else "Cluster Table"
            print(f"\n    {idx}. Routing Table: {colorize(rt_name, Colors.BOLD, use_color)}")
            print(f"       ID:     {rt_id}")
            print(f"       Routes: {len(routes)} ({def_badge})")
            for r in routes:
                r_dest = r.get("destination", "unknown")
                r_nh = r.get("next_hop", {}).get("address", "n/a")
                r_zone = r.get("zone", {}).get("name", "n/a")
                if verbose:
                    r_name = r.get("name", "n/a")
                    r_id = r.get("id", "n/a")
                    print(f"       - {r_dest:<18} via {r_nh:<15} (zone: {r_zone})")
                    print(f"         Route: {r_name} [{r_id}]")
                else:
                    print(f"       - {r_dest:<18} via {r_nh:<15} (zone: {r_zone})")

        # Build TMM -> Worker Node mapping
        if tmm_mapping is None:
            tmm_ns = bnk_res.get("infra_namespace", "f5-bnk")
            tmm_mapping = get_tmm_node_ip_mapping(kube_cli, infra_ns=tmm_ns)
        if verbose and tmm_mapping["tmm_details"]:
            print(f"\n    • Discovered {len(tmm_mapping['tmm_details'])} TMM pods across {len(tmm_mapping['node_ip_map'])} worker nodes:")
            for td in tmm_mapping["tmm_details"]:
                net1_str = ", ".join(td.get('net1_ips', [])) if td.get('net1_ips') else "None"
                self_str = ", ".join(td.get('self_ips', [])) if td.get('self_ips') else "None"
                vs_str = ", ".join(td.get('vs_vips', [])) if td.get('vs_vips') else "None"
                print(f"      - Pod:            {td['pod_name']}")
                print(f"        Node:           {td['node_name']}")
                print(f"        Node IP:        {td['node_ip']}")
                print(f"        net1 IPs:       {net1_str}")
                print(f"        self_ip:        {self_str}")
                if td.get('vs_vips'):
                    print(f"        virtual_server: {vs_str}")
                if td.get('trans_addrs'):
                    trans_str = ", ".join(td['trans_addrs'])
                    print(f"        trans_addr:     {trans_str}")

        # 6.1 Infra CR VLAN & TMM address check
        print("\n  " + colorize("6.1 Verifying Infra CR VLAN Addresses & IPAM Propagation...", Colors.CYAN, use_color))
        infra_rts = check_infra_routes_and_ipam(kube_cli, rts_info, tmm_mapping, use_color=use_color)
        if not infra_rts["infra_found"]:
            print(f"    [{colorize('WARN', Colors.YELLOW, use_color)}] No Infra CR found to verify VLAN routes.")
        else:
            if not infra_rts["all_next_hops_matched"]:
                next_hops_passed = False

            for ventry in infra_rts["vlan_networks"]:
                print(f"    • Infra '{ventry['infra_name']}' -> VLAN Network '{ventry['vlan_name']}'")
                print(f"      - F5 IPAM CR: '{ventry['ipam_cr_name']}' ({len(ventry['ipam_ips'])} allocated IPs)")
                tmm_count = len(ventry['tmm_pod_ips'])
                print(f"      - TMM Pods verified: {tmm_count}")
                if ventry["ipam_match"]:
                    print(f"      [{colorize('PASS', Colors.GREEN, use_color)}] Assigned TMM pod VLAN addresses match f5-ipam CR status.")
                else:
                    routes_passed = False
                    print(f"      [{colorize('FAIL', Colors.RED, use_color)}] Mismatch between TMM pod VLAN addresses and f5-ipam CR status!")

                for addr, vdata in ventry["route_verdicts"].items():
                    exp_nh = vdata.get("expected_next_hop")
                    tmm_p = vdata.get("tmm_pod")
                    nh_status = vdata.get("all_next_hops_match", True)
                    if vdata["covered_in_all"]:
                        if exp_nh:
                            if nh_status:
                                nh_badge = colorize("PASS", Colors.GREEN, use_color)
                                nh_info = f"Next-Hop: {exp_nh} [{nh_badge}]"
                            else:
                                nh_badge = colorize("FAIL", Colors.RED, use_color)
                                nh_info = f"Next-Hop mismatch: expected {exp_nh} [{nh_badge}]"
                                next_hops_passed = False
                        else:
                            if nh_status:
                                nh_badge = colorize("PASS", Colors.GREEN, use_color)
                                nh_info = f"Next-Hop: valid TMM node [{nh_badge}]"
                            else:
                                nh_badge = colorize("FAIL", Colors.RED, use_color)
                                nh_info = f"Next-Hop mismatch: not a TMM node [{nh_badge}]"
                                next_hops_passed = False

                        print(f"      [{colorize('PASS', Colors.GREEN, use_color)}] VLAN Address: {colorize(addr, Colors.BOLD, use_color)} ({nh_info})")
                        if exp_nh and tmm_p:
                            print(f"         Hosting Pod:   {tmm_p}")

                        if verbose:
                            owned_net1 = vdata.get("assigned_to_net1", False)
                            owned_self = vdata.get("owned_by_self_ip", False)
                            own_parts = []
                            if owned_self:
                                own_parts.append("self_ip")
                            if owned_net1:
                                own_parts.append("net1")
                            if own_parts:
                                own_txt = " + ".join(own_parts)
                                print(f"         TMM Ownership: Confirmed ({own_txt})")
                            elif tmm_p:
                                print("         TMM Ownership: Unconfirmed in TMM")

                        gw_ip = vdata.get("subnet_gw_ip")
                        if gw_ip:
                            gw_res = vdata.get("subnet_gw_reserved", False)
                            if gw_res:
                                gw_badge = colorize("PASS", Colors.GREEN, use_color)
                                print(f"         Subnet GW (.1): {gw_ip} reserved in F5 IPAM [{gw_badge}]")
                            else:
                                gw_badge = colorize("WARN", Colors.YELLOW, use_color)
                                print(f"         Subnet GW (.1): {gw_ip} NOT reserved in F5 IPAM [{gw_badge}]")

                        if verbose:
                            print("         Covered in both routing tables:")
                            for rt_n, c_sub in vdata["table_coverage"].items():
                                if c_sub.get("covered"):
                                    sub_nh_m = c_sub.get("next_hop_match", False)
                                    sub_nh_badge = colorize("PASS", Colors.GREEN, use_color) if sub_nh_m else colorize("FAIL", Colors.RED, use_color)
                                    print(f"         • Table: {rt_n}")
                                    print(f"           Route: {c_sub['route_destination']} via {c_sub['next_hop']} (zone: {c_sub.get('zone', 'n/a')}) [{sub_nh_badge}]")
                                    print(f"           Name:  {c_sub.get('route_name', 'n/a')}")
                                    print(f"           ID:    {c_sub.get('route_id', 'n/a')}")
                        else:
                            for rt_n, c_sub in vdata["table_coverage"].items():
                                if c_sub.get("covered"):
                                    print(f"         • {rt_n:<36}: {c_sub['route_destination']} (via {c_sub['next_hop']})")
                    else:
                        routes_passed = False
                        print(f"      [{colorize('FAIL', Colors.RED, use_color)}] VLAN Address: {colorize(addr, Colors.BOLD, use_color)}")
                        print("             NOT covered in all routing tables!")

        # 6.2 Infra CR Static Routes check
        print("\n  " + colorize("6.2 Verifying Infra CR Static Routes & TMM Routing Configuration...", Colors.CYAN, use_color))
        sr_rts = check_infra_static_routes(kube_cli, tmm_mapping, use_color=use_color, verbose=verbose)
        if not sr_rts["infra_found"]:
            print(f"    [{colorize('WARN', Colors.YELLOW, use_color)}] No Infra CR found to verify static routes.")
        elif not sr_rts["has_static_routes"]:
            print(f"    [{colorize('INFO', Colors.BLUE, use_color)}] No staticRoutes configured in Infra CR.")
        else:
            if not sr_rts["all_passed"]:
                routes_passed = False

        # 6.3 Gateway CR VIP address check
        print("\n  " + colorize("6.3 Verifying Gateway CR VIP Addresses & IPAM Propagation...", Colors.CYAN, use_color))
        gw_rts = check_gateway_routes_and_ipam(kube_cli, rts_info, tmm_mapping, use_color=use_color)
        if gw_rts["gateway_count"] == 0:
            print(f"    [{colorize('INFO', Colors.BLUE, use_color)}] No Gateway CRs found in cluster.")
        else:
            if not gw_rts["all_next_hops_matched"]:
                next_hops_passed = False

            for gentry in gw_rts["gateways"]:
                print(f"    • Gateway '{gentry['name']}' (namespace: {gentry['namespace']}):")
                print(f"      - VIPs ({len(gentry['vips'])}): {', '.join(gentry['vips']) if gentry['vips'] else 'None'}")
                print(f"      - F5 IPAM CR: '{gentry['ipam_cr_name']}'")
                if gentry["ipam_match"]:
                    print(f"      [{colorize('PASS', Colors.GREEN, use_color)}] Gateway VIP addresses match f5-ipam CR status.")
                else:
                    routes_passed = False
                    print(f"      [{colorize('FAIL', Colors.RED, use_color)}] Gateway VIP addresses differ from f5-ipam CR status!")

                for vip, vdata in gentry["route_verdicts"].items():
                    exp_nh = vdata.get("expected_next_hop")
                    tmm_p = vdata.get("tmm_pod")
                    nh_status = vdata.get("all_next_hops_match", True)
                    if vdata["covered_in_all"]:
                        if exp_nh:
                            if nh_status:
                                nh_badge = colorize("PASS", Colors.GREEN, use_color)
                                nh_info = f"Next-Hop: {exp_nh} [{nh_badge}]"
                            else:
                                nh_badge = colorize("FAIL", Colors.RED, use_color)
                                nh_info = f"Next-Hop mismatch: expected {exp_nh} [{nh_badge}]"
                                next_hops_passed = False
                        else:
                            if nh_status:
                                nh_badge = colorize("PASS", Colors.GREEN, use_color)
                                nh_info = f"Next-Hop: valid TMM node [{nh_badge}]"
                            else:
                                nh_badge = colorize("FAIL", Colors.RED, use_color)
                                nh_info = f"Next-Hop mismatch: not a TMM node [{nh_badge}]"
                                next_hops_passed = False

                        print(f"      [{colorize('PASS', Colors.GREEN, use_color)}] Gateway VIP: {colorize(vip, Colors.BOLD, use_color)} ({nh_info})")
                        if exp_nh and tmm_p:
                            print(f"         Hosting Pod:   {tmm_p}")

                        if verbose:
                            owned_net1 = vdata.get("assigned_to_net1", False)
                            owned_vs = vdata.get("owned_by_vs", False)
                            own_parts = []
                            if owned_vs:
                                own_parts.append("virtual_server")
                            if owned_net1:
                                own_parts.append("net1")
                            if own_parts:
                                own_txt = " + ".join(own_parts)
                                print(f"         TMM Ownership: Confirmed ({own_txt})")
                            elif tmm_p:
                                print("         TMM Ownership: Unconfirmed in TMM")
                            print("         Covered in both routing tables:")
                            for rt_n, c_sub in vdata["table_coverage"].items():
                                if c_sub.get("covered"):
                                    sub_nh_m = c_sub.get("next_hop_match", False)
                                    sub_nh_badge = colorize("PASS", Colors.GREEN, use_color) if sub_nh_m else colorize("FAIL", Colors.RED, use_color)
                                    print(f"         • Table: {rt_n}")
                                    print(f"           Route: {c_sub['route_destination']} via {c_sub['next_hop']} (zone: {c_sub.get('zone', 'n/a')}) [{sub_nh_badge}]")
                                    print(f"           Name:  {c_sub.get('route_name', 'n/a')}")
                                    print(f"           ID:    {c_sub.get('route_id', 'n/a')}")
                        else:
                            for rt_n, c_sub in vdata["table_coverage"].items():
                                if c_sub.get("covered"):
                                    print(f"         • {rt_n:<36}: {c_sub['route_destination']} (via {c_sub['next_hop']})")
                    else:
                        routes_passed = False
                        print(f"      [{colorize('FAIL', Colors.RED, use_color)}] Gateway VIP: {colorize(vip, Colors.BOLD, use_color)}")
                        print("             NOT covered in all routing tables!")

        # 6.4 EgressGateway CR check
        print("\n  " + colorize("6.4 Verifying EgressGateway CR & SNAT IPAM Propagation...", Colors.CYAN, use_color))
        egw_rts = check_egress_gateway_routes_and_ipam(kube_cli, rts_info, tmm_mapping, use_color=use_color)
        if not egw_rts.get("all_next_hops_matched", True):
            next_hops_passed = False
        if not egw_rts.get("all_routes_covered", True):
            routes_passed = False

        if egw_rts["egress_gateway_count"] == 0:
            print(f"    [{colorize('INFO', Colors.BLUE, use_color)}] No standalone EgressGateway CRs currently deployed in cluster.")
        else:
            for egw in egw_rts["egress_gateways"]:
                egw_name = egw.get("name", "n/a")
                egw_ns = egw.get("namespace", "f5-bnk")
                print(f"    • EgressGateway '{colorize(egw_name, Colors.BOLD, use_color)}' in namespace '{colorize(egw_ns, Colors.BOLD, use_color)}':")
                if egw.get("gs_name"):
                    sec_info = f" (section: '{egw['section_name']}')" if egw.get("section_name") else ""
                    print(f"      - GatewaySettings: '{egw['gs_name']}'{sec_info}")

                status = egw.get("status")
                if status == "SKIP":
                    print(f"      [{colorize('SKIP', Colors.YELLOW, use_color)}] {egw['skip_reason']}")
                elif status == "FAIL":
                    routes_passed = False
                    print(f"      [{colorize('FAIL', Colors.RED, use_color)}] {egw.get('message', 'Validation failed')}")
                elif status == "WARN":
                    print(f"      [{colorize('WARN', Colors.YELLOW, use_color)}] {egw.get('message', 'Warning')}")
                elif status == "POOL":
                    pool_name = egw.get("pool_name", "unknown")
                    ipam_str = ", ".join(egw.get("ipam_refs", []))
                    print(f"      - SNAT Pool: '{pool_name}' (ipamRefs: {ipam_str})")
                    for cidr, cdata in egw.get("route_verdicts", {}).items():
                        if cdata.get("covered_in_all"):
                            nh_m = cdata.get("all_next_hops_match", True)
                            nh_badge = colorize("PASS", Colors.GREEN, use_color) if nh_m else colorize("FAIL", Colors.RED, use_color)
                            print(f"      [{colorize('PASS', Colors.GREEN, use_color)}] SNAT Subnet: {colorize(cidr, Colors.BOLD, use_color)} [Next-Hop: {nh_badge}]")
                            if verbose:
                                owned_net1 = cdata.get("assigned_to_net1", False)
                                owned_trans = cdata.get("owned_by_trans", False)
                                own_parts = []
                                if owned_trans:
                                    own_parts.append("translation_address")
                                if owned_net1:
                                    own_parts.append("net1")
                                if own_parts:
                                    own_txt = " + ".join(own_parts)
                                    print(f"         TMM Ownership: Confirmed ({own_txt})")
                                print("         Covered in both routing tables:")
                                for rt_n, c_sub in cdata["table_coverage"].items():
                                    if c_sub.get("covered"):
                                        sub_nh_m = c_sub.get("next_hop_match", False)
                                        sub_badge = colorize("PASS", Colors.GREEN, use_color) if sub_nh_m else colorize("FAIL", Colors.RED, use_color)
                                        print(f"         • Table: {rt_n}")
                                        print(f"           Route: {c_sub['route_destination']} via {c_sub['next_hop']} (zone: {c_sub.get('zone', 'n/a')}) [{sub_badge}]")
                                        print(f"           Name:  {c_sub.get('route_name', 'n/a')}")
                                        print(f"           ID:    {c_sub.get('route_id', 'n/a')}")
                            else:
                                for rt_n, c_sub in cdata["table_coverage"].items():
                                    if c_sub.get("covered"):
                                        print(f"         • {rt_n:<34}: {c_sub['route_destination']} (via {c_sub['next_hop']})")
                        else:
                            routes_passed = False
                            print(f"      [{colorize('FAIL', Colors.RED, use_color)}] SNAT Subnet: {colorize(cidr, Colors.BOLD, use_color)}")
                            print("             NOT covered in all routing tables!")

    elapsed = time.perf_counter() - start_time
    print("\n" + "=" * 80)
    print(f"Total Execution Time: {elapsed:.2f} seconds")

    healthy = all_ports_open and bnk_res["infra_found"] and ping_passed and routes_passed and next_hops_passed and not ping_warned
    if healthy:
        print(colorize("OVERALL STATUS: HEALTHY", Colors.GREEN, use_color))
        print("  All BNK configurations, next-hop routes, and network connectivity verified.")
        print("=" * 80 + "\n")
        return 0
    elif not all_ports_open:
        print(colorize("OVERALL STATUS: ACTION REQUIRED", Colors.RED, use_color))
        print("  Security Group rule missing for VXLAN traffic.")
        print("  Recommendation: Run the 'ibmcloud is security-group-rule-add' command shown above")
        print("  to allow inbound UDP traffic.")
        print("=" * 80 + "\n")
        return 1
    elif not routes_passed:
        print(colorize("OVERALL STATUS: ACTION REQUIRED", Colors.RED, use_color))
        print("  Route table or IPAM mismatch detected.")
        print("  Recommendation: Check VPC routing tables to ensure routes exist for TMM VLAN, VIP,")
        print("  or SNAT subnets.")
        print("=" * 80 + "\n")
        return 1
    elif not next_hops_passed:
        print(colorize("OVERALL STATUS: ACTION REQUIRED", Colors.RED, use_color))
        print("  Route next-hop does not match the worker node IP hosting TMM.")
        print("  Recommendation: Check that F5 IPAM / cloud route controller is configuring the")
        print("  next-hop to the worker node Internal IP where the TMM pod is running.")
        print("=" * 80 + "\n")
        return 1
    elif not ping_passed:
        print(colorize("OVERALL STATUS: ACTION REQUIRED", Colors.RED, use_color))
        print("  Dataplane ping to TMM failed.")
        print("  Recommendation: Check TMM pod status and node overlay tunnel interfaces.")
        print("=" * 80 + "\n")
        return 1
    else:
        print(colorize("OVERALL STATUS: WARNING", Colors.YELLOW, use_color))
        print("  Review items marked with [WARN] above.")
        print("=" * 80 + "\n")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="IBM Cloud & F5 BIG-IP Next for Kubernetes (BNK) Cluster Checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # List all clusters in IBM Cloud account
  python3 check_bnk.py

  # Run core BNK diagnostic checks on tf-cluster-jp-hk
  python3 check_bnk.py --cluster tf-cluster-jp-hk

  # Send ICMP pings from all worker nodes to all TMMs over VXLAN/dataplane tunnel
  python3 check_bnk.py --cluster tf-cluster-jp-hk --ping-vxlan

  # Check VPC routing tables, routes, and cross-verify next-hop against TMM worker node IPs
  python3 check_bnk.py --cluster tf-cluster-jp-hk --check-routes

  # Run all diagnostics with verbose details (all individual pings and route matching details)
  python3 check_bnk.py --cluster tf-cluster-jp-hk --all -v

  # Check specific VXLAN port (e.g. 6789)
  python3 check_bnk.py --cluster tf-cluster-jp-hk --vxlan-port 6789

  # Pass IBM Cloud API Key directly
  python3 check_bnk.py --cluster tf-cluster-jp-hk --api-key <YOUR_KEY>
""",
    )
    parser.add_argument(
        "-c",
        "--cluster",
        help="Cluster name or ID to perform BNK checks on. If omitted, lists all clusters.",
    )
    parser.add_argument(
        "-p",
        "--vxlan-port",
        type=int,
        help="Custom VXLAN port to verify in Security Group (e.g. 6789, 4789). Defaults to port detected from Infra CR.",
    )
    parser.add_argument(
        "--check-routes",
        "--check-route",
        dest="check_routes",
        action="store_true",
        help="Fetch VPC routing tables (verify exactly 2), list routes, and verify Infra/Gateway/EgressGateway IPAM route coverage & next-hop.",
    )
    parser.add_argument(
        "--ping-vxlan",
        action="store_true",
        help="Send ICMP pings from all worker nodes to all TMM pods over dataplane tunnels concurrently.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all diagnostic checks including security groups, multi-node VXLAN ping, and VPC route table next-hop verification.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show verbose output, including all individual node-to-TMM ping pairs, full route attributes, and next-hop verification details.",
    )
    parser.add_argument(
        "--api-key",
        help="IBM Cloud API Key. If omitted, uses IBMCLOUD_API_KEY env var or existing 'ibmcloud' session.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in terminal output.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON summary instead of human-readable text.",
    )
    parser.add_argument(
        "--check-prereqs",
        action="store_true",
        help="Run comprehensive check on all required tools, plugins, and credentials.",
    )

    args = parser.parse_args()
    use_color = not args.no_color and sys.stdout.isatty()

    try:
        # Explicit prerequisite check mode
        if args.check_prereqs:
            passed = check_prerequisites(
                cluster_mode=bool(args.cluster),
                api_key=args.api_key,
                use_color=use_color,
                quiet_if_pass=False,
            )
            sys.exit(0 if passed else 1)

        # Automatic pre-flight check: runs quietly if all prerequisites pass.
        # If any required component is missing, prints detailed remediation and exits with code 1.
        if not check_prerequisites(
            cluster_mode=bool(args.cluster),
            api_key=args.api_key,
            use_color=use_color,
            quiet_if_pass=True,
        ):
            sys.exit(1)

        if args.api_key:
            ensure_ibmcloud_login(args.api_key)

        if not args.cluster:
            clusters = list_clusters()
            if args.json:
                print(json.dumps(clusters, indent=2))
            else:
                display_clusters(clusters, use_color=use_color)
            sys.exit(0)
        else:
            check_routes = args.check_routes or args.all
            ping_vxlan = args.ping_vxlan or args.all
            exit_code = run_diagnostics(
                args.cluster,
                custom_port=args.vxlan_port,
                check_routes=check_routes,
                ping_vxlan=ping_vxlan,
                verbose=args.verbose,
                use_color=use_color,
            )
            sys.exit(exit_code)

    except Exception as e:
        print(f"\n{colorize('ERROR:', Colors.RED, use_color)} {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

