# Power BI Dashboard Design
## Cementing Operations Decision-Support System

---

## Overview

This dashboard serves two audiences with different needs:

| Audience | Question They're Asking | Dashboard Page |
|---|---|---|
| Operations Manager | How did our jobs perform this period? | Page 1: KPI Summary |
| Field Engineer | What happened during a specific job? | Page 2: Job Detail |
| Both | Where are the problems? | Page 3: Alerts & Issues |

---

## Data Sources (connect in this order)

In Power BI Desktop: **Home → Get Data → Text/CSV**

Load all four files from `07_output/`:

| File | Used On |
|---|---|
| `pbi_job_summary.csv` | Page 1, Page 3 |
| `pbi_time_series.csv` | Page 2 |
| `pbi_alerts.csv` | Page 3 |
| `pbi_stage_summary.csv` | Page 2 |

**Relationships to create** (Model view):
- `pbi_job_summary[Job ID]` → `pbi_time_series[Job ID]` (one-to-many)
- `pbi_job_summary[Job ID]` → `pbi_alerts[Job ID]` (one-to-many)
- `pbi_job_summary[Job ID]` → `pbi_stage_summary[Job ID]` (one-to-many)

---

## Page 1: KPI Summary
**Audience:** Operations Manager
**Decision supported:** Which jobs need review? Is our fleet performing to standard?

### Visuals

**Row 1 — Headline KPI Cards (4 cards across top)**
- Total Jobs: `COUNT(Job ID)`
- Success Rate: `DIVIDE(COUNTROWS(FILTER(..., [Job Success]=TRUE)), COUNTROWS(...))`
- Average NPT %: `AVERAGE(NPT %)`
- Average Volume Efficiency %: `AVERAGE(Volume Efficiency %)`

> Why cards first: managers scan headlines before diving into detail.
> These four numbers answer "are we okay?" in under 3 seconds.

**Row 2 — Bar Chart: Volume Efficiency % by Job**
- X-axis: Job ID
- Y-axis: Volume Efficiency %
- Color: Success Label (green = Success, red = Failed)
- Reference line at 90% (the threshold)

> This chart immediately shows which jobs missed the volume target
> and by how much. The reference line makes the threshold visible.

**Row 3 — Left: Donut Chart — Job Success Distribution**
- Values: Count of jobs
- Legend: Success Label

**Row 3 — Right: Scatter Plot — NPT % vs Volume Efficiency %**
- X-axis: NPT %
- Y-axis: Volume Efficiency %
- Size: Total Alerts
- Color: Success Label
- Quadrant lines at NPT=10%, Volume=90%

> This is the most analytical visual on the page. Jobs in the
> top-left quadrant (low NPT, high volume) are your best performers.
> Jobs bottom-right (high NPT, low volume) are your problem jobs.
> Alert count as bubble size adds a third dimension without clutter.

**Row 4 — Table: Job Scorecard**
Columns: Job ID | Well | NPT % | Volume Eff % | Pressure Stability | Total Alerts | Success Label | Failure Reason

> Conditional formatting:
> - NPT %: green < 5%, yellow 5-10%, red > 10%
> - Volume Eff %: green > 90%, yellow 70-90%, red < 70%
> - Success Label: green/red background

**Filters (right panel):**
- Success Label slicer
- Well slicer
- NPT Category slicer

---

## Page 2: Job Detail
**Audience:** Field Engineer
**Decision supported:** What exactly happened during this job? Where did it go wrong?

### Visuals

**Top — Job selector slicer**
- Single select dropdown: Job ID
- Selecting a job filters ALL visuals on this page

**Row 1 — Context Cards (5 cards)**
- Well location
- Job duration (minutes)
- Pumping time (minutes)
- NPT time (minutes)
- Job outcome (Success/Failed)

**Row 2 — Line Chart: Pressure Over Time (main chart)**
- X-axis: Timestamp
- Y-axis: Pressure (psi)
- Lines: Pressure (psi) + Pressure Rolling Mean (dashed)
- Markers: Has Alert = TRUE points highlighted in red
- Stage shown as background color bands (use analytics pane)

> This is the chart a field engineer actually uses to understand
> what happened. The rolling mean shows the trend — pressure readings
> jumping far above it are the anomalies. Stage bands show context:
> was the spike during pumping or displacement?

**Row 3 — Left: Line Chart: Flow Rate Over Time**
- X-axis: Timestamp
- Y-axis: Flow Rate (bpm)
- Color: Is Active Stage (active = blue, inactive = grey)

**Row 3 — Right: Bar Chart: Stage Duration Breakdown**
- Source: pbi_stage_summary
- X-axis: Stage (sorted by Stage Order)
- Y-axis: Duration (min)
- Color: Stage

> Shows how time was distributed across stages for this job.
> An unusually long idle stage after pumping started = NPT evidence.

**Row 4 — Table: Alerts for This Job**
- Source: pbi_alerts filtered to selected Job ID
- Columns: Timestamp | Alert Label | Severity | Stage | Description
- Conditional formatting: Severity — critical = red, warning = amber

---

## Page 3: Alerts & Issues
**Audience:** Both (operations review meetings)
**Decision supported:** What are our recurring failure patterns? Which alert types dominate?

### Visuals

**Row 1 — Alert KPI Cards**
- Total Alerts
- Critical Alerts: `COUNTROWS(FILTER(pbi_alerts, [Severity]="critical"))`
- Jobs with Alerts: `DISTINCTCOUNT(pbi_alerts[Job ID])`
- Most Common Alert: (use a card with top N filter)

**Row 2 — Left: Bar Chart: Alerts by Type**
- X-axis: Alert Label
- Y-axis: Count
- Color: Severity (critical = red, warning = amber)

**Row 2 — Right: Bar Chart: Alerts by Job**
- X-axis: Job ID
- Y-axis: Count of alerts
- Color: Alert Type (stacked)

> Stacked by type shows whether one job has many types of problems
> (systemic issue) vs many of the same type (specific fault).

**Row 3 — Full width: Alert Timeline**
- X-axis: Timestamp
- Y-axis: Job ID
- Markers: one dot per alert, sized by severity
- Color: Alert Type

> This is a Gantt-style scatter chart. Shows when during each job
> alerts fired. Clusters of alerts = chaotic periods. Isolated
> alerts = single events. Pattern is immediately visible.

**Row 4 — Detail Table: Full Alert Log**
- All columns from pbi_alerts
- Sort: Severity Order ascending (critical first)
- Conditional formatting on Severity column

**Filters:**
- Date range
- Severity slicer
- Alert Type slicer
- Well slicer

---

## Formatting Guidelines

**Colors (use consistently across all pages):**
- Success / Normal: `#2ECC71` (green)
- Warning: `#F39C12` (amber)
- Critical / Failed: `#E74C3C` (red)
- Pumping stage: `#3498DB` (blue)
- Displacement stage: `#9B59B6` (purple)
- Idle stage: `#95A5A6` (grey)
- Flush stage: `#1ABC9C` (teal)

**Typography:**
- Page titles: Segoe UI, 16pt, bold
- Card values: Segoe UI, 28pt, bold
- Table text: Segoe UI, 10pt

**Layout:**
- Dark header bar at top of each page with page title and logo placeholder
- Consistent left-side filter panel across all pages
- White card backgrounds with subtle shadow

---

## Interview Talking Points for This Dashboard

**Why three pages and not one?**
> "Each page answers a different question at a different level of detail.
> The KPI page is strategic — a manager scans it in 30 seconds.
> The Job Detail page is operational — an engineer spends 10 minutes on it
> reviewing one job. Mixing them would serve neither user well."

**Why pre-compute categories in Python instead of DAX?**
> "DAX can compute these, but every calculation runs at query time.
> Pre-computing in Python means Power BI reads static values — faster
> refresh, simpler report, easier to maintain. The logic lives in one
> place: config.py."

**What decision does the scatter plot on Page 1 support?**
> "It shows the trade-off between two independent failure modes — NPT
> and volume efficiency. A job can fail on either dimension independently.
> The scatter makes both visible simultaneously and clusters naturally
> form around job quality patterns."
