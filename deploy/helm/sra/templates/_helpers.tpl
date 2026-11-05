{{- define "sra.labels" -}}
app.kubernetes.io/name: sra
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
{{- end -}}

{{- define "sra.image" -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end -}}

{{- define "sra.env" -}}
- name: DATABASE_URL
  value: "postgresql://{{ .Values.cloudSql.user | urlquery }}@127.0.0.1:5432/{{ .Values.cloudSql.database }}?sslmode=disable"
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
- name: SRA_GCS_BUCKET
  value: {{ .Values.gcs.bucket | quote }}
- name: LLM_PROVIDER
  value: {{ .Values.llm.provider | quote }}
- name: LLM_BASE_URL
  value: "http://{{ .Release.Name }}-ollama:11434"
- name: LLM_MODEL
  value: {{ .Values.llm.model | quote }}
- name: LLM_TIMEOUT
  value: {{ .Values.llm.timeoutSeconds | quote }}
- name: LLM_THINK
  value: {{ .Values.llm.think | default false | quote }}
- name: LLM_NUM_CTX
  value: {{ .Values.llm.numCtx | quote }}
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
