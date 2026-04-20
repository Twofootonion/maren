# MAREN Architecture

**Marvis Autonomous Remediation Engine for Networks**

This document describes MAREN's internal design: how data moves through the system, how anomaly scores are calculated, why the tier model is structured the way it is, how telemetry signals are correlated across devices, and where the current design has known limitations.

---

## Table of Contents

1. [Data Flow](#1-data-flow)
2. [Scoring Model](#2-scoring-model)
3. [Scoring Model — Worked Example](#3-scoring-model--worked-example)
4. [Tier Model Rationale](#4-tier-model-rationale)
5. [Telemetry Correlation](#5-telemetry-correlation)
6. [Output Layer](#6-output-layer)
7. [Known Limitations](#7-known-limitations)

---

## 1. Data Flow

A single MAREN cycle proceeds through six stages. Each stage is implemented as a discrete module with no shared mutable state between them — outputs are passed explicitly as return values.

```
Mist Cloud
    │
    │  HTTPS (REST)
    ▼
┌───────────────────────────────────────────────────────┐
│ 1. Poller (core/poller.py)                            │
│    GET /api/v1/orgs/{org_id}/stats/devices            │
│    GET /api/v1/orgs/{org_id}/insights/sites           │
│    Returns: raw telemetry dicts, one per device/site  │
└───────────────────────┬───────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────┐
│ 2. Correlator (core/correlator.py)                    │
│    Groups signals by device MAC + site ID             │
│    Deduplicates overlapping events                    │
│    Returns: list[AnomalyCandidate]                    │
└───────────────────────┬───────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────┐
│ 3. Scorer (core/scorer.py)                            │
│    Assigns score 0–100 to each AnomalyCandidate       │
│    Applies signal weights and duration multiplier     │
│    Returns: list[ScoredAnomaly]                       │
└───────────────────────┬───────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────┐
│ 4. Decision Engine (core/decision.py)                 │
│    Maps score → tier (autonomous/supervised/manual)   │
│    Selects action type per anomaly class              │
│    Returns: list[RemediationDecision]                 │
└───────────────────────┬───────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────┐
│ 5. Executor (core/executor.py)                        │
│    Executes or dry-runs each RemediationDecision      │
│    Calls Mist API with retry + rate limiting          │
│    Returns: list[RemediationResult]                   │
└───────────────────────┬───────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────┐
│ 6. Output Layer                                       │
│    audit_log.py  → appends NDJSON entry per result    │
│    summary.py    → rewrites Markdown run summary      │
│    webhook.py    → fires Slack / PagerDuty if enabled │
└───────────────────────────────────────────────────────┘
```

### Stage interactions

The Poller has the only external dependency (the Mist API). Every subsequent stage operates on in-memory Python objects. This means the entire pipeline from Correlator through Output Layer can be tested without network access, which is why all 168 unit tests run offline.

The Executor is the only stage that writes back to the Mist API. It is also the only stage where dry-run mode has a visible effect: in dry-run, Executor logs the action it would take and returns a `RemediationResult` with `result="dry_run"` without issuing the API call.

---

## 2. Scoring Model

Each anomaly candidate is scored on a 0–100 scale. The score is a weighted sum of normalized signal values, then multiplied by a duration factor.

### Signal weights

| Signal | Weight | Description |
|--------|--------|-------------|
| interference_index | 0.35 | Co-channel and adjacent-channel interference severity |
| error_rate | 0.25 | Port error rate or authentication failure rate |
| client_impact | 0.20 | Fraction of associated clients actively affected |
| coverage_gap | 0.10 | RSSI degradation vs. site baseline |
| repeat_event | 0.10 | Whether this anomaly recurred in the prior polling cycle |

Each signal value is normalized to [0.0, 1.0] before weighting. The weighted sum produces a base score in [0.0, 100.0].

### Duration multiplier

Anomalies that persist across multiple polling cycles are amplified. The multiplier is:

```
duration_multiplier = 1.0 + (0.05 × min(cycles_active, 10))
```

Maximum multiplier is 1.5 (at 10+ consecutive cycles). This prevents a brief transient spike from triggering autonomous action while ensuring a sustained degradation eventually crosses the threshold even if individual signal values are moderate.

The final score is capped at 100.0 after the multiplier is applied.

### Score interpretation

```
  0 ──────────── 75 ─────────── 85 ──────────── 92 ──── 100
  │   discard    │  autonomous  │  supervised   │  manual │
```

Scores below 75.0 are written to the debug log and discarded. No remediation action is taken.

---

## 3. Scoring Model — Worked Example

**Scenario:** AP `ap-lobby-01` at Chicago HQ is showing degraded performance. The Poller returns the following telemetry for this device:

| Signal | Raw value | Normalized | Weight | Contribution |
|--------|-----------|------------|--------|--------------|
| interference_index | 0.82 | 0.82 | 0.35 | 28.7 |
| error_rate | 0.61 | 0.61 | 0.25 | 15.25 |
| client_impact | 0.55 | 0.55 | 0.20 | 11.0 |
| coverage_gap | 0.30 | 0.30 | 0.10 | 3.0 |
| repeat_event | 1.0 | 1.0 | 0.10 | 10.0 |

**Base score:** 28.7 + 15.25 + 11.0 + 3.0 + 10.0 = **67.95**

This anomaly has been active for 4 consecutive cycles:

```
duration_multiplier = 1.0 + (0.05 × 4) = 1.20
final_score = 67.95 × 1.20 = 81.54
```

**Result:** Score 81.54 falls in the autonomous tier (≥ 75.0, < 85.0). Decision Engine selects `channel_change` as the action for an interference-class anomaly. Executor issues a Mist API call to reassign the AP to a less congested channel. The audit log records `result="success"` and `score=81.54`.

Without the duration multiplier, the base score of 67.95 would have been discarded. The multiplier correctly elevates a persistently degraded AP that would otherwise be missed by a single-cycle snapshot.

---

## 4. Tier Model Rationale

MAREN uses three tiers rather than a binary execute/skip model because different remediation actions carry different blast radii.

**Autonomous tier (score ≥ 75.0)**
Actions in this tier are low-risk, easily reversible, and have no service impact if they misfire. Channel changes and RRM recalculations fall here. The worst-case outcome of an incorrect autonomous action is a brief re-association event for nearby clients — recoverable in seconds with no data loss.

**Supervised tier (score ≥ 85.0)**
Actions in this tier are higher-risk or have broader scope. Disabling and re-enabling a WLAN briefly disconnects all clients on that SSID. Bouncing a switch port drops the connected device. These actions require a human to acknowledge before MAREN proceeds, providing a circuit breaker against false positives at higher impact. The 300-second acknowledgment window is intentionally short — a pending action that nobody reviews within five minutes is treated as skipped and logged, rather than holding indefinitely.

**Manual tier (score ≥ 92.0)**
At this score range, the anomaly is severe enough that the corrective action warrants explicit human execution — either because the fix requires access MAREN doesn't have (template-locked devices, hardware replacement, ISP escalation) or because the scope of impact is too broad to automate safely. MAREN logs the anomaly, records the decision, and fires a PagerDuty alert if configured. No Mist API write call is issued.

**Why these specific thresholds?**
75 / 85 / 92 are starting defaults calibrated against a reference dataset of Marvis anomaly scores from production Mist deployments. They are intentionally conservative — operators are expected to tune them downward over time as they build confidence in MAREN's behavior in their specific environment.

---

## 5. Telemetry Correlation

The Correlator's job is to prevent the same underlying problem from generating multiple redundant remediation actions.

### The problem

A single root cause — for example, a rogue AP on channel 6 — may produce correlated signals across several devices: multiple APs in the same coverage zone will all report elevated interference. Without correlation, MAREN would attempt to remediate each AP independently, potentially issuing five channel changes when one is sufficient.

### How correlation works

The Correlator groups raw telemetry events by two keys: `(site_id, anomaly_class)`. Within each group it applies three filters:

1. **Spatial deduplication:** If multiple APs report the same anomaly class within the same site, only the device with the highest signal severity is retained as the primary candidate. Other affected devices are recorded in the candidate's `correlated_devices` list but do not generate independent actions.

2. **Temporal deduplication:** If an anomaly for the same `(device_mac, anomaly_class)` pair was already remediated in the prior polling cycle with a `success` result, the Correlator suppresses it for one additional cycle. This prevents a remediation from immediately re-triggering before the network has time to stabilize.

3. **Cross-tier deduplication:** If the same device has a pending supervised-tier action awaiting operator acknowledgment, any lower-scoring autonomous-tier action for the same device is held until the supervised action resolves. This prevents partial fixes from complicating the operator's view of the incident.

### What the audit log records

Telemetry arrays (raw client stats, per-radio signal samples, switch port counters) are stripped from audit log entries and replaced with summary counts (`client_count`, `device_count`). This keeps individual log entries under 1 KB regardless of site size. Full telemetry is available in the Mist dashboard for post-incident analysis.

---

## 6. Output Layer

### Audit log (`output/audit_log.py`)

Writes one JSON object per line to the configured NDJSON file. The file is opened in append mode on each run, so the full history accumulates across runs. Each entry is written atomically (a single `file.write()` call with a trailing newline) to minimize the risk of partial writes corrupting the stream.

Fields written per entry: `timestamp`, `run_id`, `site_id`, `site_name`, `device_mac`, `device_name`, `action`, `tier`, `result`, `score`, `dry_run`, `duration_ms`, `client_count`, `device_count`, `reason`, `error`, `operator`.

### Run summary (`output/summary.py`)

Reads the current run's `RemediationResult` list (not the audit log file) and renders a Markdown document. The file is overwritten on each run — it represents the most recent run only. For historical reporting, use the audit log.

### Webhooks (`output/webhook.py`)

Webhook delivery is fire-and-forget with a single retry on connection error. A failed webhook delivery does not affect the audit log write or the process exit code — MAREN never fails a run because a Slack message couldn't be delivered.

Query strings are stripped from webhook URLs before any logging. This protects Slack incoming webhook tokens and PagerDuty integration keys from appearing in log output.

---

## 7. Known Limitations

**Single-org scope.** MAREN operates against one Mist org per process. Multi-org deployments require running separate instances with separate `.env` files.

**No persistent state between runs.** MAREN does not maintain a database. The temporal deduplication in the Correlator is within-process only — if the process restarts between polling cycles, the suppression window resets. In practice this means a remediation could re-trigger on the first cycle after a restart.

**Supervised acknowledgment is polling-based.** The current operator acknowledgment flow checks a configurable endpoint on each cycle rather than maintaining a persistent connection. This introduces up to one poll-interval of latency between an operator approving an action and MAREN executing it.

**No rollback.** MAREN does not attempt to undo a remediation that was applied successfully but produced no improvement. Post-remediation validation (confirming the score dropped after a channel change, for example) is planned but not implemented in v1.0.

**Template-locked devices always fail at manual tier.** Mist API returns HTTP 403 for write operations on template-managed devices. MAREN logs these as `result="failed"` with the API error. The fix requires removing the device from the template in the Mist dashboard, which is outside MAREN's scope.

**Rate limiter is per-process, not per-org.** The token bucket rate limiter enforces API rate limits within a single MAREN process. If multiple MAREN instances run concurrently against the same org, they share the same API quota but maintain independent rate limiters — aggregate call rate could exceed Mist's per-org limit under high anomaly load with multiple instances.