---
title: Redis evictions spiking
alert_patterns: ["alert://datadog/redis.*"]
tags:
  - service:cache
  - severity:sev3
severity: sev3
---

# Redis evictions spiking

When Redis hits maxmemory it starts evicting keys based on the policy.

## Check

Look at the eviction counter.

```bash
redis-cli info stats | grep evicted_keys
```

## Mitigate

Bump maxmemory by 25% if the host has headroom.
