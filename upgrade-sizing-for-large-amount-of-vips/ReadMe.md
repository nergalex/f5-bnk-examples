# How to upgrade sizing for a large amount of VIPs

## 1. SYMPTOMS - F5 BNK Controller OOM & Gateway Programming Latency at Scale

When F5 BIG-IP Next for Kubernetes (BNK) is deployed in a high-density multi-tenant workloads (500 application namespaces, 500 Ingress Gateways, 500 Egress Gateways, 5,000 ClusterIP Services, and 5,000 HTTPRoutes), two symptoms could be observed:
1. **The controller restarted.** `f5-cne-controller` ran out of memory and was killed (`OOMKilled`, exit code 137)
2. **Gateway Status Programming Delay.** Gateways stayed in `Programmed: Unknown` or `AddressNotAssigned`, and did not pass traffic until they were programmed.

---

## 2. CAUSE

### 2.1 Controller runs out of memory

- **Small default memory limit.** The default `Tiny` sizing profile limits the controller to **1 GiB**.
- **The controller caches a huge amount of Secrets and Services.**  When the controller is configured to watch all namespaces `spec.watchNamespaces: ["All"])`, the client-go informers ingest and retain thousands of Secret and Service objects into the controller's in-memory cache.
- **Internal certificates renew at the same moment.** F5 BNK utilizes internal mTLS certificates across its microservices. These certificates are valid for **1 hour** and all internal certificates schedule renewal at the 40-minute mark. Therefore, all certificates renew at the exact same second, generating secret-related informer events (Certificate, Secret, and ConfigMap updates) in a single synchronized wave.

The simultaneous arrival of secret updates,
alongside 5,000 Service watch events,
causes rapid heap allocation as the controller deserializes objects and evaluates Gateway bindings.
Under high scale storm conditions, memory consumption climbs to the full memory consumption.
Any burst of cluster activity (such as bulk namespace deletion or workload redeployment) tips the memory usage over the allocated one,
triggering an immediate Linux kernel cgroup OOM termination (Exit Code 137).

Therefore, this context pushes memory close to the 1 GiB limit with the default `Tiny` sizing profile.

### 2.2 Gateway Status Update Latency (Dataplane Blackout Window)

The Out-of-Memory (OOM) described above could create a blackout as the controller underwent consecutive OOMKilled restarts 
(~once every 85 minutes, coinciding with two 40-minute renewal cycles),
repeatedly wiping the workqueue before Gateways could be programmed.


- **One queue, one worker.** The controller, ``f5-cne-controller``, processes every change (Gateways, routes, Services, Secrets, policies) one at a time, a FIFO (First-In, First-Out) workqueue.
- **New Gateway and Queue Contention** While the underlying F5 IPAM Controller (FIC) allocates IP addresses rapidly in memory (<1 ms per Gateway), the Gateway reconciliation loop must wait in line behind preceding route, service, and secret events before updating the Gateway's status.
- **Blackout Window.** Newly created Gateways remain in status.conditions ``[Programmed=Unknown]`` (Reason: ``Pending``, Message: ``Waiting for controller``) or ``AddressNotAssigned`` until the queue drains.
- **Crashes make it worse.** If the controller crashes due to an OOM event during synchronization, in-flight work in the FIFO queue is lost. The newly restarted pod must re-populate its informer caches from the API server and re-queue all cluster resources from scratch, compounding the status delay into hours.

---

## 3. Workaround

### 3.1 Raise the controller memory limit

Raise the memory about four times the observed peak.
For example of a ``Tiny`` deployment: **limit** to **4 GiB** in the `CNEInstance` for an observed peak of ~1 GiB.

Keep the resource **requests** unchanged, to ensure that Kubernetes pod scheduling is unaffected 
and avoids requiring extra reserved node capacity.

This change will give the controller container ample headroom to absorb informer cache indexing, 
40-minute certificate renewal bursts, 
and bulk operations.

```yaml
apiVersion: k8s.f5.com/v1
kind: CNEInstance
metadata:
  name: f5-bnk-f5-cne-controller
  namespace: f5-bnk
spec:
  advanced:
    cneController:
      resources:
        limits:
          memory: "4Gi"
          cpu: "2000m"
        requests:
          memory: "768Mi" # Kept as-is from default Tiny profile
          cpu: "300m"     # Kept as-is from default Tiny profile
```

Modify `metadata.name` and `metadata.namespace` to match your `CNEInstance`.


### 3.2 Spread the certificate renewals

Run this script after the installation of BNK. It spreads the renewals across the 40-minute window, so that certificates renew one at a time instead of all together.

```bash

    #!/bin/bash
    
    # Stagger internal certificate renewal deadlines across the 40-minute window (Pure kubectl)
    for ns in f5-bnk f5-utils; do
      # Initialize staggered time with the current UTC time
      staggered_time=$(date -u +%Y-%m-%dT%H:%M:%SZ)
      
      for cert in $(kubectl get certificate -n "$ns" -o jsonpath='{.items[*].metadata.name}'); do
        echo "Staggering renewal deadline for: $ns / $cert"
        
        # Annotate the certificate with the current staggered time
        kubectl annotate certificate "$cert" -n "$ns" cert-manager.io/renew-at="$staggered_time" --overwrite
        
        # Increment staggered time by 35 seconds for the next certificate
        staggered_time=$(date -u -d "$staggered_time + 35 seconds" +%Y-%m-%dT%H:%M:%SZ)
      done
    done

```

Replace `f5-bnk` and `f5-utils` with the namespaces that hold your BNK certificates.

Check the result: the renewal times should be spaced apart, not identical.

```bash
kubectl get certificate -A --sort-by=.status.renewalTime \
  -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,RENEWAL:.status.renewalTime
```

### DO NOT modify watchNamespaces

> **Keep `watchNamespaces: ["All"]`.**
> Do not configure spec.watchNamespaces with a large list of individual application namespaces

Because, specifying a list of hundreds of namespaces causes the controller's WatcherManager to spawn separate per-namespace informers (10 watchers per namespace = 5,000+ separate HTTP list/watch connections), triggering client-side Query Per Second rate-limiting throttling and worsening queue latency.

---

## 4. Permanent fix

F5 is evaluating these product improvements:

- **Lower memory use** and faster processing of the controller queue.
- **Ignore irrelevant Secrets**, such as service account tokens, so they no longer use memory or create events.
- **Configurable certificate lifetime with random jitter**, so that renewals no longer happen at the same time.

