======================
Shared GatewaySettings
======================

Overview
========

This example shows business unit namespaces using Gateway and EgressGateway resources with one shared GatewaySettings resource. The Gateway, EgressGateway, and GatewaySettings resources are deployed in gateway-ns, which is separate from the application namespaces.

Namespace and CR Relationships
==============================

::

                                  gateway-ns
  +------------------------------------------------------------------------+
  | GatewaySettings: shared-gateway-settings                               |
  |   ingressConfig.ipamRefs: listener-addresses                           |
  |   egressConfigs: app-egress / UseIngressAddress                        |
  |                                                                        |
  | Gateway: bu-1-gateway              EgressGateway: bu-1-egress          |
  |   listener: http                       sourceSelector: bu-1            |
  |   hostname: bu-1.example.com                                           |
  |   allowedRoutes namespace: bu-1                                        |
  |                                                                        |
  | Gateway: bu-2-gateway              EgressGateway: bu-2-egress          |
  |   listener: bu-2-app-1-http           sourceSelector: bu-2             |
  |   hostname: app-1.bu-2.example.com                                     |
  |   allowedRoutes namespace: bu-2                                        |
  |   listener: bu-2-app-2-http                                            |
  |   hostname: app-2.bu-2.example.com                                     |
  |   allowedRoutes namespace: bu-2                                        |
  +--------------------------+----------------------------+----------------+
                             |                            |
                             | parentRefs                 | sourceSelector
                             |                            |
                bu-1         |                            | bu-2
  +--------------------------v------+      +--------------v-------------------+
  | HTTPRoute: bu-1-route           |      | HTTPRoute: bu-2-app-1-route      |
  | hostname: bu-1.example.com      |      | hostname: app-1.bu-2.example.com |
  | Service: bu-1-service           |      | Service: bu-2-app-1-service      |
  | Application pods                |      |                                  |
  +---------------------------------+      | HTTPRoute: bu-2-app-2-route      |
                                           | hostname: app-2.bu-2.example.com |
                                           | Service: bu-2-app-2-service      |
                                           | Application pods                 |
                                           +----------------------------------+

Gateways and EgressGateways reference GatewaySettings in gateway-ns.
HTTPRoutes attach across namespaces through parentRefs. Each Gateway listener
authorizes routes from its matching namespace using allowedRoutes.

Namespace Layout
----------------

+------------+--------------------------------------------------------------------------+---------------------------------------------------------+
| Namespace  | Resources                                                                | Purpose                                                 |
+============+==========================================================================+=========================================================+
| gateway-ns | GatewaySettings, Gateway, EgressGateway                                  | Shared gateway infrastructure and per-application       |
|            |                                                                          | gateway bindings                                        |
+------------+--------------------------------------------------------------------------+---------------------------------------------------------+
| bu-1       | HTTPRoute, application Service, application pods                         | Business unit 1 workload and route                      |
+------------+--------------------------------------------------------------------------+---------------------------------------------------------+
| bu-2       | Two HTTPRoute resources, application Service resources, application pods | Business unit 2 workloads and routes                    |
+------------+--------------------------------------------------------------------------+---------------------------------------------------------+

The business unit namespaces do not contain GatewaySettings, Gateway, or EgressGateway resources.

Shared GatewaySettings
======================

The GatewaySettings resource is created once in gateway-ns. Both Gateways reference it through spec.infrastructure.parametersRef. Both EgressGateways reference the app-egress entry through sectionName.
With UseIngressAddress, an EgressGateway uses a SNAT IP from the Gateway that serves the same application namespace.
The listener-addresses IPAM reference must correspond to configuration available through the singleton Infra resource.

GatewaySettings in gateway-ns:

.. code-block:: yaml

    apiVersion: gateway.k8s.f5.com/v1alpha1
    kind: GatewaySettings
    metadata:
      name: shared-gateway-settings
      namespace: gateway-ns
    spec:
      ingressConfig:
        defaultListenerNetwork:
          ipamRefs:
            - name: listener-addresses
      egressConfigs:
        - name: app-egress
          sourceNATConfig:
            type: UseIngressAddress

Business Unit 1 Gateway and EgressGateway
=========================================

The Gateway listener accepts routes only from bu-1 by namespace label. The EgressGateway selects source pods only from bu-1 by explicit namespace name.

Gateway and EgressGateway in gateway-ns:

.. code-block:: yaml

    apiVersion: gateway.networking.k8s.io/v1
    kind: Gateway
    metadata:
      name: bu-1-gateway
      namespace: gateway-ns
    spec:
      gatewayClassName: f5-gateway-class
      infrastructure:
        parametersRef:
          group: gateway.k8s.f5.com
          kind: GatewaySettings
          name: shared-gateway-settings
      listeners:
        - name: http
          protocol: HTTP
          port: 80
          hostname: bu-1.example.com
          allowedRoutes:
            namespaces:
              from: Selector
              selector:
                matchLabels:
                  kubernetes.io/metadata.name: bu-1
    ---
    apiVersion: gateway.k8s.f5.com/v1alpha1
    kind: EgressGateway
    metadata:
      name: bu-1-egress
      namespace: gateway-ns
    spec:
      gatewayClassName: f5-gateway-class
      infrastructure:
        parametersRef:
          name: shared-gateway-settings
          sectionName: app-egress
      sourceSelector:
        selectionMode: NamespaceSelector
        namespaces:
          matchNames:
            - bu-1

Business Unit 1 HTTPRoute
-------------------------

The HTTPRoute remains in bu-1. Its parentRefs.namespace points to the Gateway in gateway-ns. The Gateway listener's allowedRoutes selector authorizes the attachment.

HTTPRoute in bu-1:

.. code-block:: yaml

    apiVersion: gateway.networking.k8s.io/v1
    kind: HTTPRoute
    metadata:
      name: bu-1-route
      namespace: bu-1
    spec:
      parentRefs:
        - name: bu-1-gateway
          namespace: gateway-ns
          sectionName: http
      hostnames:
        - bu-1.example.com
      rules:
        - matches:
            - path:
                type: PathPrefix
                value: /
          backendRefs:
            - name: bu-1-service
              port: 8080

Business Unit 2 Gateway and EgressGateway
=========================================

The Gateway has two listeners for separate applications in bu-2. Both listeners allow routes from bu-2 by namespace label and share the same Gateway VIP. With UseIngressAddress, bu-2-egress uses that same VIP as its egress SNAT address. This provides one common VIP and SNAT address per business unit namespace. The EgressGateway selects source pods from bu-2 by explicit namespace name.

Gateway and EgressGateway in gateway-ns:

.. code-block:: yaml

    apiVersion: gateway.networking.k8s.io/v1
    kind: Gateway
    metadata:
      name: bu-2-gateway
      namespace: gateway-ns
    spec:
      gatewayClassName: f5-gateway-class
      infrastructure:
        parametersRef:
          group: gateway.k8s.f5.com
          kind: GatewaySettings
          name: shared-gateway-settings
      listeners:
        - name: bu-2-app-1-http
          protocol: HTTP
          port: 80
          hostname: app-1.bu-2.example.com
          allowedRoutes:
            namespaces:
              from: Selector
              selector:
                matchLabels:
                  kubernetes.io/metadata.name: bu-2
        - name: bu-2-app-2-http
          protocol: HTTP
          port: 80
          hostname: app-2.bu-2.example.com
          allowedRoutes:
            namespaces:
              from: Selector
              selector:
                matchLabels:
                  kubernetes.io/metadata.name: bu-2
    ---
    apiVersion: gateway.k8s.f5.com/v1alpha1
    kind: EgressGateway
    metadata:
      name: bu-2-egress
      namespace: gateway-ns
    spec:
      gatewayClassName: f5-gateway-class
      infrastructure:
        parametersRef:
          name: shared-gateway-settings
          sectionName: app-egress
      sourceSelector:
        selectionMode: NamespaceSelector
        namespaces:
          matchNames:
            - bu-2

Business Unit 2 Application 1 HTTPRoute
---------------------------------------

The HTTPRoute remains in bu-2. Its parentRefs.namespace points to the Gateway in gateway-ns. The Gateway listener's allowedRoutes selector authorizes the attachment.

HTTPRoute in bu-2:

.. code-block:: yaml

    apiVersion: gateway.networking.k8s.io/v1
    kind: HTTPRoute
    metadata:
      name: bu-2-app-1-route
      namespace: bu-2
    spec:
      parentRefs:
        - name: bu-2-gateway
          namespace: gateway-ns
          sectionName: bu-2-app-1-http
      hostnames:
        - app-1.bu-2.example.com
      rules:
        - matches:
            - path:
                type: PathPrefix
                value: /
          backendRefs:
            - name: bu-2-app-1-service
              port: 8080

Business Unit 2 Application 2 HTTPRoute
---------------------------------------

The HTTPRoute remains in bu-2 and attaches to the bu-2-app-2-http listener on bu-2-gateway.

HTTPRoute in bu-2:

.. code-block:: yaml

    apiVersion: gateway.networking.k8s.io/v1
    kind: HTTPRoute
    metadata:
      name: bu-2-app-2-route
      namespace: bu-2
    spec:
      parentRefs:
        - name: bu-2-gateway
          namespace: gateway-ns
          sectionName: bu-2-app-2-http
      hostnames:
        - app-2.bu-2.example.com
      rules:
        - matches:
            - path:
                type: PathPrefix
                value: /
          backendRefs:
            - name: bu-2-app-2-service
              port: 8080

Linking Rules
=============

.. note::

   When 'UseIngressAddress' is enabled, the controller determines the egress SNAT address by matching the EgressGateway's sourceSelector namespace with the Gateway whose listener permits allowedRoutes targeting the same namespace. This guarantees that each EgressGateway only uses the VIP of its associated Gateway, even with a shared GatewaySettings.

+----------------------+------------+---------------------------------------------------------------+
| Resource             | Namespace  | Link                                                          |
+======================+============+===============================================================+
| shared-gateway-settings | gateway-ns | Referenced by both Gateways and both EgressGateways           |
+----------------------+------------+---------------------------------------------------------------+
| bu-1-gateway         | gateway-ns | Accepts only routes from bu-1                                 |
+----------------------+------------+---------------------------------------------------------------+
| bu-1-egress          | gateway-ns | Selects only source pods in bu-1                              |
+----------------------+------------+---------------------------------------------------------------+
| bu-1-route           | bu-1       | Parent is gateway-ns/bu-1-gateway                             |
+----------------------+------------+---------------------------------------------------------------+
| bu-2-gateway         | gateway-ns | Has two dedicated listeners for applications in bu-2          |
+----------------------+------------+---------------------------------------------------------------+
| bu-2-egress          | gateway-ns | Selects only source pods in bu-2                              |
+----------------------+------------+---------------------------------------------------------------+
| bu-2-app-1-route     | bu-2       | Parent is gateway-ns/bu-2-gateway listener bu-2-app-1-http    |
+----------------------+------------+---------------------------------------------------------------+
| bu-2-app-2-route     | bu-2       | Parent is gateway-ns/bu-2-gateway listener bu-2-app-2-http    |
+----------------------+------------+---------------------------------------------------------------+

