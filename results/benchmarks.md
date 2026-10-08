# Benchmarks

`make bench` · 2026-10-08T20:42:28+0530 · Apple M3, 8 cores, 16.0 GB RAM, macOS-26.5.1-arm64-arm-64bit, Python 3.11.14

Mock upstream fixed at 300 ms (it also answers the faithfulness judge, in the same time). Each run is 30 s of Locust traffic: the dev set's safe cases, round-robin, each user sending its next request as soon as the last one returns. Guard decision cache off. *Added* latency is total minus upstream, from the proxy's request log; *client* latency includes the mock's 300 ms (twice with the judge). CPU is % of one core, for the proxy process only.

| Scenario | Users | Req/s | Added p50 ms | Added p99 ms | Client p50 ms | Client p99 ms | CPU % mean | Peak RAM MB | Blocked | HTTP errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| no_guards | 1 | 3.3 | 0.0 | 0.1 | 310 | 310 | 2 | 70 | 0/98 | 0 |
| no_guards | 10 | 32.2 | 0.0 | 0.0 | 310 | 320 | 12 | 71 | 0/973 | 0 |
| no_guards | 50 | 149.4 | 0.0 | 0.0 | 330 | 380 | 20 | 74 | 0/4477 | 0 |
| cheap_only | 1 | 3.2 | 0.7 | 4.0 | 310 | 310 | 2 | 71 | 0/98 | 0 |
| cheap_only | 10 | 32.0 | 4.2 | 12.8 | 310 | 330 | 11 | 71 | 0/964 | 0 |
| cheap_only | 50 | 151.8 | 9.8 | 26.9 | 330 | 380 | 22 | 74 | 0/4611 | 0 |
| all_but_faithfulness | 1 | 2.7 | 60.0 | 122.8 | 360 | 430 | 84 | 1797 | 0/82 | 0 |
| all_but_faithfulness | 10 | 20.5 | 155.2 | 496.3 | 460 | 800 | 586 | 1811 | 0/621 | 0 |
| all_but_faithfulness | 50 | 26.9 | 1576.5 | 2532.8 | 1700 | 2800 | 770 | 1452 | 533/844 | 0 |
| all | 1 | 0.4 | 2260.2 | 2283.7 | 2600 | 2600 | 373 | 3752 | 0/12 | 0 |
| all | 10 | 3.3 | 3005.8 | 3061.7 | 3000 | 3100 | 777 | 3021 | 100/100 | 0 |
| all | 50 | 16.3 | 3014.6 | 3093.2 | 3000 | 3400 | 780 | 3027 | 504/504 | 0 |

All traffic is safe, so *Blocked* counts guards that failed closed: a guard that overruns its policy's `timeout_ms` (or crashes) is decided by `on_error`, and the injection guards fail closed.

Policies per scenario:

- `no_guards`: none
- `cheap_only`: max_length, output_schema, secrets
- `all_but_faithfulness`: banned_topics, jailbreak, max_length, output_schema, pii, prompt_injection, secrets, toxicity
- `all` (requests send the FAQ as context): banned_topics, faithfulness, jailbreak, max_length, output_schema, pii, prompt_injection, secrets, toxicity

Per-guard latency, `cheap_only`, 1 user(s):

| Guard | p50 ms | p99 ms |
|---|---:|---:|
| output_schema@1.0.0 | 0.1 | 3.6 |
| secrets@1.0.1 | 0.2 | 0.4 |
| max_length@1.0.0 | 0.1 | 0.2 |

Per-guard latency, `all_but_faithfulness`, 1 user(s):

| Guard | p50 ms | p99 ms |
|---|---:|---:|
| prompt_injection@1.1.1 | 30.0 | 83.8 |
| jailbreak@1.1.1 | 29.9 | 83.8 |
| toxicity@1.1.0 | 28.3 | 56.1 |
| pii@1.0.1 | 1.6 | 30.1 |
| banned_topics@1.0.0 | 6.0 | 28.3 |
| output_schema@1.0.0 | 0.1 | 3.7 |
| secrets@1.0.1 | 0.1 | 0.4 |
| max_length@1.0.0 | 0.1 | 0.1 |

Per-guard latency, `all`, 1 user(s):

| Guard | p50 ms | p99 ms |
|---|---:|---:|
| prompt_injection@1.1.1 | 1926.9 | 1949.0 |
| jailbreak@1.1.1 | 1925.3 | 1946.3 |
| faithfulness@1.0.0 | 304.3 | 305.6 |
| pii@1.0.1 | 14.0 | 42.8 |
| banned_topics@1.0.0 | 17.1 | 38.9 |
| toxicity@1.1.0 | 28.4 | 29.7 |
| secrets@1.0.1 | 0.2 | 0.3 |
| max_length@1.0.0 | 0.1 | 0.2 |
| output_schema@1.0.0 | 0.1 | 0.2 |

`faithfulness` here is the mock standing in for the judge, not Gemini.

Guard errors (timeouts), by run:

- `all_but_faithfulness`, 50 users: pii@1.0.1 531
- `all`, 10 users: banned_topics@1.0.0 85, jailbreak@1.1.1 100, pii@1.0.1 91, prompt_injection@1.1.1 100
- `all`, 50 users: banned_topics@1.0.0 488, jailbreak@1.1.1 499, pii@1.0.1 499, prompt_injection@1.1.1 499
