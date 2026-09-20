{{/* SPDX-License-Identifier: MIT */}}
{{/* SPDX-FileCopyrightText: Copyright (c) 2026 Dell Technologies */}}
{{- define "kerberator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kerberator.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "kerberator.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "kerberator.labels" -}}
app.kubernetes.io/name: {{ include "kerberator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "kerberator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kerberator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "kerberator.imageRef" -}}
{{/* Prefer explicit image.tag; else use appVersion. Kerberator's
     container images publish with a leading "v" (e.g. v0.2.0), while
     Helm's AppVersion is a bare SemVer ("0.2.0"). Auto-prepend the v
     when falling back to AppVersion so the two conventions align. */}}
{{- $tag := .Values.image.tag -}}
{{- if not $tag -}}
{{- $tag = printf "v%s" .Chart.AppVersion -}}
{{- end -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
