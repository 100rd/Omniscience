"""Tests for TerraformParser, KubernetesParser, extract_infra_graph, and dispatch routing.

Covers all acceptance criteria for Issue #28 — Infrastructure dependency edges.
"""

from __future__ import annotations

from omniscience_parsers import (
    KubernetesParser,
    ParsedDocument,
    TerraformParser,
    default_dispatch,
    extract_infra_graph,
)
from omniscience_parsers.infra.graph import EdgeData, EntityData

# ============================================================================
# Fixture data — Terraform HCL
# ============================================================================

_TF_SIMPLE = b"""\
resource "aws_s3_bucket" "my_bucket" {
  bucket = "my-data"
  tags = {
    Environment = "prod"
  }
}
"""

_TF_WITH_DEPS = b"""\
resource "aws_s3_bucket" "primary" {
  bucket = "primary"
}

resource "aws_s3_bucket_policy" "policy" {
  bucket     = aws_s3_bucket.primary.id
  depends_on = [aws_s3_bucket.primary]
}
"""

_TF_WITH_INTERPOLATION = b"""\
resource "aws_iam_role_policy_attachment" "attach" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}
"""

_TF_DATA_SOURCE = b"""\
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]
}
"""

_TF_MODULE = b"""\
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.0.0"

  name = "my-vpc"
  cidr = "10.0.0.0/16"
}
"""

_TF_VARIABLE = b"""\
variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.micro"
}
"""

_TF_MULTIPLE_BLOCKS = b"""\
variable "region" {
  default = "us-east-1"
}

resource "aws_instance" "web" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = var.region
  depends_on    = [aws_security_group.allow_http]
}

data "aws_ami" "ubuntu" {
  most_recent = true
}
"""

_TF_JSON = b"""\
{
  "resource": {
    "aws_s3_bucket": {
      "my_bucket": {
        "bucket": "my-data"
      }
    }
  },
  "variable": {
    "region": {
      "default": "us-east-1"
    }
  }
}
"""

_TF_EMPTY = b""

_TF_NO_BLOCKS = b"# Just a comment\n# No blocks here\n"

# ============================================================================
# Fixture data — Kubernetes YAML
# ============================================================================

_K8S_DEPLOYMENT = b"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
  namespace: default
  labels:
    app: nginx
spec:
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
        - name: nginx
          image: nginx:latest
"""

_K8S_SERVICE = b"""\
apiVersion: v1
kind: Service
metadata:
  name: nginx-svc
  namespace: default
spec:
  selector:
    app: nginx
  ports:
    - port: 80
      targetPort: 80
"""

_K8S_MULTI_DOC = b"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: webapp
  namespace: production
spec:
  selector:
    matchLabels:
      app: webapp
---
apiVersion: v1
kind: Service
metadata:
  name: webapp-svc
  namespace: production
spec:
  selector:
    app: webapp
  ports:
    - port: 80
"""

_K8S_CONFIGMAP = b"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
  namespace: default
data:
  key: value
"""

_K8S_WITH_VOLUME = b"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: default
spec:
  selector:
    matchLabels:
      app: api
  template:
    spec:
      volumes:
        - name: config-vol
          configMap:
            name: app-config
      containers:
        - name: api
          image: api:1.0
          envFrom:
            - secretRef:
                name: api-secrets
"""

_K8S_OWNER_REF = b"""\
apiVersion: v1
kind: Pod
metadata:
  name: worker-pod
  namespace: default
  ownerReferences:
    - kind: ReplicaSet
      name: worker-rs
      apiVersion: apps/v1
      uid: abc123
spec:
  containers:
    - name: worker
      image: worker:latest
"""

_K8S_NOT_KUBERNETES = b"""\
foo: bar
baz:
  - one
  - two
"""

_K8S_INVALID_YAML = b"""\
kind: Deployment
metadata:
  name: broken
  labels: [invalid yaml: {
"""

_K8S_INGRESS = b"""\
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-ingress
  namespace: default
spec:
  rules:
    - host: example.com
"""


# ============================================================================
# TerraformParser tests
# ============================================================================


class TestTerraformParserCanHandle:
    def setup_method(self) -> None:
        self.parser = TerraformParser()

    def test_handles_tf_extension(self) -> None:
        assert self.parser.can_handle("", ".tf") is True

    def test_handles_tf_json_extension(self) -> None:
        assert self.parser.can_handle("", ".tf.json") is True

    def test_handles_terraform_content_type(self) -> None:
        assert self.parser.can_handle("application/x-terraform", "") is True

    def test_handles_hcl_content_type(self) -> None:
        assert self.parser.can_handle("application/hcl", "") is True

    def test_does_not_handle_python(self) -> None:
        assert self.parser.can_handle("text/x-python", ".py") is False

    def test_does_not_handle_markdown(self) -> None:
        assert self.parser.can_handle("text/markdown", ".md") is False


class TestTerraformParserHCL:
    def setup_method(self) -> None:
        self.parser = TerraformParser()

    def test_resource_section_extracted(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "resource.aws_s3_bucket.my_bucket" in symbols

    def test_resource_heading_path(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        section = next(s for s in doc.sections if s.symbol == "resource.aws_s3_bucket.my_bucket")
        assert section.heading_path == ["resource", "aws_s3_bucket", "my_bucket"]

    def test_resource_metadata_block_type(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        section = next(s for s in doc.sections if s.symbol == "resource.aws_s3_bucket.my_bucket")
        assert section.metadata["block_type"] == "resource"

    def test_resource_metadata_resource_type(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        section = next(s for s in doc.sections if s.symbol == "resource.aws_s3_bucket.my_bucket")
        assert section.metadata["resource_type"] == "aws_s3_bucket"

    def test_explicit_depends_on_captured(self) -> None:
        doc = self.parser.parse(_TF_WITH_DEPS, "main.tf")
        policy = next(
            s for s in doc.sections if s.symbol == "resource.aws_s3_bucket_policy.policy"
        )
        assert "aws_s3_bucket.primary" in policy.metadata["depends_on"]

    def test_implicit_reference_captured(self) -> None:
        doc = self.parser.parse(_TF_WITH_DEPS, "main.tf")
        policy = next(
            s for s in doc.sections if s.symbol == "resource.aws_s3_bucket_policy.policy"
        )
        refs = policy.metadata.get("refs", [])
        assert any("aws_s3_bucket" in r for r in refs)

    def test_interpolation_reference_captured(self) -> None:
        doc = self.parser.parse(_TF_WITH_INTERPOLATION, "main.tf")
        attach = next(
            s
            for s in doc.sections
            if s.symbol == "resource.aws_iam_role_policy_attachment.attach"
        )
        refs = attach.metadata.get("refs", [])
        assert any("aws_iam_role" in r or "aws_iam_policy" in r for r in refs)

    def test_data_source_extracted(self) -> None:
        doc = self.parser.parse(_TF_DATA_SOURCE, "data.tf")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "data.aws_ami.ubuntu" in symbols

    def test_module_extracted(self) -> None:
        doc = self.parser.parse(_TF_MODULE, "main.tf")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "module.vpc" in symbols

    def test_variable_extracted(self) -> None:
        doc = self.parser.parse(_TF_VARIABLE, "variables.tf")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "variable.instance_type" in symbols

    def test_multiple_blocks_all_extracted(self) -> None:
        doc = self.parser.parse(_TF_MULTIPLE_BLOCKS, "main.tf")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "variable.region" in symbols
        assert "resource.aws_instance.web" in symbols
        assert "data.aws_ami.ubuntu" in symbols

    def test_content_type_is_terraform(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        assert doc.content_type == "application/x-terraform"

    def test_language_is_terraform(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        assert doc.language == "terraform"

    def test_line_numbers_positive(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        for section in doc.sections:
            assert section.line_start >= 1
            assert section.line_end >= section.line_start

    def test_empty_file_produces_no_sections(self) -> None:
        doc = self.parser.parse(_TF_EMPTY, "empty.tf")
        assert doc.sections == []

    def test_comment_only_file_produces_no_sections(self) -> None:
        doc = self.parser.parse(_TF_NO_BLOCKS, "comments.tf")
        assert doc.sections == []


class TestTerraformParserJSON:
    def setup_method(self) -> None:
        self.parser = TerraformParser()

    def test_json_resource_extracted(self) -> None:
        doc = self.parser.parse(_TF_JSON, "main.tf.json")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "resource.aws_s3_bucket.my_bucket" in symbols

    def test_json_variable_extracted(self) -> None:
        doc = self.parser.parse(_TF_JSON, "main.tf.json")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "variable.region" in symbols

    def test_json_content_type(self) -> None:
        doc = self.parser.parse(_TF_JSON, "main.tf.json")
        assert doc.content_type == "application/x-terraform"

    def test_invalid_json_graceful(self) -> None:
        doc = self.parser.parse(b"{invalid json}", "bad.tf.json")
        assert "parse_error" in doc.metadata


# ============================================================================
# KubernetesParser tests
# ============================================================================


class TestKubernetesParserCanHandle:
    def setup_method(self) -> None:
        self.parser = KubernetesParser()

    def test_handles_yaml_extension(self) -> None:
        assert self.parser.can_handle("", ".yaml") is True

    def test_handles_yml_extension(self) -> None:
        assert self.parser.can_handle("", ".yml") is True

    def test_handles_kubernetes_content_type(self) -> None:
        assert self.parser.can_handle("application/x-kubernetes", "") is True

    def test_does_not_handle_tf_extension(self) -> None:
        assert self.parser.can_handle("", ".tf") is False

    def test_does_not_handle_markdown(self) -> None:
        assert self.parser.can_handle("text/markdown", ".md") is False


class TestKubernetesParserDeployment:
    def setup_method(self) -> None:
        self.parser = KubernetesParser()

    def test_deployment_section_extracted(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "Deployment/default/nginx" in symbols

    def test_deployment_heading_path(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        section = next(s for s in doc.sections if s.symbol == "Deployment/default/nginx")
        assert section.heading_path == ["Deployment", "default", "nginx"]

    def test_deployment_metadata_kind(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        section = next(s for s in doc.sections if s.symbol == "Deployment/default/nginx")
        assert section.metadata["kind"] == "Deployment"

    def test_deployment_labels_captured(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        section = next(s for s in doc.sections if s.symbol == "Deployment/default/nginx")
        assert section.metadata["labels"].get("app") == "nginx"

    def test_deployment_selector_captured(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        section = next(s for s in doc.sections if s.symbol == "Deployment/default/nginx")
        assert section.metadata["selectors"].get("app") == "nginx"

    def test_content_type_is_kubernetes(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        assert doc.content_type == "application/x-kubernetes"

    def test_language_is_yaml(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        assert doc.language == "yaml"


class TestKubernetesParserService:
    def setup_method(self) -> None:
        self.parser = KubernetesParser()

    def test_service_section_extracted(self) -> None:
        doc = self.parser.parse(_K8S_SERVICE, "service.yaml")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "Service/default/nginx-svc" in symbols

    def test_service_selector_captured(self) -> None:
        doc = self.parser.parse(_K8S_SERVICE, "service.yaml")
        section = next(s for s in doc.sections if s.symbol == "Service/default/nginx-svc")
        assert section.metadata["service_selector"].get("app") == "nginx"


class TestKubernetesParserMultiDoc:
    def setup_method(self) -> None:
        self.parser = KubernetesParser()

    def test_multi_doc_both_extracted(self) -> None:
        doc = self.parser.parse(_K8S_MULTI_DOC, "stack.yaml")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "Deployment/production/webapp" in symbols
        assert "Service/production/webapp-svc" in symbols

    def test_multi_doc_section_count(self) -> None:
        doc = self.parser.parse(_K8S_MULTI_DOC, "stack.yaml")
        assert len(doc.sections) == 2


class TestKubernetesParserEdgeCases:
    def setup_method(self) -> None:
        self.parser = KubernetesParser()

    def test_owner_references_captured(self) -> None:
        doc = self.parser.parse(_K8S_OWNER_REF, "pod.yaml")
        section = next(s for s in doc.sections if s.symbol == "Pod/default/worker-pod")
        assert any("ReplicaSet" in ref for ref in section.metadata["owner_refs"])

    def test_volume_configmap_ref_captured(self) -> None:
        doc = self.parser.parse(_K8S_WITH_VOLUME, "deploy.yaml")
        section = next(s for s in doc.sections if s.symbol == "Deployment/default/api")
        vol_refs = section.metadata.get("volume_refs", [])
        assert any("ConfigMap" in ref and "app-config" in ref for ref in vol_refs)

    def test_secret_envfrom_ref_captured(self) -> None:
        doc = self.parser.parse(_K8S_WITH_VOLUME, "deploy.yaml")
        section = next(s for s in doc.sections if s.symbol == "Deployment/default/api")
        vol_refs = section.metadata.get("volume_refs", [])
        assert any("Secret" in ref and "api-secrets" in ref for ref in vol_refs)

    def test_non_k8s_yaml_falls_back_to_plaintext(self) -> None:
        doc = self.parser.parse(_K8S_NOT_KUBERNETES, "config.yaml")
        assert doc.content_type == "text/plain"

    def test_invalid_yaml_does_not_raise(self) -> None:
        # Should not raise; parse_warnings may be set
        doc = self.parser.parse(_K8S_INVALID_YAML, "broken.yaml")
        assert isinstance(doc, ParsedDocument)

    def test_configmap_extracted(self) -> None:
        doc = self.parser.parse(_K8S_CONFIGMAP, "configmap.yaml")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "ConfigMap/default/app-config" in symbols

    def test_ingress_extracted(self) -> None:
        doc = self.parser.parse(_K8S_INGRESS, "ingress.yaml")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "Ingress/default/my-ingress" in symbols


# ============================================================================
# Graph extractor tests
# ============================================================================


class TestExtractInfraGraphTerraform:
    def setup_method(self) -> None:
        self.parser = TerraformParser()

    def test_entities_returned_for_resource(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        entities, _edges = extract_infra_graph(doc)
        symbols = [e.symbol for e in entities]
        assert "resource.aws_s3_bucket.my_bucket" in symbols

    def test_entity_kind_is_terraform_resource(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        entities, _ = extract_infra_graph(doc)
        entity = next(e for e in entities if e.symbol == "resource.aws_s3_bucket.my_bucket")
        assert entity.kind == "terraform_resource"

    def test_explicit_depends_on_edge(self) -> None:
        doc = self.parser.parse(_TF_WITH_DEPS, "main.tf")
        _, edges = extract_infra_graph(doc)
        depends_on_edges = [
            e for e in edges
            if e.edge_type == "depends_on"
            and e.from_symbol == "resource.aws_s3_bucket_policy.policy"
        ]
        assert len(depends_on_edges) >= 1
        assert any("aws_s3_bucket.primary" in e.to_symbol for e in depends_on_edges)

    def test_implicit_reference_edge(self) -> None:
        doc = self.parser.parse(_TF_WITH_DEPS, "main.tf")
        _, edges = extract_infra_graph(doc)
        ref_edges = [
            e for e in edges
            if e.edge_type == "references"
            and e.from_symbol == "resource.aws_s3_bucket_policy.policy"
        ]
        assert any("aws_s3_bucket" in e.to_symbol for e in ref_edges)

    def test_no_edges_for_independent_resource(self) -> None:
        doc = self.parser.parse(_TF_SIMPLE, "main.tf")
        _, edges = extract_infra_graph(doc)
        # Simple bucket with no deps/refs
        policy_edges = [
            e for e in edges
            if e.from_symbol == "resource.aws_s3_bucket.my_bucket"
        ]
        assert policy_edges == []

    def test_module_entity_kind(self) -> None:
        doc = self.parser.parse(_TF_MODULE, "main.tf")
        entities, _ = extract_infra_graph(doc)
        entity = next(e for e in entities if e.symbol == "module.vpc")
        assert entity.kind == "terraform_module"

    def test_variable_entity_kind(self) -> None:
        doc = self.parser.parse(_TF_VARIABLE, "variables.tf")
        entities, _ = extract_infra_graph(doc)
        entity = next(e for e in entities if e.symbol == "variable.instance_type")
        assert entity.kind == "terraform_variable"

    def test_data_source_entity_kind(self) -> None:
        doc = self.parser.parse(_TF_DATA_SOURCE, "data.tf")
        entities, _ = extract_infra_graph(doc)
        entity = next(e for e in entities if e.symbol == "data.aws_ami.ubuntu")
        assert entity.kind == "terraform_data_source"

    def test_returns_empty_for_non_infra_doc(self) -> None:
        from omniscience_parsers import MarkdownParser

        doc = MarkdownParser().parse(b"# Hello\n\nWorld.\n", "README.md")
        entities, edges = extract_infra_graph(doc)
        assert entities == []
        assert edges == []


class TestExtractInfraGraphKubernetes:
    def setup_method(self) -> None:
        self.parser = KubernetesParser()

    def test_entities_returned_for_deployment(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        entities, _ = extract_infra_graph(doc)
        symbols = [e.symbol for e in entities]
        assert "Deployment/default/nginx" in symbols

    def test_entity_kind_is_k8s_resource(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        entities, _ = extract_infra_graph(doc)
        entity = next(e for e in entities if e.symbol == "Deployment/default/nginx")
        assert entity.kind == "k8s_resource"
        assert entity.extra.get("k8s_kind") == "Deployment"

    def test_entity_labels_populated(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        entities, _ = extract_infra_graph(doc)
        entity = next(e for e in entities if e.symbol == "Deployment/default/nginx")
        assert entity.labels.get("app") == "nginx"

    def test_selects_edge_from_deployment(self) -> None:
        doc = self.parser.parse(_K8S_DEPLOYMENT, "deployment.yaml")
        _, edges = extract_infra_graph(doc)
        selects = [
            e for e in edges
            if e.edge_type == "selects"
            and e.from_symbol == "Deployment/default/nginx"
        ]
        assert len(selects) >= 1
        assert selects[0].extra.get("selector", {}).get("app") == "nginx"

    def test_selects_edge_from_service(self) -> None:
        doc = self.parser.parse(_K8S_SERVICE, "service.yaml")
        _, edges = extract_infra_graph(doc)
        selects = [
            e for e in edges
            if e.edge_type == "selects"
            and e.from_symbol == "Service/default/nginx-svc"
        ]
        assert len(selects) >= 1

    def test_owns_edge_from_owner_ref(self) -> None:
        doc = self.parser.parse(_K8S_OWNER_REF, "pod.yaml")
        _, edges = extract_infra_graph(doc)
        owns = [e for e in edges if e.edge_type == "owns"]
        assert len(owns) >= 1
        assert owns[0].to_symbol == "Pod/default/worker-pod"
        assert "ReplicaSet" in owns[0].from_symbol

    def test_mounts_edge_for_configmap_volume(self) -> None:
        doc = self.parser.parse(_K8S_WITH_VOLUME, "deploy.yaml")
        _, edges = extract_infra_graph(doc)
        mounts = [e for e in edges if e.edge_type == "mounts"]
        assert any("ConfigMap" in e.to_symbol for e in mounts)

    def test_mounts_edge_for_secret_envfrom(self) -> None:
        doc = self.parser.parse(_K8S_WITH_VOLUME, "deploy.yaml")
        _, edges = extract_infra_graph(doc)
        mounts = [e for e in edges if e.edge_type == "mounts"]
        assert any("Secret" in e.to_symbol for e in mounts)

    def test_multi_doc_graph_contains_all_entities(self) -> None:
        doc = self.parser.parse(_K8S_MULTI_DOC, "stack.yaml")
        entities, _ = extract_infra_graph(doc)
        symbols = [e.symbol for e in entities]
        assert "Deployment/production/webapp" in symbols
        assert "Service/production/webapp-svc" in symbols


# ============================================================================
# Dispatch routing tests
# ============================================================================


class TestDispatchRoutesTerraform:
    def setup_method(self) -> None:
        self.dispatch = default_dispatch()

    def test_routes_tf_extension_to_terraform_parser(self) -> None:
        parser = self.dispatch.get_parser("", ".tf")
        assert isinstance(parser, TerraformParser)

    def test_routes_tf_json_extension_to_terraform_parser(self) -> None:
        parser = self.dispatch.get_parser("", ".tf.json")
        assert isinstance(parser, TerraformParser)

    def test_routes_terraform_content_type(self) -> None:
        parser = self.dispatch.get_parser("application/x-terraform", "")
        assert isinstance(parser, TerraformParser)

    def test_parses_tf_content_end_to_end(self) -> None:
        doc = self.dispatch.parse(
            _TF_SIMPLE,
            content_type="",
            file_extension=".tf",
            file_path="main.tf",
        )
        assert doc.content_type == "application/x-terraform"
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "resource.aws_s3_bucket.my_bucket" in symbols


class TestDispatchRoutesKubernetes:
    def setup_method(self) -> None:
        self.dispatch = default_dispatch()

    def test_routes_yaml_extension_to_kubernetes_parser(self) -> None:
        parser = self.dispatch.get_parser("", ".yaml")
        assert isinstance(parser, KubernetesParser)

    def test_routes_yml_extension_to_kubernetes_parser(self) -> None:
        parser = self.dispatch.get_parser("", ".yml")
        assert isinstance(parser, KubernetesParser)

    def test_parses_k8s_content_end_to_end(self) -> None:
        doc = self.dispatch.parse(
            _K8S_DEPLOYMENT,
            content_type="",
            file_extension=".yaml",
            file_path="deployment.yaml",
        )
        assert doc.content_type == "application/x-kubernetes"
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert "Deployment/default/nginx" in symbols


class TestEdgeDataEntityData:
    """Sanity tests for the data-transfer objects themselves."""

    def test_entity_data_defaults(self) -> None:
        e = EntityData(symbol="resource.foo.bar", kind="terraform_resource", name="bar")
        assert e.namespace == ""
        assert e.labels == {}
        assert e.extra == {}

    def test_edge_data_defaults(self) -> None:
        e = EdgeData(from_symbol="a", to_symbol="b", edge_type="depends_on")
        assert e.extra == {}

    def test_entity_data_with_labels(self) -> None:
        e = EntityData(
            symbol="Deployment/default/app",
            kind="k8s_resource",
            name="app",
            namespace="default",
            labels={"app": "app"},
        )
        assert e.labels["app"] == "app"
        assert e.namespace == "default"
