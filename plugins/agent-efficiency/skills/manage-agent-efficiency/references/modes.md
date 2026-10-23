# Mode and estimator reference

## Mode comparison

| Mode | Primary objective | Typical behavior | Main tradeoff |
| --- | --- | --- | --- |
| Throughput | Finish as soon as practical | More tools, safe parallel work, broad validation | Highest expected token/allowance use |
| Balanced | Balance speed, usage, and confidence | Focused context, useful parallel reads, proportionate tests | Neither absolute fastest nor absolute cheapest |
| Stretch | Minimize tokens and tool calls | Narrow reads, sequential work, concise output, targeted tests | Longer ETA and less optional exploration |

Mode policies never override user intent, safety rules, workspace permissions, or required approvals.

## Estimator inputs

Use all available inputs:

- task complexity and future-task profile;
- model and reasoning effort;
- current context, expected output, and cache ratio;
- expected tools, tool-output volume, retries, and validation;
- local, cloud, or code-review execution surface;
- Fast mode;
- subagent fan-out and media operations;
- plan/metering regime, active usage buckets, remaining credits or used percentage, and reset times;
- other activity sharing the same agentic allowance.

The deterministic estimator intentionally returns ranges. Capacity requires a fresh usage-window
snapshot; its future-task range stays low confidence until comparable local history exists.

## Capacity calculation

For each active window:

```text
estimated turns remaining = remaining capacity / predicted capacity per future turn
```

When only a percentage is available:

```text
estimated turns remaining = (100 - usedPercent) / expected percent consumed per turn
```

Use the smallest result across active windows. Do not sum independent window results.
