{{/*
Expand the name of the chart.
*/}}
{{- define "fyers-autotrader.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "fyers-autotrader.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Chart label (name + version).
*/}}
{{- define "fyers-autotrader.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Shared labels applied to every resource.
*/}}
{{- define "fyers-autotrader.labels" -}}
helm.sh/chart: {{ include "fyers-autotrader.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}

{{/*
Selector labels for a given service name.
Usage: include "fyers-autotrader.selectorLabels" (dict "name" "core-engine" "root" .)
*/}}
{{- define "fyers-autotrader.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end }}

{{/*
Full image reference for an application service.
Usage: include "fyers-autotrader.image" (dict "svc" $svcValues "root" .)
*/}}
{{- define "fyers-autotrader.image" -}}
{{- printf "%s/%s:%s" .root.Values.image.registry .svc.image .root.Values.image.tag }}
{{- end }}
