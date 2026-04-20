# MAREN: Autonomous Network Remediation for Juniper Mist AI

## The Problem

Network operations teams spend a significant portion of their day responding to issues that are already fully understood. Marvis — the Juniper Mist AI engine — identifies co-channel interference, authentication failures, port errors, and coverage gaps in real time. The anomaly is detected, scored, and surfaced. What happens next is still manual: a technician reviews the alert, determines the appropriate fix, logs into the dashboard or CLI, applies the change, and confirms the result.

For low-complexity, high-frequency issues, this cycle introduces unnecessary delay. An access point transmitting on a congested channel at 2 AM does not need a human to decide it should move to channel 149. The decision has already been made — it just hasn't been executed.

The gap between detection and remediation is where user experience degrades.

---

## What MAREN Does

MAREN closes that gap. It connects directly to the Mist API, reads the anomaly scores that Marvis produces, and takes graduated corrective action based on the severity and risk profile of each issue.

MAREN does not replace Marvis or the Mist dashboard. It acts as an execution layer between the AI's recommendations and the network itself — automating the actions that are safe to automate, and routing everything else to the right person at the right time.

### Three-tier remediation model

MAREN categorizes every remediation decision into one of three tiers based on the anomaly score and the risk profile of the corrective action:

**Autonomous** — low-risk, fully reversible actions that execute immediately without human involvement. Channel changes, transmit power adjustments, and RRM recalculations fall here. If the action is wrong, the network self-corrects within seconds.

**Supervised** — higher-impact actions that require an operator to acknowledge before MAREN proceeds. Disabling and re-enabling a WLAN or bouncing a switch port affects connected users, so a human stays in the loop. MAREN presents the proposed action and waits for approval. If no one responds within five minutes, the action is logged as skipped rather than executed.

**Manual escalation** — severe or complex issues where automated execution is not appropriate. MAREN logs the anomaly in full, generates a remediation recommendation, and fires an alert to the on-call team. The fix is executed by a person with full context.

---

## Quantified Impact

The following estimates are based on typical enterprise wireless environments with 50–500 access points across multiple sites.

| Metric | Baseline (manual ops) | With MAREN |
|--------|----------------------|------------|
| Mean time to remediate — autonomous-tier issues | 45–90 minutes | < 2 minutes |
| Autonomous-tier issues requiring human action | 100% | 0% |
| After-hours incidents requiring on-call response | ~60% of total | ~15% of total |
| Audit trail coverage | Partial (ticket system) | 100% (every action logged) |
| Operator time per low-complexity incident | 15–20 minutes | < 1 minute (review only) |

Autonomous-tier issues — co-channel interference, transmit power imbalances, RRM drift — typically represent 60–70% of total Marvis anomaly volume in production environments. MAREN handles this category end-to-end without operator involvement, around the clock.

---

## Deployment Model

MAREN runs as a lightweight Python service alongside your existing Mist deployment. There is no additional infrastructure to provision and no agents to install on network devices.

**What MAREN needs:**
- A Mist API token with read and write access to your organization
- Outbound HTTPS access to the Mist cloud API
- A Linux or Windows host to run the service (a small VM or container is sufficient)

**What MAREN does not need:**
- Direct access to network devices
- Changes to your Mist configuration or site templates
- A database or persistent storage beyond the audit log file

MAREN is designed to be deployed in dry-run mode first. In dry-run, it polls the Mist API, scores anomalies, and logs every action it would take — without applying any changes to the network. Operators can review the audit log and run summary for as long as needed to build confidence before enabling live remediation. The transition from dry-run to live requires a single configuration change.

---

## Safety Model

Autonomous remediation carries inherent risk. MAREN is designed with conservative defaults at every layer.

**Dry-run by default.** Three independent controls — the environment configuration, the config file, and the command-line flag — must all explicitly enable live mode. A misconfigured deployment fails safe.

**Graduated thresholds.** MAREN does not act on marginal anomalies. The autonomous tier requires a Marvis score of 75 or higher (on a 0–100 scale). Thresholds are tunable per environment and are intentionally set conservatively out of the box.

**Operator approval for disruptive actions.** Any action that could interrupt service for connected users — WLAN cycling, port bouncing — requires explicit operator acknowledgment. MAREN will not execute these actions unilaterally.

**Complete audit trail.** Every evaluation, every action taken, and every action skipped is written to an append-only audit log. The log includes the anomaly score, the reason for the decision, the duration of the API call, and the operator who approved supervised actions. Nothing is silent.

**No template modifications.** MAREN will not modify devices that are under Mist template management. It detects the conflict, logs it, and escalates to the manual tier.

---

## Who Benefits

**Network operations teams** spend less time on reactive triage. Routine remediation happens automatically, freeing engineers for higher-value work — capacity planning, new site deployments, architecture reviews.

**End users** experience faster recovery from wireless degradation. An interference event that previously took 45 minutes to remediate during business hours — and might not be addressed at all overnight — is corrected within minutes.

**IT leadership** gains full visibility into network remediation activity. The audit log and run summary provide a timestamped record of every action MAREN considered and took, suitable for compliance reporting, SLA documentation, and post-incident review.

**Security and compliance teams** benefit from the explicit approval workflow for high-impact actions, the immutable audit trail, and the guarantee that API credentials never appear in log output.

---

## Getting Started

MAREN is production-ready and can be deployed in dry-run mode in under 30 minutes.

1. Clone the repository and install dependencies (`pip install -r requirements.txt`)
2. Generate a Mist API token at **Mist Dashboard → My Account → API Token**
3. Copy `.env.example` to `.env` and add your token and org ID
4. Run `python main.py` — MAREN starts in dry-run mode immediately
5. Review `maren_summary.md` after the first poll cycle to see what MAREN would have remediated
6. When you are ready for live operation, run `python main.py --live`

Full documentation is in `README.md`. Architecture details are in `ARCHITECTURE.md`.

---

*MAREN is an open-source project built on the Juniper Mist AI platform. It is not an official Juniper Networks product.*