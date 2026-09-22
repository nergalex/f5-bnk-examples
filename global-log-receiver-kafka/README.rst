How to expose BNK logs to Kafka?
=================================

Notes
-----

* This guide covers configuring Fluentd to forward logs to an existing Kafka
  broker over mTLS, with optional record transformation. It assumes Kafka is
  already deployed and accessible.
* This is included exclusively in version ``>= 2.4``.
* Tested version: 2.4

1. Add Kafka mTLS certificates to the Fluentd custom secret
------------------------------------------------------------

``ca.crt``, ``client.crt``, and ``client.key`` are paths to your certificate
files. Update the command accordingly to point to the correct certificate
locations.

.. code-block:: bash

   kubectl patch secret f5-toda-fluentd-custom-secret -n f5-utils --type=merge \
     -p '{"data":{ \
       "kafka-ca.crt":"'$(base64 -w0 < ca.crt)'", \
       "kafka-client.crt":"'$(base64 -w0 < client.crt)'", \
       "kafka-client.key":"'$(base64 -w0 < client.key)'" \
     }}'

2. Save the Fluentd Kafka configuration
----------------------------------------

Save the following configuration as ``fluentd-kafka.yaml``.

.. note::

   Adjust the ``brokers``, ``default_topic``, and envelope fields in the
   ``<record>`` block to match your environment.

.. code-block:: yaml

   apiVersion: v1
   kind: ConfigMap
   metadata:
     name: f5-toda-fluentd-custom
     namespace: f5-utils
   data:
     kafka.conf: |
       # Route logs to the @KAFKA pipeline for transformation
       <store>
         @type relabel
         @label @KAFKA
       </store>
     kafka.pipeline: |
       # Transform and forward logs to Kafka
       <label @KAFKA>
         <filter **>
           @type record_transformer
           enable_ruby true
           renew_record true
           <record>
             @timestamp ${time.strftime('%Y-%m-%dT%H:%M:%S%z')}
             sourceIP 10.244.0.1
             hostname ${record['node'] || 'unknown'}
             providee_term spk-term
             bnpp ${{'collect' => {'code_name' => 'spk-cnf', 'version' => '1.0', 'topic_prefix' => 'f5'}, 'dataset' => 'iworkflow', 'auid' => 'ap12538', 'env' => 'dev', 'retention' => '30d'}}
             event.kind event
             data ${record}
           </record>
         </filter>
         <match **>
           @type kafka2
           brokers kafka.f5-utils.svc.cluster.local:9092
           default_topic f5-logs
           ssl_ca_cert /fluentd/etc/custom-certs/kafka-ca.crt
           ssl_client_cert /fluentd/etc/custom-certs/kafka-client.crt
           ssl_client_cert_key /fluentd/etc/custom-certs/kafka-client.key
           <format>
             @type json
           </format>
           <buffer>
             @type memory
             flush_interval 5s
           </buffer>
         </match>
       </label>

3. Apply the Fluentd ConfigMap
------------------------------

.. code-block:: bash

   kubectl apply -f fluentd-kafka.yaml

4. Restart Fluentd
------------------

Restart Fluentd to apply the Kafka configuration by deleting the existing
pod or pods.

.. code-block:: bash

   kubectl -n f5-utils get pods --no-headers -o custom-columns=":metadata.name" \
     | grep fluentd | xargs -r kubectl -n f5-utils delete pod

5. Verify logs in Kafka
-----------------------

Consume messages from the topic to confirm that logs are arriving with the
transformed envelope.

Example expected output:

.. code-block:: json

   {
     "@timestamp": "2026-06-21T12:23:03+0000",
     "sourceIP": "10.244.0.1",
     "hostname": "datkube-worker",
     "providee_term": "spk-term",
     "bnpp": {
       "collect": {
         "code_name": "spk-cnf",
         "version": "1.0",
         "topic_prefix": "f5"
       },
       "dataset": "iworkflow",
       "auid": "ap12538",
       "env": "dev",
       "retention": "30d"
     },
     "event.kind": "event",
     "data": {
       "service": "f5-tmm-routing",
       "type": "stdout",
       "pod_name": "f5-tmm",
       "log": "BGP : INFO 22.22.21.101-Outgoing [FSM] Routeadv Timer Expiry",
       "namespace": "default",
       "node": "datkube-worker",
       "pod": "f5-tmm-8zzxp",
       "pod_id": "35b6bed0-1cb0-44ea-85a9-c249f4500276"
     }
   }

