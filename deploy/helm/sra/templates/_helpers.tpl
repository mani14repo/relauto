{{- define "sra.labels" -}}
app.kubernetes.io/name: sra
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
{{- end -}}

{{- define "sra.image" -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end -}}

{{- define "sra.env" -}}
{{- if .Values.cloudSql.useCapInjected }}
{{- /* CAP injects CAP_POSTGRES_<APP>_<DB>_JDBC_URI and CAP_POSTGRES_USER (as for COG); the app reads them itself. */}}
{{- if .Values.cloudSql.capJdbcVar }}
- name: SRA_CAP_POSTGRES_VAR
  value: {{ .Values.cloudSql.capJdbcVar | quote }}
{{- end }}
{{- else }}
- name: DATABASE_URL
  value: "postgresql://{{ .Values.cloudSql.user | urlquery }}@127.0.0.1:5432/{{ .Values.cloudSql.database }}?sslmode=disable"
{{- end }}
- name: RETRIEVAL_RERANK
  value: {{ .Values.retrieval.rerank | default "off" | quote }}
{{- if .Values.servicenow.instanceUrl }}
- name: SERVICENOW_INSTANCE_URL
  value: {{ .Values.servicenow.instanceUrl | quote }}
{{- if .Values.servicenow.tokenKey }}
- name: SERVICENOW_TOKEN
  valueFrom: {secretKeyRef: {name: {{ required "servicenow.existingSecret is required when servicenow.instanceUrl is set" .Values.servicenow.existingSecret }}, key: {{ .Values.servicenow.tokenKey }}}}
{{- else }}
- name: SERVICENOW_USER
  valueFrom: {secretKeyRef: {name: {{ required "servicenow.existingSecret is required when servicenow.instanceUrl is set" .Values.servicenow.existingSecret }}, key: {{ .Values.servicenow.userKey | default "username" }}}}
- name: SERVICENOW_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ .Values.servicenow.existingSecret }}, key: {{ .Values.servicenow.passwordKey | default "password" }}}}
{{- end }}
{{- end }}
{{- if .Values.kafka.enabled }}
- name: KAFKA_BOOTSTRAP_SERVERS
  value: {{ .Values.kafka.bootstrap | quote }}
- name: KAFKA_GROUP
  value: {{ .Values.kafka.group | quote }}
{{- if .Values.kafka.securityProtocol }}
- name: KAFKA_SECURITY_PROTOCOL
  value: {{ .Values.kafka.securityProtocol | quote }}
- name: KAFKA_SASL_MECHANISM
  value: {{ .Values.kafka.saslMechanism | quote }}
- name: KAFKA_USERNAME
  valueFrom: {secretKeyRef: {name: {{ .Values.kafka.existingSecret }}, key: username}}
- name: KAFKA_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ .Values.kafka.existingSecret }}, key: password}}
{{- end }}
{{- end }}
{{- with .Values.gcs.bucket }}
- name: SRA_GCS_BUCKET
  value: {{ . | quote }}
{{- end }}
- name: SRA_INGEST_HOURS
  value: {{ .Values.ingestion.hours | default "" | quote }}
- name: SRA_INGEST_STANDARDS
  value: {{ ternary "true" "false" (ne .Values.ingestion.standards false) | quote }}
{{- with .Values.github }}
{{- if .host }}
- name: GH_HOST
  value: {{ .host | quote }}
{{- end }}
{{- if .hosts }}
- name: SRA_GITHUB_HOSTS
  value: {{ .hosts | quote }}
{{- end }}
{{- if .existingSecret }}
{{- /* gh reads the token from its environment; the app code never reads or logs it. GH_ENTERPRISE_TOKEN serves every host but github.com. */}}
- name: {{ if or (not .host) (eq .host "github.com") }}GH_TOKEN{{ else }}GH_ENTERPRISE_TOKEN{{ end }}
  valueFrom: {secretKeyRef: {name: {{ .existingSecret }}, key: {{ .tokenKey | default "token" }}}}
{{- end }}
{{- end }}
- name: LLM_ENABLED
  value: {{ ternary "true" "false" (ne .Values.llm.enabled false) | quote }}
- name: LLM_PROVIDER
  value: {{ .Values.llm.provider | quote }}
- name: LLM_TIMEOUT
  value: {{ .Values.llm.timeoutSeconds | quote }}
{{- if or (eq .Values.llm.provider "anthropic") (eq .Values.llm.provider "lms_gateway") }}
- name: LLM_MODEL
  value: {{ .Values.llm.gateway.model | default "claude-opus-5-5" | quote }}
{{- /* Claude through the LMS gateway, as COG does: base URL here, key from the approved secret store at runtime. */}}
- name: ANTHROPIC_BASE_URL
  value: {{ required "llm.gateway.baseUrl is required when llm.provider is anthropic" .Values.llm.gateway.baseUrl | quote }}
- name: ANTHROPIC_API_KEY
  valueFrom: {secretKeyRef: {name: {{ required "llm.gateway.existingSecret is required when llm.provider is anthropic" .Values.llm.gateway.existingSecret }}, key: {{ .Values.llm.gateway.secretKey | default "ANTHROPIC_API_KEY" }}}}
- name: LLM_CONCURRENCY
  value: {{ .Values.llm.gateway.concurrency | default 4 | quote }}
- name: LLM_EFFORT
  value: {{ .Values.llm.gateway.effort | quote }}   {{- /* "" is meaningful: do not send the field */}}
- name: LLM_OUTPUT_MODE
  value: {{ .Values.llm.gateway.outputMode | default "tool" | quote }}
{{- else }}
- name: LLM_MODEL
  value: {{ .Values.llm.model | quote }}
- name: LLM_BASE_URL
  {{- /* one URL per Ollama replica (stable pod DNS via the headless service) so criteria spread across replicas */}}
  {{- $urls := list }}
  {{- range $i := until (int .Values.ollama.replicas) }}
  {{- $urls = append $urls (printf "http://%s-ollama-%d.%s-ollama-pods:11434" $.Release.Name $i $.Release.Name) }}
  {{- end }}
  value: {{ join "," $urls | quote }}
- name: LLM_CONCURRENCY
  value: {{ .Values.ollama.numParallel | default 1 | quote }}
- name: LLM_KEEP_ALIVE
  value: "24h"
- name: LLM_NUM_PREDICT
  value: {{ .Values.llm.numPredict | default 768 | quote }}
- name: LLM_THINK
  value: {{ .Values.llm.think | default false | quote }}
- name: LLM_NUM_CTX
  value: {{ .Values.llm.numCtx | quote }}
{{- end }}
- name: HF_HUB_OFFLINE
  value: "1"
{{- end -}}

{{- define "sra.proxy" -}}
- name: cloud-sql-proxy
  image: {{ .Values.cloudSql.proxyImage }}
  args: ["--auto-iam-authn", "--private-ip", "--port=5432", "--structured-logs", {{ .Values.cloudSql.instance | quote }}]
  restartPolicy: Always   # native sidecar: starts before and stops after the app container
  securityContext: {runAsNonRoot: true, allowPrivilegeEscalation: false, readOnlyRootFilesystem: true}
  resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: 500m, memory: 256Mi}}
{{- end -}}
