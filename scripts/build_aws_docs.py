"""Build enriched AWS resource documents for Omniscience seeding.

Performs two passes:
  1. ResourceGroups Tagging API — full coverage (all tagged resource types)
  2. Per-service describe calls (ELBv2, EC2 SG, RDS, EC2 instances, ECS) —
     merges enriched data into existing documents and creates new documents
     for resources the Tagging API missed (e.g. ELB listeners without tags).

Output format: list of JSON objects
  {
    "external_id": <ARN>,
    "uri":         "aws://<acct>/<region>/<svc>/<rtype>/<rid>",
    "title":       "...",
    "text":        "<human-readable facts>",
    "metadata":    {...structured fields...}
  }

This contract is consumed by scripts/seed_from_docs.py — do not break it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from typing import Any

import boto3
import botocore.config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_REGION = "eu-west-1"
DEFAULT_OUTPUT = "/tmp/aws_docs.json"

ACCOUNTS: list[tuple[str, str, str]] = [
    ("qbiq-shared-ro", "qbiq-shared", "513293611525"),
    ("qbiq-prod-ro", "qbiq-prod", "477664915275"),
    ("qbiq-mgmt-ro", "QbiqAI", "836659526145"),
    ("network-ro", "Network", "072240929110"),
    ("platform-ro", "Platform", "977778967799"),
    ("security-ro", "Security", "965384156200"),
    ("analytics-ro", "analytics", "120945137764"),
    ("log-archive-ro", "Log Archive", "017091937169"),
]

# Resource types that are too noisy / short-lived to be useful for retrieval.
EXCLUDE_TYPES: frozenset[str] = frozenset(
    {
        "eks:pod",
        "ssm:session",
        "ec2:snapshot",
        "ec2:image",
        "ec2:network-interface",
        "backup:recovery-point",
        "elasticache:snapshot",
        "rds:snapshot",
        "ec2:security-group-rule",
        "ec2:elastic-ip",
    }
)

# Adaptive retry with exponential back-off handles transient throttling.
BOTO_CONFIG = botocore.config.Config(
    retries={"max_attempts": 10, "mode": "adaptive"},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(profile: str, region: str) -> boto3.Session | None:
    """Return a boto3 Session for *profile*, or None on credential failure."""
    try:
        return boto3.Session(profile_name=profile, region_name=region)
    except Exception as exc:
        log.warning("profile %s: session error — %s", profile, exc)
        return None


def _client(session: boto3.Session, service: str) -> Any:
    return session.client(service, config=BOTO_CONFIG)


def _arn_to_doc_key(arn: str) -> str:
    return arn


def _make_doc(
    *,
    arn: str,
    account_id: str,
    account_name: str,
    region: str,
    service: str,
    resource_type: str,
    resource_id: str,
    display_name: str,
    tags: dict[str, str],
    extra_text: str = "",
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a document in the canonical Omniscience format."""
    tag_str = ", ".join(f"{k}={v}" for k, v in list(tags.items())[:12]) or "none"
    text = (
        f"AWS {service} {resource_type} '{display_name}' in account "
        f"{account_name} ({account_id}), region {region}. "
        f"Resource ID: {resource_id}. ARN: {arn}. Tags: {tag_str}."
    )
    if extra_text:
        text = f"{text} {extra_text}"

    meta: dict[str, Any] = {
        "service": service,
        "resource_type": f"aws_{service}_{resource_type}",
        "account_id": account_id,
        "account_name": account_name,
        "region": region,
        "arn": arn,
        "name": display_name,
        "tags": tags,
    }
    if extra_meta:
        meta.update(extra_meta)

    return {
        "external_id": arn,
        "uri": f"aws://{account_id}/{region}/{service}/{resource_type}/{resource_id}",
        "title": f"{service}:{resource_type} {display_name} ({account_name})",
        "text": text,
        "metadata": meta,
    }


# ---------------------------------------------------------------------------
# Pass 1 — ResourceGroups Tagging API (base documents)
# ---------------------------------------------------------------------------


def scrape_tagging_api(
    session: boto3.Session,
    account_id: str,
    account_name: str,
    region: str,
) -> dict[str, dict[str, Any]]:
    """Return {arn: doc} for all tagged resources in this account/region."""
    cli = _client(session, "resourcegroupstaggingapi")
    docs: dict[str, dict[str, Any]] = {}
    try:
        paginator = cli.get_paginator("get_resources")
        for page in paginator.paginate(ResourcesPerPage=100):
            for r in page["ResourceTagMappingList"]:
                arn: str = r["ResourceARN"]
                parts = arn.split(":")
                svc = parts[2]
                rtype = parts[5].split("/")[0] if len(parts) > 5 else "?"
                if f"{svc}:{rtype}" in EXCLUDE_TYPES:
                    continue
                rid = (
                    parts[5].split("/", 1)[1]
                    if len(parts) > 5 and "/" in parts[5]
                    else (parts[6] if len(parts) > 6 else parts[5])
                )
                tags = {t["Key"]: t["Value"] for t in r.get("Tags", [])}
                display_name = tags.get("Name") or rid
                docs[arn] = _make_doc(
                    arn=arn,
                    account_id=account_id,
                    account_name=account_name,
                    region=region,
                    service=svc,
                    resource_type=rtype,
                    resource_id=rid,
                    display_name=display_name,
                    tags=tags,
                )
    except Exception as exc:
        log.warning(
            "account %s (%s): tagging API partial error — %s",
            account_name,
            account_id,
            exc,
        )
    return docs


# ---------------------------------------------------------------------------
# Pass 2 — per-service enrichment
# ---------------------------------------------------------------------------


def _merge_enrichment(
    docs: dict[str, dict[str, Any]],
    arn: str,
    *,
    account_id: str,
    account_name: str,
    region: str,
    service: str,
    resource_type: str,
    resource_id: str,
    display_name: str,
    tags: dict[str, str],
    extra_text: str,
    extra_meta: dict[str, Any],
) -> None:
    """Merge enrichment data into an existing doc, or create a new one."""
    if arn in docs:
        doc = docs[arn]
        doc["text"] = doc["text"].rstrip(".") + f". {extra_text}"
        doc["metadata"].update(extra_meta)
    else:
        docs[arn] = _make_doc(
            arn=arn,
            account_id=account_id,
            account_name=account_name,
            region=region,
            service=service,
            resource_type=resource_type,
            resource_id=resource_id,
            display_name=display_name,
            tags=tags,
            extra_text=extra_text,
            extra_meta=extra_meta,
        )


def enrich_elb(
    docs: dict[str, dict[str, Any]],
    session: boto3.Session,
    account_id: str,
    account_name: str,
    region: str,
) -> None:
    """Enrich ELBv2 load balancers, listeners, and target groups."""
    cli = _client(session, "elbv2")
    try:
        _enrich_elb_load_balancers(docs, cli, account_id, account_name, region)
        _enrich_elb_target_groups(docs, cli, account_id, account_name, region)
    except Exception as exc:
        log.warning("account %s: elbv2 enrichment error — %s", account_name, exc)


def _enrich_elb_load_balancers(
    docs: dict[str, dict[str, Any]],
    cli: Any,
    account_id: str,
    account_name: str,
    region: str,
) -> None:
    paginator = cli.get_paginator("describe_load_balancers")
    for page in paginator.paginate():
        for lb in page["LoadBalancers"]:
            lb_arn: str = lb["LoadBalancerArn"]
            name = lb.get("LoadBalancerName", lb_arn.split("/")[-2])
            azs = [az["ZoneName"] for az in lb.get("AvailabilityZones", [])]
            state = lb.get("State", {}).get("Code", "unknown")
            extra_text = (
                f"Load balancer scheme={lb.get('Scheme', 'unknown')}, "
                f"type={lb.get('Type', 'unknown')}, "
                f"dns={lb.get('DNSName', 'none')}, "
                f"state={state}, "
                f"vpc={lb.get('VpcId', 'none')}, "
                f"azs={azs}."
            )
            extra_meta: dict[str, Any] = {
                "lb_scheme": lb.get("Scheme"),
                "lb_type": lb.get("Type"),
                "lb_dns_name": lb.get("DNSName"),
                "lb_state": state,
                "lb_vpc_id": lb.get("VpcId"),
                "lb_azs": azs,
                "lb_listeners": [],
            }
            _merge_enrichment(
                docs,
                lb_arn,
                account_id=account_id,
                account_name=account_name,
                region=region,
                service="elasticloadbalancing",
                resource_type="loadbalancer",
                resource_id=name,
                display_name=name,
                tags={},
                extra_text=extra_text,
                extra_meta=extra_meta,
            )
            _enrich_elb_listeners(docs, cli, lb_arn, account_id, account_name, region)


def _enrich_elb_listeners(
    docs: dict[str, dict[str, Any]],
    cli: Any,
    lb_arn: str,
    account_id: str,
    account_name: str,
    region: str,
) -> None:
    try:
        paginator = cli.get_paginator("describe_listeners")
        listeners: list[dict[str, Any]] = []
        for page in paginator.paginate(LoadBalancerArn=lb_arn):
            for lst in page["Listeners"]:
                listener_info = {
                    "port": lst.get("Port"),
                    "protocol": lst.get("Protocol"),
                    "ssl_policy": lst.get("SslPolicy"),
                    "arn": lst["ListenerArn"],
                }
                listeners.append(listener_info)
                # Create a document for the listener itself.
                lst_arn = lst["ListenerArn"]
                port = lst.get("Port", "?")
                proto = lst.get("Protocol", "?")
                ssl = lst.get("SslPolicy", "")
                extra_text = (
                    f"Listener port={port}, protocol={proto}"
                    + (f", ssl_policy={ssl}" if ssl else "")
                    + f", attached to LB {lb_arn}."
                )
                _merge_enrichment(
                    docs,
                    lst_arn,
                    account_id=account_id,
                    account_name=account_name,
                    region=region,
                    service="elasticloadbalancing",
                    resource_type="listener",
                    resource_id=lst_arn.split("/")[-1],
                    display_name=f"listener:{proto}:{port}",
                    tags={},
                    extra_text=extra_text,
                    extra_meta={
                        "listener_port": port,
                        "listener_protocol": proto,
                        "listener_ssl_policy": ssl or None,
                        "lb_arn": lb_arn,
                    },
                )
        # Attach listener list to the parent LB document.
        if lb_arn in docs and listeners:
            docs[lb_arn]["metadata"]["lb_listeners"] = listeners
            ports_str = ", ".join(str(lst["port"]) for lst in listeners if lst.get("port"))
            if ports_str:
                docs[lb_arn]["text"] += f" Listeners on ports: {ports_str}."
    except Exception as exc:
        log.debug("listeners for %s: %s", lb_arn, exc)


def _enrich_elb_target_groups(
    docs: dict[str, dict[str, Any]],
    cli: Any,
    account_id: str,
    account_name: str,
    region: str,
) -> None:
    paginator = cli.get_paginator("describe_target_groups")
    for page in paginator.paginate():
        for tg in page["TargetGroups"]:
            tg_arn: str = tg["TargetGroupArn"]
            name = tg.get("TargetGroupName", tg_arn.split(":")[-1])
            hc = tg.get("HealthCheckPath", ""), tg.get("HealthCheckPort", "traffic-port")
            hc_text = (
                f"HealthCheck path={hc[0] or 'none'}, port={hc[1]}"
                if hc[0]
                else f"HealthCheck port={hc[1]}"
            )
            extra_text = (
                f"Target group port={tg.get('Port', '?')}, "
                f"protocol={tg.get('Protocol', '?')}, "
                f"target_type={tg.get('TargetType', '?')}. "
                f"{hc_text}."
            )
            _merge_enrichment(
                docs,
                tg_arn,
                account_id=account_id,
                account_name=account_name,
                region=region,
                service="elasticloadbalancing",
                resource_type="targetgroup",
                resource_id=name,
                display_name=name,
                tags={},
                extra_text=extra_text,
                extra_meta={
                    "tg_port": tg.get("Port"),
                    "tg_protocol": tg.get("Protocol"),
                    "tg_target_type": tg.get("TargetType"),
                    "tg_health_check_path": tg.get("HealthCheckPath"),
                    "tg_health_check_port": tg.get("HealthCheckPort"),
                    "lb_arns": tg.get("LoadBalancerArns", []),
                },
            )


def enrich_sg(
    docs: dict[str, dict[str, Any]],
    session: boto3.Session,
    account_id: str,
    account_name: str,
    region: str,
) -> None:
    """Enrich EC2 security groups with inbound/outbound rules."""
    cli = _client(session, "ec2")
    try:
        paginator = cli.get_paginator("describe_security_groups")
        for page in paginator.paginate():
            for sg in page["SecurityGroups"]:
                sg_id: str = sg["GroupId"]
                arn = f"arn:aws:ec2:{region}:{account_id}:security-group/{sg_id}"
                name = sg.get("GroupName", sg_id)
                tags = {t["Key"]: t["Value"] for t in sg.get("Tags", [])}
                display_name = tags.get("Name") or name

                inbound = _format_sg_rules(sg.get("IpPermissions", []))
                outbound = _format_sg_rules(sg.get("IpPermissionsEgress", []))
                extra_text = (
                    f"VPC: {sg.get('VpcId', 'none')}. "
                    f"Inbound rules: {inbound or 'none'}. "
                    f"Outbound rules: {outbound or 'none'}."
                )
                _merge_enrichment(
                    docs,
                    arn,
                    account_id=account_id,
                    account_name=account_name,
                    region=region,
                    service="ec2",
                    resource_type="security-group",
                    resource_id=sg_id,
                    display_name=display_name,
                    tags=tags,
                    extra_text=extra_text,
                    extra_meta={
                        "sg_id": sg_id,
                        "sg_name": name,
                        "sg_description": sg.get("Description"),
                        "sg_vpc_id": sg.get("VpcId"),
                        "sg_inbound_rules": _parse_sg_rules(sg.get("IpPermissions", [])),
                        "sg_outbound_rules": _parse_sg_rules(sg.get("IpPermissionsEgress", [])),
                    },
                )
    except Exception as exc:
        log.warning("account %s: security-group enrichment error — %s", account_name, exc)


def _parse_sg_rules(
    permissions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rules = []
    for perm in permissions:
        from_port = perm.get("FromPort", -1)
        to_port = perm.get("ToPort", -1)
        protocol = perm.get("IpProtocol", "-1")
        cidrs = [r["CidrIp"] for r in perm.get("IpRanges", [])]
        cidrs += [r["CidrIpv6"] for r in perm.get("Ipv6Ranges", [])]
        sg_refs = [r["GroupId"] for r in perm.get("UserIdGroupPairs", [])]
        rules.append(
            {
                "from_port": from_port,
                "to_port": to_port,
                "protocol": protocol,
                "cidrs": cidrs,
                "sg_refs": sg_refs,
            }
        )
    return rules


def _format_sg_rules(permissions: list[dict[str, Any]]) -> str:
    parts = []
    for perm in permissions[:8]:  # cap verbosity
        proto = perm.get("IpProtocol", "-1")
        fp = perm.get("FromPort", -1)
        tp = perm.get("ToPort", -1)
        port_str = "all" if fp == -1 else (str(fp) if fp == tp else f"{fp}-{tp}")
        cidrs = [r["CidrIp"] for r in perm.get("IpRanges", [])]
        cidrs += [r["CidrIpv6"] for r in perm.get("Ipv6Ranges", [])]
        sg_refs = [r["GroupId"] for r in perm.get("UserIdGroupPairs", [])]
        targets = cidrs + sg_refs or ["?"]
        parts.append(f"{proto}:{port_str} from {'+'.join(targets[:3])}")
    return "; ".join(parts)


def enrich_rds(
    docs: dict[str, dict[str, Any]],
    session: boto3.Session,
    account_id: str,
    account_name: str,
    region: str,
) -> None:
    """Enrich RDS instances with engine, endpoint, and storage details."""
    cli = _client(session, "rds")
    try:
        paginator = cli.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page["DBInstances"]:
                arn: str = db["DBInstanceArn"]
                db_id = db["DBInstanceIdentifier"]
                endpoint = db.get("Endpoint", {})
                ep_addr = endpoint.get("Address", "none")
                ep_port = endpoint.get("Port", "none")
                extra_text = (
                    f"Engine: {db.get('Engine', '?')} "
                    f"{db.get('EngineVersion', '')}. "
                    f"Instance class: {db.get('DBInstanceClass', '?')}. "
                    f"Endpoint: {ep_addr}:{ep_port}. "
                    f"MultiAZ: {db.get('MultiAZ', False)}. "
                    f"Storage: {db.get('StorageType', '?')} "
                    f"{db.get('AllocatedStorage', '?')}GB."
                )
                tags = {t["Key"]: t["Value"] for t in db.get("TagList", [])}
                _merge_enrichment(
                    docs,
                    arn,
                    account_id=account_id,
                    account_name=account_name,
                    region=region,
                    service="rds",
                    resource_type="db",
                    resource_id=db_id,
                    display_name=db_id,
                    tags=tags,
                    extra_text=extra_text,
                    extra_meta={
                        "rds_engine": db.get("Engine"),
                        "rds_engine_version": db.get("EngineVersion"),
                        "rds_instance_class": db.get("DBInstanceClass"),
                        "rds_endpoint_address": ep_addr,
                        "rds_endpoint_port": ep_port,
                        "rds_multi_az": db.get("MultiAZ", False),
                        "rds_storage_type": db.get("StorageType"),
                        "rds_allocated_storage_gb": db.get("AllocatedStorage"),
                        "rds_status": db.get("DBInstanceStatus"),
                    },
                )
    except Exception as exc:
        log.warning("account %s: rds enrichment error — %s", account_name, exc)


def enrich_ec2(
    docs: dict[str, dict[str, Any]],
    session: boto3.Session,
    account_id: str,
    account_name: str,
    region: str,
) -> None:
    """Enrich EC2 instances with instance type, state, and network details."""
    cli = _client(session, "ec2")
    try:
        paginator = cli.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page["Reservations"]:
                for inst in reservation["Instances"]:
                    inst_id = inst["InstanceId"]
                    arn = f"arn:aws:ec2:{region}:{account_id}:instance/{inst_id}"
                    state = inst.get("State", {}).get("Name", "unknown")
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    display_name = tags.get("Name") or inst_id
                    extra_text = (
                        f"Instance type: {inst.get('InstanceType', '?')}. "
                        f"State: {state}. "
                        f"Private IP: {inst.get('PrivateIpAddress', 'none')}. "
                        f"VPC: {inst.get('VpcId', 'none')}. "
                        f"Subnet: {inst.get('SubnetId', 'none')}. "
                        f"Image: {inst.get('ImageId', 'none')}."
                    )
                    _merge_enrichment(
                        docs,
                        arn,
                        account_id=account_id,
                        account_name=account_name,
                        region=region,
                        service="ec2",
                        resource_type="instance",
                        resource_id=inst_id,
                        display_name=display_name,
                        tags=tags,
                        extra_text=extra_text,
                        extra_meta={
                            "ec2_instance_type": inst.get("InstanceType"),
                            "ec2_state": state,
                            "ec2_private_ip": inst.get("PrivateIpAddress"),
                            "ec2_vpc_id": inst.get("VpcId"),
                            "ec2_subnet_id": inst.get("SubnetId"),
                            "ec2_image_id": inst.get("ImageId"),
                            "ec2_launch_time": (
                                inst["LaunchTime"].isoformat() if inst.get("LaunchTime") else None
                            ),
                        },
                    )
    except Exception as exc:
        log.warning("account %s: ec2-instances enrichment error — %s", account_name, exc)


def enrich_ecs(
    docs: dict[str, dict[str, Any]],
    session: boto3.Session,
    account_id: str,
    account_name: str,
    region: str,
) -> None:
    """Enrich ECS clusters and services with desired/running counts."""
    cli = _client(session, "ecs")
    try:
        cluster_arns = _list_ecs_clusters(cli)
        for cluster_arn in cluster_arns:
            _enrich_ecs_services(docs, cli, cluster_arn, account_id, account_name, region)
    except Exception as exc:
        log.warning("account %s: ecs enrichment error — %s", account_name, exc)


def _list_ecs_clusters(cli: Any) -> list[str]:
    arns: list[str] = []
    paginator = cli.get_paginator("list_clusters")
    for page in paginator.paginate():
        arns.extend(page.get("clusterArns", []))
    return arns


def _enrich_ecs_services(
    docs: dict[str, dict[str, Any]],
    cli: Any,
    cluster_arn: str,
    account_id: str,
    account_name: str,
    region: str,
) -> None:
    service_arns: list[str] = []
    svc_paginator = cli.get_paginator("list_services")
    for page in svc_paginator.paginate(cluster=cluster_arn):
        service_arns.extend(page.get("serviceArns", []))
    if not service_arns:
        return

    # describe_services accepts up to 10 ARNs per call.
    for i in range(0, len(service_arns), 10):
        batch = service_arns[i : i + 10]
        try:
            resp = cli.describe_services(cluster=cluster_arn, services=batch)
        except Exception as exc:
            log.debug("ecs describe_services batch error: %s", exc)
            continue
        for svc in resp.get("services", []):
            svc_arn: str = svc["serviceArn"]
            svc_name = svc.get("serviceName", svc_arn.split("/")[-1])
            desired = svc.get("desiredCount", 0)
            running = svc.get("runningCount", 0)
            task_def = svc.get("taskDefinition", "unknown")
            launch_type = svc.get("launchType", "unknown")
            extra_text = (
                f"ECS service desired={desired}, running={running}, "
                f"launch_type={launch_type}, "
                f"task_definition={task_def.split('/')[-1]}."
            )
            _merge_enrichment(
                docs,
                svc_arn,
                account_id=account_id,
                account_name=account_name,
                region=region,
                service="ecs",
                resource_type="service",
                resource_id=svc_name,
                display_name=svc_name,
                tags={},
                extra_text=extra_text,
                extra_meta={
                    "ecs_desired_count": desired,
                    "ecs_running_count": running,
                    "ecs_launch_type": launch_type,
                    "ecs_task_definition": task_def,
                    "ecs_cluster_arn": cluster_arn,
                    "ecs_status": svc.get("status"),
                },
            )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _scrape_account(
    profile: str,
    account_name: str,
    account_id: str,
    region: str,
) -> dict[str, dict[str, Any]]:
    """Scrape one AWS account; return {arn: doc}."""
    session = _make_session(profile, region)
    if session is None:
        return {}

    # Pass 1: base documents from Tagging API.
    docs = scrape_tagging_api(session, account_id, account_name, region)
    base_count = len(docs)
    log.info("%s (%s): %d base docs from Tagging API", account_name, account_id, base_count)

    # Pass 2: per-service enrichment.
    enrichers = [
        ("elbv2", enrich_elb),
        ("ec2-sg", enrich_sg),
        ("rds", enrich_rds),
        ("ec2", enrich_ec2),
        ("ecs", enrich_ecs),
    ]
    for label, fn in enrichers:
        try:
            fn(docs, session, account_id, account_name, region)
        except Exception as exc:
            log.warning("%s (%s): %s enrichment failed — %s", account_name, account_id, label, exc)

    log.info(
        "%s (%s): %d docs after enrichment (+%d new)",
        account_name,
        account_id,
        len(docs),
        len(docs) - base_count,
    )
    return docs


def build_docs(
    accounts: list[tuple[str, str, str]],
    region: str,
) -> list[dict[str, Any]]:
    """Scrape all accounts; return deduplicated document list."""
    all_docs: dict[str, dict[str, Any]] = {}
    counter: Counter[str] = Counter()

    for profile, account_name, account_id in accounts:
        account_docs = _scrape_account(profile, account_name, account_id, region)
        for arn, doc in account_docs.items():
            all_docs[arn] = doc
            counter[doc["metadata"].get("resource_type", "unknown")] += 1

    return list(all_docs.values())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build enriched AWS resource documents for Omniscience seeding."
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region to scrape (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--accounts",
        nargs="+",
        metavar="PROFILE:NAME:ID",
        help=(
            "Override account list as PROFILE:NAME:ID triplets. "
            "Default: all 8 production accounts."
        ),
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    accounts: list[tuple[str, str, str]]
    if args.accounts:
        accounts = []
        for entry in args.accounts:
            parts = entry.split(":", 2)
            if len(parts) != 3:
                log.error("Invalid account spec %r — expected PROFILE:NAME:ID", entry)
                sys.exit(1)
            accounts.append((parts[0], parts[1], parts[2]))
    else:
        accounts = ACCOUNTS

    docs = build_docs(accounts, args.region)

    with open(args.output, "w") as fh:
        json.dump(docs, fh)

    # Summary
    counter: Counter[str] = Counter()
    lb_with_ports = 0
    for doc in docs:
        counter[doc["metadata"].get("resource_type", "unknown")] += 1
        if "loadbalancer" in doc["metadata"].get("resource_type", "") and doc["metadata"].get(
            "lb_listeners"
        ):
            lb_with_ports += 1

    print(f"\nTotal docs: {len(docs)}")
    print(f"LB docs with listener ports: {lb_with_ports}")
    print("Top resource types:")
    for rtype, cnt in counter.most_common(25):
        print(f"  {rtype:<45} {cnt}")
    print(f"\nOutput: {args.output}")


if __name__ == "__main__":
    main()
