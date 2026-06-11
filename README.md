# S.P.E.A.R. — Statistical Proactive Early Adaptive Routing

> An SDN framework for proactive congestion avoidance and flow-aware traffic engineering in multi-path mesh topologies.

**Mini Project | B.Tech Computer Science and Engineering**  
College of Engineering Trivandrum | APJ Abdul Kalam Technological University | April 2026

---

## Overview

Modern mesh networks suffer from a fundamental problem: static shortest-path routing over-utilizes primary links while leaving redundant paths completely idle. When high-bandwidth **Elephant flows** (bulk TCP transfers) share the network with latency-sensitive **Mouse flows** (ICMP, small TCP), the Elephant flows overwhelm the primary path, causing packet loss and high latency for the Mouse flows — *before* any rerouting is triggered.

**S.P.E.A.R.** addresses this by shifting from reactive to **proactive** traffic engineering. Instead of waiting for congestion to occur, the framework uses trend-based statistical analysis to *anticipate* link saturation and reroute Elephant flows before performance degrades.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│              CONTROL PLANE  (S.P.E.A.R. Logic)          │
│                                                         │
│  ┌──────────────────┐        ┌────────────────────────┐ │
│  │  Traffic Monitor  │───────▶│   Adaptive Routing     │ │
│  │  (Trend Analysis) │        │   Engine               │ │
│  └────────┬─────────┘        │   (Ghost Weights +      │ │
│           │                  │    Dijkstra)            │ │
│  ┌────────▼─────────┐        └────────────────────────┘ │
│  │  Flow Classifier  │                                   │
│  │ (Elephant/Mouse)  │                                   │
│  └───────────────────┘                                   │
└─────────────────────────────────────────────────────────┘
                        │
          Southbound Interface (OpenFlow 1.3)
                        │
┌─────────────────────────────────────────────────────────┐
│                DATA PLANE  (Switches / Hosts)            │
└─────────────────────────────────────────────────────────┘
```

---

## Network Topology

```
                  ┌──── s2 ────┐
h1 ──── s1 ───────┼──── s3 ────┼──── s4 ──── h2
                  └── s5 ─ s6 ─┘
```

| Path | Route | Bandwidth | Delay | Role |
|------|-------|-----------|-------|------|
| A | s1 → s2 → s4 | 10 Mbps | 5 ms/link | Primary (congests first) |
| B | s1 → s3 → s4 | 10 Mbps | 5 ms/link | Secondary |
| C | s1 → s5 → s6 → s4 | 100 Mbps | 10 ms/link | Elephant fallback |

Mouse flows prefer the short low-latency Paths A/B. Elephant flows are proactively redirected to the wide Path C before A/B saturate.

---

## Key Components

### 1. Predictive Utilization Index (PUI)
Computes a trend-aware metric per link using real-time byte count statistics:

```
PUI = speed + 0.5 × acceleration
```

Where speed = Mbps over the last interval and acceleration = rate of change. This slope-based analysis lets the controller anticipate saturation *before* it occurs, rather than reacting after packet loss.

### 2. Flow Classification
Flows are classified at Layer 4 on first Packet-In:
- **Elephant flows** — TCP traffic on port 5001 (iperf) → routed via Ghost Weight Dijkstra
- **Mouse flows** — all other IP traffic → routed via standard shortest-path Dijkstra

### 3. Ghost Weight Mechanism
When PUI exceeds the congestion threshold on a link, a penalty `α` is added to its weight:

```
w_ghost(e) = w(e) + α
```

This makes the link expensive for Elephant path selection without removing it from the topology. Elephant flows naturally avoid it via Dijkstra. Mouse flows use a separate unmodified graph and are unaffected.

### 4. Damping Mechanism
Ghost weights are gradually reduced by a fixed step every monitoring interval:

```
w(e) ← max(1, w(e) − DAMPING_STEP)   every 5 seconds
```

This prevents route oscillation and TCP session disruption caused by rapid weight swings (route flapping).

### 5. Synthetic ARP Proxy
The controller maintains an IP-to-MAC table. On an ARP REQUEST:
- **Target known** → controller crafts a synthetic ARP REPLY directly. Zero flooding.
- **Target unknown** → flood normally; reply teaches the controller the MAC.

This eliminates broadcast storms inherent to redundant mesh topologies, which caused 100% packet loss in the static routing baseline.

---

## Results

| Metric | Static Routing (Baseline) | S.P.E.A.R. |
|--------|--------------------------|------------|
| Elephant Flow Throughput | 5–8 Mbps (unstable) | **9.08 Mbps** (stable) |
| Mouse Flow Throughput | Significant degradation | **10.0 Mbps** (protected) |
| Packet Loss | **100%** (ARP flooding) | **0%** |
| Jain Fairness Index | < 0.7 | **0.9983** |
| Round-Trip Time | Unstable | **~40 ms** (stable) |

---

## How to Run

### Prerequisites

```bash
# Install Ryu SDN Framework
pip install ryu

# Install Mininet
sudo apt-get install mininet
```

### Step 1 — Start the Controller

```bash
ryu-manager spear_controller.py --observe-links
```

Wait until you see `S1...S6 connected` in the terminal.

### Step 2 — Start the Topology

```bash
sudo mn --custom topology.py \
        --topo triple_path \
        --controller remote,ip=127.0.0.1,port=6633 \
        --switch ovsk,protocols=OpenFlow13 \
        --link tc
```

> `--link tc` is **required** to enforce the bandwidth and delay parameters. Without it, Mininet ignores them.

### Step 3 — Test

```bash
# Inside Mininet CLI

# Verify connectivity (should be 0% loss)
mininet> pingall

# Run a Mouse flow
mininet> h2 iperf -s -p 9999 &
mininet> h1 iperf -c h2 -p 9999 -t 30 &

# Run an Elephant flow (triggers proactive rerouting)
mininet> h2 iperf -s -p 5001 &
mininet> h1 iperf -c h2 -p 5001 -t 60

# Watch Terminal 1 for [ELEPHANT], [REROUTE], [GHOST], [DAMPING] logs
```

### Verify Installed Flow Rules

```bash
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

---

## Configuration

Key tunable constants in `spear_controller.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `CONGESTION_THRESHOLD_MBPS` | 50 | PUI threshold to trigger rerouting |
| `GHOST_WEIGHT_PENALTY` | 1000 | Weight added to congested links |
| `GHOST_WEIGHT_MAX` | 2000 | Maximum ghost weight cap |
| `DAMPING_STEP` | 200 | Weight recovered per interval |
| `MONITOR_INTERVAL_SEC` | 5 | Port stats polling interval |
| `MOUSE_LINK_PENALTY` | 300 | Per-mouse weight boost for elephant path selection |
| `ELEPHANT_PORT` | 5001 | TCP port used to identify elephant flows |

---

## Files

| File | Description |
|------|-------------|
| `spear_controller.py` | Ryu SDN controller — full S.P.E.A.R. logic |
| `topology.py` | Mininet custom topology — 6-switch triple-path mesh |

---

## Team

| Name | Roll No. |
|------|----------|
| Angel Roy | TVE23CS035 |
| Angelina Raj | TVE23CS033 |
| Ashna Vijayan | TVE23CS043 |
| Devika G | TVE23CS052 |

**Guide:** Prof. Narasimhan T, Assistant Professor, Dept. of CSE, CET  
**Institution:** College of Engineering Trivandrum, Kerala

---

## Tech Stack

- **Language:** Python
- **SDN Framework:** Ryu Controller
- **Protocol:** OpenFlow 1.3
- **Emulator:** Mininet 2.3
- **Traffic Tools:** iPerf3, ICMP ping
