==========================
Single Certificate for BNK
==========================

Purpose
-------
Allows a single Kubernetes Secret containing certificate files to be used for all mTLS communication between BNK components installed by the F5 Lifecycle Operator (FLO).

Pre-requisites
--------------
This installation expects the existence of a single certificate contained in a Kubernetes Secret. The contents of this Secret must be the following files, in standard PEM formatting:

*   tls.crt - The certificate file
*   tls.key - The private key of the certificate
*   ca.crt - The signing certificate

The following generation instructions include a command to create the Secret containing these files.

Generating the Certificate
--------------------------

Create a Self-signed CA Certificate
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
This step is optional if a CA certificate is already available for signing. It is recommended that this be a CA certificate managed by some other system and contains different distinguished name fields to ensure the leaf certificate is not treated as being self-signed.

Generate the CA's key:

.. code-block:: shell

    $ openssl genrsa -out ca.key 4096

Create the configuration file for the CA:

.. code-block:: shell

    $ cat extfile.cnf
    [ req ]
    distinguished_name = req_distinguished_name
    x509_extensions = v3_ca
    prompt = no

    [ req_distinguished_name ]
    C = US
    ST = Washington
    L = Seattle
    O = F5 Networks
    OU = PD
    CN = f5net-ca

    [ v3_ca ]
    subjectKeyIdentifier = hash
    authorityKeyIdentifier = keyid:always,issuer
    basicConstraints = critical,CA:true
    keyUsage = critical, digitalSignature, cRLSign, keyCertSign

Self-sign the CA certificate:

.. code-block:: shell

    $ openssl req -new -x509 -days 3650 -key ca.key -out ca.crt -config extfile.cnf

Create and Sign the Single Certificate
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Create the client key:

.. code-block:: shell

    $ openssl genrsa -out tls.key 4096

The following configuration file can be used to generate this certificate as it is required to have specific Common Name and Subject Alternative Name fields set for usage by all of the components. Note the fields containing the <namespace> placeholder. This should be replaced by the namespace(s) where the component is expected to be installed.

.. code-block:: shell

    $ cat certconf.cnf
    [ req ]
    distinguished_name = req_distinguished_name
    req_extensions = v3_req
    prompt = no

    [ req_distinguished_name ]
    C = US
    ST = Washington
    L = Seattle
    O = F5 Networks
    OU = PD
    CN = f5net

    [ v3_req ]
    subjectAltName = @alt_names

    [ alt_names ]
    DNS.1 = *.f5-observer.<namespace>.svc.cluster.local
    DNS.2 = dssm-f5-dssm.<namespace>
    DNS.3 = dssm-svc
    DNS.4 = f5-access-renderer
    DNS.5 = f5-afm.<namespace>
    DNS.6 = f5-analyzer-grpc-svc
    DNS.7 = f5-analyzer.<namespace>
    DNS.8 = f5-bdosd
    DNS.9 = f5-bdosd.<namespace>
    DNS.10 = f5-csrc-grpc-svc
    DNS.11 = f5-downloader.<namespace>
    DNS.12 = f5-dssm-db.<namespace>
    DNS.13 = f5-dssm-sentinel.<namespace>
    DNS.14 = f5-dssm.<namespace>
    DNS.15 = f5-dwbld.<namespace>
    DNS.16 = f5-ipam-ctlr.<namespace>
    DNS.17 = f5-ipsd.<namespace>
    DNS.18 = f5-observer
    DNS.19 = f5-observer.<namespace>
    DNS.20 = f5-rabbit.<namespace>
    DNS.21 = f5-spk-csrc.<namespace>
    DNS.22 = f5-spk-cwc
    DNS.23 = f5-spk-cwc.<namespace>
    DNS.24 = f5-spk-cwc.<namespace>.svc
    DNS.25 = f5-tmm
    DNS.26 = f5-tmm.<namespace>
    DNS.27 = f5-toda-fluentd-external.<namespace>
    DNS.28 = f5-toda-fluentd.<namespace>
    DNS.29 = f5-validation-svc.<namespace>.svc
    DNS.30 = f5dr-svc-f5dr
    DNS.31 = f5net
    DNS.32 = grpc-apmd-svc
    DNS.33 = grpc-bdosd-svc
    DNS.34 = grpc-bdosd-svc.<namespace>
    DNS.35 = grpc-downloader-svc
    DNS.36 = grpc-dwbld-svc
    DNS.37 = grpc-ipsd-svc
    DNS.38 = grpc-pccd-svc
    DNS.39 = grpc-stream-ipsd-svc
    DNS.40 = grpc-svc
    DNS.41 = grpc-svc-f5dr
    DNS.42 = grpc-svc-f5dr.<namespace>
    DNS.43 = grpc-svc-f5dr.<namespace>.svc.cluster.local
    DNS.44 = grpc-svc.<namespace>
    DNS.45 = grpc-urlcat-svc
    DNS.46 = observer
    DNS.47 = otel-collector
    DNS.48 = otel-collector-svc
    DNS.49 = otel-collector-svc.<namespace>
    DNS.50 = otel-collector.<namespace>
    DNS.51 = rabbitmq-server.<namespace>
    DNS.55 = f5-cne-controller
    DNS.56 = f5-cne-controller.<namespace>
    DNS.57 = f5-cne-controller.<namespace>.svc
    DNS.58 = f5-coremond.<namespace>
    DNS.59 = f5-coremond.<namespace>.svc
    DNS.60 = f5-coremond.<namespace>.svc.cluster.local
    DNS.61 = f5-observer
    DNS.62 = f5-observer.<namespace>
    DNS.63 = f5-observer-operator
    DNS.64 = f5-observer-operator.<namespace>
    DNS.65 = f5-observer-receiver
    DNS.66 = f5-observer-receiver.<namespace>
    DNS.67 = *.f5-observer-operator.<namespace>.svc.cluster.local
    DNS.68 = *.f5-observer-receiver.<namespace>.svc.cluster.local
    DNS.69 = f5-ebc-grpc-svc
    DNS.70 = f5-ebc-grpc-svc.<namespace>
    DNS.71 = f5-ebc-grpc-svc.<namespace>.svc
    DNS.72 = f5-ext-bigip-controller.<namespace>
    DNS.73 = f5-ipsec-qkview
    DNS.74 = f5-ipsec-qkview.<namespace>
    DNS.75 = f5-ipsec-qkview.<namespace>.svc.cluster.local
    DNS.76 = f5-ipsec-qkview
    DNS.77 = f5-ipsec
    DNS.78 = f5-ipsec.<namespace>

Create the client cert signing request:

.. code-block:: shell

    $ openssl req -new -key tls.key -out tls.csr -config certconf.cnf

Sign the client certificate:

.. code-block:: shell

    $ openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -out tls.crt -days 3650 -sha256 -extfile certconf.cnf -extensions v3_req

Create the secret containing the cert information
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
.. code-block:: shell

    $ kubectl create secret generic single-secret --type=kubernetes.io/tls --from-file=tls.crt --from-file=tls.key --from-file=ca.crt

With this, the secret is installed in the system:

.. code-block:: shell

    $ kubectl get secret single-secret
    NAME            TYPE                DATA   AGE
    single-secret   kubernetes.io/tls   3      15m

Configuration
-------------

FLO Configuration
~~~~~~~~~~~~~~~~~
Ensure the FLO values file contains the following fields to use a single certificate and disable cert-manager usage:

.. code-block:: yaml

    $ cat flo-values.yaml
    global:
      certmgr:
        enabled: false
        secretName: single-secret

CNEInstance
~~~~~~~~~~~
The CNEInstance custom resource requires no special configuration. The setting in the FLO values file means that all components installed by applying a CNEInstance will utilize the same certificate Secret.

If the spec.certificate field has been set, that field can be removed entirely as it will be ignored when the FLO values contains global.certmgr.enabled: false.

Migrating from Cert-manager to Single Secret
--------------------------------------------
If your system already uses cert-manager to manage certificates used by the BNK components, the steps to convert to a single secret mode are:

1.  Create the single certificate secret using the instructions in the Generating the Certificate section.
2.  Update the values file used to install FLO with the certmgr configuration from the FLO Configuration section and perform a helm upgrade flo using the updated values file to enable the single secret mode.
3.  (Optional) Update the installed CNEInstance to remove the configured spec.certificate field. This is optional because FLO will ignore this field if it is set when certmgr.enabled is false.

You may see components restart due to the update to their individual volume mounts in order to utilize the new secret instead of the previous cert-manager configured secrets.
