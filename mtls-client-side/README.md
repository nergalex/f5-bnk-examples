# Client mTLS on a BNK Gateway

This guide makes `https://www.example.com` accept only clients that present a certificate signed by your CA. Only the Gateway changes: the HTTPRoute and the application stay as they are.

The example uses the Gateway `gateway` in the namespace `f5-bnk-instance`. Replace them with your own.

## Requirements

- Gateway API 1.5 CRDs or later (the `spec.tls.frontend` field). Check with:

  ```bash
  kubectl get crd gateways.gateway.networking.k8s.io \
    -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version}'
  ```

- A Gateway with an HTTPS listener (`tls.mode: Terminate`).
- These files:

| File | Content |
| --- | --- |
| `ca-chain.crt` | Root CA and intermediate CA, both in one PEM file |
| `client.crt` | Client certificate, signed by the intermediate CA |
| `client.key` | Client private key |

Check that the client certificate validates against the CA:

```bash
openssl verify -CAfile ca-chain.crt client.crt
```

## 1. Store the CA in Kubernetes

The Gateway reads the trusted CA from a **ConfigMap** (not a Secret) with the key `ca.crt`. Create it in the Gateway's namespace:

```bash
kubectl create configmap ca-chain -n f5-bnk-instance --from-file=ca.crt=ca-chain.crt
```

`--from-file=ca.crt=ca-chain.crt` stores the file under the key `ca.crt`, the key name Gateway API expects. The file name itself doesn't matter.

Only `ca-chain.crt` goes into the cluster. `client.key` stays with the client.

## 2. Configure the Gateway

Add `spec.tls.frontend` to the Gateway, at the same level as `listeners`:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: gateway
  namespace: f5-bnk-instance
spec:
  # ... gatewayClassName, infrastructure, addresses: unchanged
  tls:
    frontend:
      default:
        validation:
          mode: AllowValidOnly      # reject clients without a valid certificate
          caCertificateRefs:
            - group: ""
              kind: ConfigMap
              name: ca-chain
  listeners:
    # ... unchanged
```

`default` applies to every HTTPS listener of the Gateway. Plain HTTP listeners are not affected.

Apply it and check that the Gateway is still programmed:

```bash
kubectl apply -f gateways.yaml
kubectl get gateway gateway -n f5-bnk-instance \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}'
```

You should see `Accepted=True Programmed=True ResolvedRefs=True`. If a listener reports `NoValidCACertificate`, the ConfigMap is missing or is in another namespace.

## 3. Test with curl

Run it from a host that can reach the Gateway VIP:

```bash
# Without a certificate: the TLS handshake fails (curl error 35 or 56)
curl -k https://www.example.com/

# With the client certificate: HTTP 200
curl -k --cert client.crt --key client.key https://www.example.com/
```

## Notes

- If the CA expires, mTLS stops working even when `client.crt` is still valid. Check the dates of the client certificate and of every certificate in `ca-chain.crt` (`openssl x509` would only read the first one):

  ```bash
  openssl x509 -in client.crt -noout -enddate
  openssl crl2pkcs7 -nocrl -certfile ca-chain.crt | openssl pkcs7 -print_certs -text -noout \
    | grep -E 'Subject:|Not After'
  ```
- To rotate the CA, replace the ConfigMap. The Gateway does not need to change:

  ```bash
  kubectl create configmap ca-chain -n f5-bnk-instance --from-file=ca.crt=ca-chain.crt \
    --dry-run=client -o yaml | kubectl apply -f -
  ```
