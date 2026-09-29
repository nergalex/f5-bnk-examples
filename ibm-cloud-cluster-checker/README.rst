===========================================================
BNK - IBM Cloud Cluster Checker & Diagnostics (check_bnk.py)
===========================================================

This page documents the BNK Cluster Checker & Diagnostics tool (check_bnk.py) for validating F5 BIG-IP Next for Kubernetes (BNK) deployments on IBM Cloud VPC clusters.

**Attachment: check_bnk.py is attached to this page for direct download.**

IBM Cloud & F5 BIG-IP Next for Kubernetes (BNK) Cluster Checker
================================================================

A lightweight, customer-ready diagnostic tool designed with zero third-party Python package dependencies.

.. contents::
   :depth: 2

1. Prerequisites & Required Components
======================================

To run this tool, the customer only needs:

1.1 Core Software
-----------------

.. list-table::
   :widths: 25 20 55
   :header-rows: 1

   * - Component
     - Required Version
     - Description
   * - Python
     - 3.8+
     - Uses only the Python standard library (json, subprocess, argparse, shutil, sys, os, ipaddress, concurrent.futures, re). No pip install or virtual environment required.
   * - ibmcloud CLI
     - Latest recommended
     - IBM Cloud Command Line Interface.
   * - ibmcloud Plugins
     - container-service (ks), vpc-infrastructure (is)
     - Run ``ibmcloud plugin install container-service`` to install.
   * - kubectl or oc
     - Compatible with cluster
     - Used to query Kubernetes Custom Resources (read-only).

1.2 Authentication & API Key
----------------------------

    - **IBM Cloud API Key (IBMCLOUD_API_KEY):**
        - Can be provided via environment variable: ``export IBMCLOUD_API_KEY="<your_api_key>"``
        - Or passed via CLI parameter: ``--api-key "<your_api_key>"``
        - If the customer has already performed ``ibmcloud login``, the script will automatically reuse the existing session.

    - **Required IAM Permissions in IBM Cloud:**
        - Kubernetes Service: Viewer (to list clusters and download read-only cluster config) or Operator.
        - VPC Infrastructure Service: Viewer or Operator on Security Groups and VPC Routing Tables.

2. Installation & Quick Start
=============================

The diagnostic tool is self-contained in a single Python script (check_bnk.py) requiring only the standard library.

To install and prepare the tool:

1. **Create check_bnk.py:**
   *   Download the attached ``check_bnk.py`` file from this Confluence page, or copy the Python script provided in the collapsible code block below and save it as ``check_bnk.py``.
2. **Make the script executable:**

   .. code-block:: bash

      chmod +x check_bnk.py

3. **Verify prerequisites & run:**

   .. code-block:: bash

      python3 check_bnk.py --check-prereqs

3. Usage Examples
=================

3.1 Verify Prerequisites & Dependencies
---------------------------------------

Run a pre-flight health check to verify that all necessary CLIs, plugins, and active credentials are in place:

.. code-block:: bash

   python3 check_bnk.py --check-prereqs

*Note: This check also runs automatically before any cluster query. If any tool or plugin is missing, the script displays the exact installation commands and halts gracefully.*

3.2 List All Clusters
---------------------

If no cluster is specified, the script queries IBM Cloud and displays all ROKS / IKS clusters across VPC and Classic infrastructure:

.. code-block:: bash

   python3 check_bnk.py

Sample output:

.. code-block:: text

   Found Clusters in IBM Cloud:
   =========================================================================================================
   NAME                             ID                     STATE      LOCATION       OPENSHIFT/KUBE WORKERS
   ---------------------------------------------------------------------------------------------------------
   staging-small-8c-20g             da01v52o0281mrr2h4bg   normal     Osaka          4.21.31      6
   staging-cluster-16C-32G          d87jjesf04qmg2fn57bg   warning    Frankfurt      4.19.45      3
   tf-cluster-jp-hk                 d821aqgf0a0cqqsubg40   warning    Frankfurt      4.19.45      6
   ...

3.3 Core BNK Diagnostics on a Specific Cluster
----------------------------------------------

When a cluster name or ID is provided, the script runs comprehensive BNK and network verification. By default, the script automatically retrieves the VXLAN port directly from the applied Infra CR:

.. code-block:: bash

   python3 check_bnk.py --cluster tf-cluster-jp-hk

Or using an explicit API key:

.. code-block:: bash

   python3 check_bnk.py --cluster tf-cluster-jp-hk --api-key <YOUR_IBMCLOUD_API_KEY>

3.4 Ping Dataplane Tunnels Across All Nodes to All TMMs (``--ping-vxlan``)
-------------------------------------------------------------------------

Sends concurrent ICMP pings from every worker node (via f5-spk-csrc pods on the host network in namespace f5-utils) to every running TMM pod:

*   **Overlay-First Strategy:** When VXLAN tunnels are active (bnkvxlan*), the tool tests the Overlay ping first using the tunnel device as the source (``-I <vxlan_dev>``) to the TMM overlay address.
*   **Skip Underlay on Overlay Pass:** If the overlay ping succeeds, the underlay test for that pair is skipped as the active dataplane tunnel is confirmed healthy.
*   **Underlay Fallback:** If an overlay ping fails, or if VXLAN tunnels are not configured on the worker nodes, the check tests underlay connectivity from the worker node physical IP (``-I <node_ip>``) to the TMM external VLAN address (the VTEP interface on net1).
*   **Security Group Check on Underlay Failure:** If an underlay ping check fails, the tool checks whether the cluster security group allows inbound ICMP. If inbound ICMP is not permitted, the result is flagged as a [WARN] (Warning) rather than a hard failure.
*   **Kernel Route Path Confirmation (ip route get):** When an underlay ping fails, the tool queries the worker node host kernel routing table (``ip route get <target_ip>``) from the node's CSRC pod, displaying the exact outgoing path (dev, via, and src) to assist with troubleshooting.

.. code-block:: bash

   python3 check_bnk.py --cluster tf-cluster-hk-new --ping-vxlan

Sample output:

.. code-block:: text

   5. Testing VXLAN / Dataplane Ping (All Nodes -> All TMMs)...
     • Worker Nodes (CSRC pods):  6
     • TMM Pods (namespace: f5-bnk): 6
     • VXLAN Tunnels:             Detected (bnkvxlan9000 - testing Overlay first, Underlay skipped on Overlay pass)
     • Total Ping Checks Tested:  36 (36 underlay check(s) skipped as overlay passed)

     [PASS] All 36 dataplane ping check(s) passed! (0% packet loss)
            (36 underlay check(s) skipped as overlay passed)
            RTT Stats: Avg: 3.20 ms | Min: 1.61 ms | Max: 6.12 ms

3.5 VPC Route Table, IPAM, and Next-Hop Verification (``--check-routes``)
------------------------------------------------------------------------

Inspects VPC routing tables, lists all routes, and cross-verifies address propagation across f5-ipam CRs (ipams.fic.f5.com) and both VPC routing tables, including verification that the route next-hop IP matches the worker node hosting that specific TMM pod:

.. code-block:: bash

   python3 check_bnk.py --cluster tf-cluster-jp-hk --check-routes

Checks performed:

*   Verifies that exactly 2 routing tables exist for the VPC (the default VPC table and the cluster table), and warns if there are more than 2.
*   Lists all routes under each table with destination CIDR, next-hop IP, and availability zone.
*   **Next-Hop Verification:** Verifies that each route's ``next_hop.address`` matches the ``InternalIP`` of the worker node hosting the TMM pod assigned to that address.
*   **Infra CR:** Reads VLAN networks, retrieves TMM pod IPs across pods concurrently, confirms assigned IPs match f5-ipam CR status, verifies subnet default gateway ``.1`` reservation in IPAM, and checks that covering routes exist in BOTH routing tables with the correct next-hop.
*   **Infra CR Static Routes:** Reads ``spec.staticRoutes``, confirms route configuration in TMM via ``configview show static_route``, validates active dataplane routes in ``bdt_cli route``, and verifies ARP resolution of route next-hop gateways via ``bdt_cli arp``.
*   **Gateway CRs:** Reads Gateway VIP addresses, confirms VIPs match f5-ipam CR status, and checks that covering routes exist in BOTH routing tables with the correct next-hop.
*   **EgressGateway CRs & SNAT Settings:** Resolves referenced GatewaySettings and inspects the SNAT mode for each EgressGateway:
    *   ``UseIngressAddress``: Notes that egress uses the Gateway VIP in the same namespace and skips redundant route check (verified in Step 6.3).
    *   ``Automap``: Notes that egress uses the TMM VLAN address and skips redundant route check (verified in Step 6.1).
    *   ``Pool``: Follows ``sourceNATPoolRef`` to the referenced IPAM pool and verifies covering routes and next-hops in BOTH VPC routing tables.

3.6 Verbose Diagnostics Output (``-v`` / ``--verbose``)
-------------------------------------------------------

Add ``-v`` or ``--verbose`` to view in-depth details for every check:

*   Individual ping results for every worker node → TMM pair with clear type badges:
    *   ``[und/local]`` / ``[und/cross]``: Underlay ping to TMM external VLAN address (co-located vs cross-node).
    *   ``[ovr/local]`` / ``[ovr/cross]``: Overlay ping to TMM VXLAN overlay address.
*   Clear ``--`` node separation breaks in verbose ping output.
*   Full route attributes under each routing table (destination CIDR, next-hop IP, zone, route name, route ID).
*   Discovered TMM pod topology and worker node internal IP mapping.
*   Detailed next-hop match verdicts on each route entry in both routing tables.
*   Dataplane static routes and gateway ARP details from TMM containers.

.. code-block:: bash

   python3 check_bnk.py --cluster tf-cluster-hk-new --all -v

Sample verbose output:

.. code-block:: text

   5. Testing VXLAN / Dataplane Ping (All Nodes -> All TMMs)...
     • Worker Nodes (CSRC pods):  6
     • TMM Pods (namespace: f5-bnk): 6
     • VXLAN Tunnels:             Detected (bnkvxlan9000 - testing Overlay & Underlay)
     • Total Ping Checks Tested:  72

     Individual Node -> TMM Ping Results:
       [PASS] ...-00006bbe -> ...-426xp (10.155.16.64   ) [und/cross] RTT: 3.52 ms
       [PASS] ...-00006bbe -> ...-7sfqt (10.155.17.64   ) [und/cross] RTT: 2.87 ms
       [PASS] ...-00006bbe -> ...-fsl9h (10.155.18.2    ) [und/cross] RTT: 3.50 ms
       ...

3.7 Run All Checks (``--all``)
------------------------------

Runs core diagnostics, all-node VXLAN ping, and VPC route table next-hop verification together:

.. code-block:: bash

   python3 check_bnk.py --cluster tf-cluster-jp-hk --all

3.8 Custom VXLAN Port Override (``--vxlan-port``)
--------------------------------------------------

Test whether a specific custom VXLAN port is permitted through the security group (overriding what is defined in the Infra CR):

.. code-block:: bash

   python3 check_bnk.py --cluster tf-cluster-jp-hk --vxlan-port 6789

3.9 Security Group Remediation Recommendations
----------------------------------------------

If any required inbound VXLAN port is missing or blocked in the security group, the tool highlights the failure with an explicit [RECOMMENDATION] and copy-pasteable remediation command:

.. code-block:: text

   [FAIL] Inbound UDP Port 6789 is NOT OPEN in security group 'kube-d821aqgf0a0cqqsubg40'.
          VXLAN encapsulation traffic across cluster nodes/underlay will be blocked!
   [RECOMMENDATION] Add inbound security group rule to allow port 6789/udp:
          ibmcloud is security-group-rule-add r010-5c732def-171a-4663-9d33-f516dafc9fcb inbound udp --port-min 6789 --port-max 6789 --remote 0.0.0.0/0
          (Tip: You can replace 0.0.0.0/0 with the cluster VPC or worker node subnet CIDR if preferred)

4. Architecture & Workflow
==========================

.. code-block:: text

   flowchart TD
       A["Start: check_bnk.py"] --> B{"Cluster provided?"}
       B -- No --> C["Query IBM Cloud KS API<br/>List all clusters in table"]
       B -- Yes --> D["Step 1: Retrieve Cluster & VPC Metadata<br/>Region, VPC ID, Status"]
       D --> E["Step 2: Verify K8s / OpenShift Connectivity<br/>Sync kubeconfig targeting cluster region"]
       E --> F["Step 3: Inspect BNK Infra CRD & CR<br/>Verify Accepted / ResolvedRefs / Programmed"]
       F --> G["Step 4: Resolve VXLAN Ports<br/>From Infra CR egressDefaults/networks or CLI arg"]
       G --> H["Step 4: Inspect VPC Security Group<br/>Check inbound UDP rules for port match<br/>Provide exact remediation command if closed"]
       H --> I{"--ping-vxlan or --all?"}
       I -- Yes --> J["Step 5: Dataplane Ping (Overlay First & Underlay Fallback)<br/>Overlay first via VXLAN tunnels; skip underlay on pass<br/>Fallback underlay with SG ICMP check and route path confirmation<br/>Report RTT statistics and packet loss"]
       I -- No --> K{"--check-routes or --all?"}
       J --> K
       K -- Yes --> L["Step 6: VPC Route Table & IPAM Next-Hop Verification<br/>- Verify exactly 2 route tables (warn if > 2)<br/>- Discover TMM pod to worker node mapping<br/>- 6.1 Confirm TMM VLAN addresses match f5-ipam & exist in BOTH tables<br/>- 6.2 Verify Infra CR staticRoutes in TMM configview, bdt_cli route, bdt_cli arp<br/>- 6.3 Confirm Gateway VIPs match f5-ipam & exist in BOTH tables<br/>- 6.4 Confirm EgressGateway / SNAT subnets exist in BOTH tables<br/>- Verify route next_hop matches worker node hosting TMM"]
       K -- No --> M["Step 7: Final Summary & Health Verdict"]
       L --> M

Test Execution Output: tf-cluster-hk-new
========================================

.. admonition:: Captured from command: ``python3 check_bnk.py --cluster tf-cluster-hk-new --all -v``

   .. container:: toggle

      .. container:: header

         **Show/Hide Diagnostics Test Output (tf-cluster-hk-new)**

      .. code-block:: text

         ================================================================================
         BNK DIAGNOSTICS FOR CLUSTER: tf-cluster-hk-new
         ================================================================================

         1. Fetching IBM Cloud Cluster Information...
           • Cluster Name:    tf-cluster-hk-new
           • Cluster ID:      d6tdhn7t07bvu2fdj5i0
           ...

         (The rest of the lengthy output is omitted for brevity but would be included here)

         ================================================================================
         Total Execution Time: 82.43 seconds
         OVERALL STATUS: HEALTHY
           All BNK configurations, next-hop routes, and network connectivity verified.
         ================================================================================

