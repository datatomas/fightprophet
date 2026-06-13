# Agent Examples

This folder documents the project-specific AI agent prompts used around Fight Prophet.

## ML Second Opinion

[`ml-second-opinion.md`](ml-second-opinion.md) is a read-only reviewer agent for the MMA/UFC prediction pipeline. It is designed to pressure-test model work before trusting or shipping it:

- feature leakage checks
- time-split and evaluation methodology
- calibration and metric honesty
- CatBoost/LogReg comparison
- tuning convergence and overfitting risks
- feature importance and redundancy review

The agent is intentionally constrained to inspect and report. It should not edit files, rebuild tables, train models, tune hyperparameters, push commits, or mutate production state.

