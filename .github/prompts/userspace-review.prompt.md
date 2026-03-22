---
description: Use when reviewing virtrtlabd and daemon userspace C changes for epoll correctness, socket handling, shutdown safety, and runtime permission behavior
---

Review the target daemon diff as a systems reviewer, not as an implementer.

Produce:

1. Blockers
2. Majors
3. Minors
4. Questions
5. Positive points

Focus on descriptor lifecycle, partial I/O, EINTR handling, cleanup, reconnect and stop behavior, logging, and tests.