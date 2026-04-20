# MAREN

**Marvis Autonomous Remediation Engine for Networks**

MAREN is an autonomous remediation engine for Juniper Mist AI environments. It polls the Mist API for network anomalies surfaced by Marvis, scores them, and takes graduated corrective action — from fully autonomous fixes to supervised approvals to manual escalation — with a complete audit trail on every run.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          MAREN Runtime                              │
│                                                                     │
│   ┌──────────┐    ┌────────────┐    ┌────────────┐    ┌─────────┐  │
│   │  Poller  │───▶│ Correlator │───▶│   Scorer   │───▶│Decision │  │
│   │          │    │            │    │            │    │ Engine  │  │
│   │ Mist API │    │ Groups by  │    │ 0–100 per  │    │ tier +  │  │
│   │ telemetry│    │ device/site│    │ anomaly    │    │ action  │  │
│   └──────────┘    └────────────┘    └────────────┘    └────┬────┘  │
│        ▲                                                    │       │
│        │                                              ┌─────▼────┐  │
│   Mist Cloud                                         │ Executor │  │
│   (REST API)                                         │          │  │
│                                                      │ autonomous│  │
│                                                      │ supervised│  │
│                                                      │ manual   │  │
│                                                      └─────┬────┘  │
│                                                            │       │
│                          ┌─────────────────────────────────▼─────┐ │
│                          │              Output Layer              │ │
│                          │                                        │ │
│                          │  audit_log.py   summary.py   webhook  │ │
│                          │  (NDJSON)       (Markdown)   (Slack / │ │
│                          │                              PagerDuty│ │
│                          └────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

**Data flow:**

1. **Poller** calls Mist API endpoints (`/stats/devices`, `/insights/sites`) on a configurable interval and returns raw telemetry for all sites in the org.
2. **Correlator** groups telemetry events by device and site, deduplicates overlapping signals, and produces a normalized list of candidate anomalies.
3. **Scorer** assigns each anomaly a score from 0–100 using weighted signal analysis (interference, error rates, client impact, duration).
4. **Decision Engine** maps each score to a remediation tier and selects the appropriate action type.
5. **Executor** carries out the action — or skips it in dry-run mode — via authenticated Mist API calls with automatic retry on 429/5xx.
6. **Output Layer** appends an entry to the NDJSON audit log, (re)writes the Markdown run summary, and fires webhooks if configured.

---

## Remediation Tiers

| Tier | Score Range | Behavior |
|------|-------------|----------|
| autonomous | ≥ 75.0 | Action executes immediately, no approval required |
| supervised | ≥ 85.0 | Action fires only after operator acknowledgment (300s timeout) |
| manual | ≥ 92.0 | Action is logged and escalated; MAREN does not execute it |

Scores below 75.0 are logged and discarded. Thresholds are configurable in `config.yaml`.

> **Note:** `bounce_port` is restricted to supervised tier and above regardless of score. See `config.yaml` → `actions.bounce_port.thresholds`.

---

## Requirements

- Python 3.11+
- Juniper Mist account with API token (read + write scope)
- Outbound HTTPS access to `api.mist.com` (or regional equivalent)

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-org/maren.git
cd maren

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.example .env
$EDITOR .env                       # Set MIST_API_TOKEN and MIST_ORG_ID at minimum

# 5. Review default config (optional)
$EDITOR config.yaml
```

---

## Configuration

MAREN is configured via two files that work together:

| File | Purpose |
|------|---------|
| `.env` | Secrets and environment-specific overrides (never commit this) |
| `config.yaml` | Structural defaults — thresholds, actions, polling, output paths |

Environment variables take precedence over `config.yaml` values where both exist.

### Required `.env` values

| Variable | Description |
|----------|-------------|
| `MIST_API_TOKEN` | Mist API token — generate at Mist dashboard → My Account → API Token |
| `MIST_ORG_ID` | Mist organization UUID |

### Key `config.yaml` settings

| Setting | Default | Description |
|---------|---------|-------------|
| `execution.dry_run` | `true` | Master dry-run switch — must be explicitly disabled for live runs |
| `execution.poll_interval_seconds` | `60` | Telemetry polling frequency |
| `thresholds.autonomous` | `75.0` | Minimum score for autonomous action |
| `thresholds.supervised` | `85.0` | Minimum score for supervised action |
| `thresholds.manual` | `92.0` | Minimum score for manual escalation |
| `supervised.ack_timeout_seconds` | `300` | Seconds to wait for operator ack before skipping |
| `output.audit_log_path` | `maren_audit.json` | NDJSON audit log path |
| `output.summary_path` | `maren_summary.md` | Markdown summary path |

See `.env.example` and `config.yaml` for the full reference with inline documentation.

---

## Running MAREN

### Dry run (default — safe, no network changes)

```bash
python main.py
```

Dry-run mode is the default at every level. MAREN will poll Mist, score anomalies, and log what it *would* do — but will not apply any changes to the network.

### Live run (applies changes)

```bash
python main.py --live
```

The `--live` flag overrides `DRY_RUN=true` in both `.env` and `config.yaml`. Confirm your thresholds and action settings before running live against a production org.

### Additional flags

```bash
# Run for a fixed number of poll cycles then exit
python main.py --cycles 5

# Override poll interval
python main.py --interval 120

# Override log level
python main.py --log-level DEBUG

# Write audit log and summary to a specific directory
python main.py --output-dir /var/log/maren
```

### Run the test suite

```bash
# Using pytest (recommended)
pytest tests/ -v

# Using stdlib unittest (no additional dependencies)
python -m unittest discover tests/ -v
```

All 168 tests run under both runners with no network access required.

---

## Output Files

### Audit log (`maren_audit.json`)

Newline-delimited JSON (NDJSON) — one entry per remediation action, appended on each run. Suitable for ingestion into Splunk, Elastic, or any log pipeline that accepts NDJSON.

```json
{"timestamp": "2026-04-06T08:14:02.341Z", "run_id": "a3f1c2...", "site_id": "site-chi-hq-01", "site_name": "Chicago HQ", "device_mac": "d4:20:b0:c1:3e:a2", "device_name": "ap-lobby-01", "action": "channel_change", "tier": "autonomous", "result": "success", "score": 87.4, "dry_run": false, "duration_ms": 412, "client_count": 23, "device_count": 1, "reason": "Co-channel interference detected on 5 GHz...", "error": null, "operator": null}
```

See `examples/sample_audit_log.json` for a full example with all result types.

### Run summary (`maren_summary.md`)

Markdown report written after each run covering result counts, tier/action breakdowns, the full remediation log table, failure details, and operator actions. See `examples/sample_run_summary.md` for a rendered example.

---

## Webhooks

MAREN can post run summaries to Slack and page PagerDuty on failures or manual escalations.

```bash
# .env
WEBHOOK_SLACK_URL=https://hooks.slack.com/services/...
WEBHOOK_PAGERDUTY_URL=https://events.pagerduty.com/v2/enqueue
```

Enable each integration in `config.yaml`:

```yaml
webhooks:
  slack:
    enabled: true
  pagerduty:
    enabled: true
    severity_filter:
      - failed
      - manual
```

Webhook URLs are redacted from all log output to prevent token leakage.

---

## Security Notes

**Credentials**
- API tokens are read from environment variables or `.env` at startup and never written to logs, audit entries, or webhook payloads.
- `.env` is in `.gitignore` by default. Never commit it.
- Token scope required: read access to device/site stats; write access only if running live.

**Dry-run default**
- `DRY_RUN=true` is the hard default at every layer (env, config, CLI). Three independent controls must all be explicitly overridden to enable live remediation. This is intentional.

**Rate limiting**
- All Mist API calls go through a shared rate limiter and a `with_retries` decorator that handles HTTP 429 with exponential backoff and full jitter. MAREN will not overwhelm the Mist API even under high anomaly load.

**Bounce port protection**
- `bounce_port` is disabled at the autonomous tier by design (threshold set to 999.0). Port bounces always require operator approval or manual execution to prevent inadvertent service disruption.

**Webhook URL handling**
- Query strings in webhook URLs (which contain Slack/PagerDuty tokens) are stripped before any logging occurs.

---

## Project Structure

```
maren/
├── core/
│   ├── poller.py          # Mist API telemetry polling
│   ├── correlator.py      # Signal grouping and deduplication
│   ├── scorer.py          # Anomaly scoring (0–100)
│   └── decision.py        # Tier assignment and action selection
├── output/
│   ├── audit_log.py       # NDJSON audit log writer
│   ├── summary.py         # Markdown run summary renderer
│   └── webhook.py         # Slack and PagerDuty delivery
├── utils/
│   ├── auth.py            # Credential loading and caching
│   ├── logger.py          # Structured logging setup
│   └── rate_limiter.py    # Token bucket + retry decorator
├── tests/
│   ├── test_poller.py     # 34 tests
│   ├── test_scorer.py     # 74 tests
│   └── test_executor.py   # 60 tests
├── examples/
│   ├── sample_audit_log.json     # NDJSON example — all result types and tiers
│   └── sample_run_summary.md     # Rendered Markdown summary example
├── main.py                # Entry point and CLI argument handling
├── config.yaml            # Default configuration
├── .env.example           # Environment variable reference
├── requirements.txt       # Pinned dependencies
├── README.md
├── ARCHITECTURE.md
└── CUSTOMER_IMPACT.md
```

---

## License

MIT