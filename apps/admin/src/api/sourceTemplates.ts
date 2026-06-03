/*
 * sourceTemplates — type-specific example config JSON for each SourceType.
 *
 * Used in the Add-source form (and optionally the Edit form) to pre-fill the
 * Config textarea when the user selects a source type.
 *
 * IMPORTANT: example configs must NOT contain secrets. Credentials, tokens,
 * CA certificates, and AWS keys belong in secrets_ref (env:/file:/k8s:).
 */

import { SourceType } from "./client";

export interface SourceTemplate {
  /** Pretty-printed JSON string to pre-fill the Config textarea. */
  configJson: string;
  /** Short hint shown below the textarea. */
  hint: string;
}

const TEMPLATES: Record<SourceType, SourceTemplate> = {
  k8s: {
    configJson: JSON.stringify(
      {
        api_server: "https://<eks-endpoint>.eks.amazonaws.com",
        cluster_name: "qbiq-shared",
        namespace: "",
        use_llm_kind_selection: false,
        default_include_kinds: [
          "Namespace",
          "Deployment",
          "Service",
          "Ingress",
          "argoproj.io/Application",
        ],
      },
      null,
      2
    ),
    hint: "Credentials (kubeconfig / service account token / CA cert) go in secrets_ref, not here.",
  },
  git: {
    configJson: JSON.stringify(
      {
        url: "https://github.com/org/repo.git",
        ref: "main",
        path_include: ["**/*.md"],
        path_exclude: [],
        max_file_size_bytes: 1000000,
      },
      null,
      2
    ),
    hint: "Git credentials (personal access token, deploy key) go in secrets_ref.",
  },
  aws: {
    configJson: JSON.stringify(
      {
        regions: ["us-east-1"],
        services: ["s3", "iam", "ec2"],
        resource_type_filters: [],
        include_organizations: false,
      },
      null,
      2
    ),
    hint: "AWS credentials (access key / role ARN) go in secrets_ref.",
  },
  s3: {
    configJson: JSON.stringify(
      {
        bucket: "my-bucket",
        prefix: "docs/",
        region: "us-east-1",
        path_include: [],
        path_exclude: [],
        max_file_size_bytes: 10000000,
      },
      null,
      2
    ),
    hint: "AWS credentials go in secrets_ref.",
  },
  fs: {
    configJson: JSON.stringify(
      {
        root_path: "/data/docs",
        path_include: ["**/*.md", "**/*.txt"],
        path_exclude: [],
        max_file_size_bytes: 1000000,
      },
      null,
      2
    ),
    hint: "No secrets required for filesystem sources.",
  },
  confluence: {
    configJson: JSON.stringify(
      {
        base_url: "https://myorg.atlassian.net",
        space_keys: [],
      },
      null,
      2
    ),
    hint: "Confluence API token goes in secrets_ref.",
  },
  notion: {
    configJson: JSON.stringify(
      {
        database_ids: [],
        page_ids: [],
      },
      null,
      2
    ),
    hint: "Notion integration secret goes in secrets_ref.",
  },
  slack: {
    configJson: JSON.stringify(
      {
        channel_ids: [],
        include_threads: true,
        lookback_days: 90,
      },
      null,
      2
    ),
    hint: "Slack bot token goes in secrets_ref.",
  },
  jira: {
    configJson: JSON.stringify(
      {
        base_url: "https://myorg.atlassian.net",
        project_keys: [],
        issue_types: [],
      },
      null,
      2
    ),
    hint: "Jira API token goes in secrets_ref.",
  },
  grafana: {
    configJson: JSON.stringify(
      {
        base_url: "https://grafana.example.com",
        include_dashboards: true,
        include_alerts: true,
      },
      null,
      2
    ),
    hint: "Grafana service account token goes in secrets_ref.",
  },
  terraform: {
    configJson: JSON.stringify(
      {
        workspace_name: "default",
        organization: "my-org",
      },
      null,
      2
    ),
    hint: "Terraform Cloud / Enterprise token goes in secrets_ref.",
  },
  alerts: {
    configJson: JSON.stringify({}, null, 2),
    hint: "No extra config required — configured via secrets_ref.",
  },
  otel: {
    configJson: JSON.stringify({}, null, 2),
    hint: "No extra config required — configured via secrets_ref.",
  },
  k8s_operator: {
    configJson: JSON.stringify(
      {
        cluster_name: "qbiq-shared",
        namespace: "",
        default_include_kinds: ["Namespace", "Deployment", "Service"],
      },
      null,
      2
    ),
    hint: "Operator credentials go in secrets_ref.",
  },
};

/** Return the template for a given SourceType (falls back to empty config). */
export function getSourceTemplate(type: SourceType): SourceTemplate {
  return (
    TEMPLATES[type] ?? {
      configJson: "{}",
      hint: "No extra config / configured via secrets_ref.",
    }
  );
}
