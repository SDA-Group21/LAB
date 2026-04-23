# The Strangler Fig Pattern

---

## Origin and Metaphor

The name comes from the strangler fig tree — a tropical plant that grows around a host tree, gradually wrapping it from the outside. Over years, the fig's roots and trunk expand until they fully encase and eventually replace the host, which dies and rots away leaving the fig standing on its own. Martin Fowler coined the software pattern in 2004 drawing directly from this metaphor.

---

## What Problem It Solves

The core concern is **how to modernise or replace a legacy system without a big-bang rewrite**.

A big-bang rewrite means stopping all feature development, rewriting everything in parallel, and switching over in one go. This approach has a well-documented failure rate — the new system must replicate every behaviour of the old one, including undocumented edge cases, before it can go live. The business takes on maximum risk at the moment of cutover.

The Strangler Fig solves four specific concerns:

- **Risk** — changes are incremental and reversible. At any point the old system is still running and can absorb traffic
- **Continuity** — the business keeps shipping features and the system stays live throughout the migration
- **Irreversibility** — each extracted piece can be validated in production before the next piece is moved
- **Scope creep** — because you migrate one capability at a time, the scope of each change is bounded and testable

---

## Context of Application

The pattern applies when all of the following are true:

- You have a working monolith that you cannot afford to take offline
- The monolith has identifiable, separable capabilities — not a single tangled ball of logic
- You want to move toward a more distributed or service-oriented architecture without committing to a full rewrite upfront
- You have a way to intercept or redirect calls — either at the HTTP layer (a proxy or API gateway), at the event layer (a message bus), or at the data layer (a shared database with a status flag)

It is most commonly applied to:

- **Legacy enterprise systems** being broken into microservices
- **Monolithic e-commerce or CMS platforms** where individual domains (payments, notifications, search) are extracted one at a time
- **SaaS platforms** that need to scale specific capabilities independently without rewriting the core

---

## How It Works — The Three Moves

Every application of the Strangler Fig follows the same three moves regardless of the technology:

1. **Intercept** — introduce a seam between the caller and the functionality you want to extract. This seam can be an HTTP proxy, a feature flag, a queue message, or a status field in a database. The monolith continues to handle everything while the seam is put in place.

2. **Migrate** — build the new service alongside the monolith. Route a subset of traffic or events to the new service. Both the monolith and the new service handle the same capability in parallel during this phase, which allows comparison and validation.

3. **Eliminate** — once the new service is proven, remove the corresponding code from the monolith. The monolith shrinks. Repeat for the next capability.

Over time the monolith is progressively hollowed out until either it disappears entirely or a stable, irreducible core remains.

---

## How It Is Applied to MZinga

In the MZinga communications context the three moves map directly to the codebase:

**Intercept** — the `afterChange` hook in `Communications.ts` line 36 is the natural seam. It already sits between the document persistence (MongoDB write) and the email delivery (SMTP call). Replacing the hook body with a `status: "pending"` write introduces the seam without touching anything else. The HTTP request that created the document now returns immediately.

**Migrate** — the external worker is built and deployed alongside the running monolith. It reads `status = "pending"` documents and processes them. At this point both paths exist: the old hook (now a no-op) and the new worker. You can validate delivery in production before committing.

**Eliminate** — once the worker is proven, the old SMTP logic (`Communications.ts` lines 37–103, `MailUtils.ts` lines 4–21) is deleted. The monolith no longer knows anything about email transport. That responsibility has been fully strangled out.

> The key insight for MZinga specifically is that the `sendToAll` pre-population hook (`Communications.ts` lines 148–172) already materialises all recipient data into the document before it is saved. This means the seam is clean — the worker finds everything it needs in the document itself and the monolith does not need to pass any additional context across the boundary.

---

## What It Does Not Solve

The Strangler Fig is a **migration strategy, not an architecture**. It does not by itself solve:

- **Data consistency** — if the monolith and the new service both write to the same database during the migration phase, you need to manage concurrent writes carefully
- **Distributed systems complexity** — once the capability is extracted, you now own a network boundary, a deployment pipeline, and an operational surface that did not exist before
- **Observability gaps** — tracing a request that starts in the monolith and completes in an external service requires distributed tracing from day one, not as an afterthought
- **Schema coupling** — in Step 1 of the MZinga evolution (DB-Coupled Worker), the Strangler Fig moves the logic out but leaves the data coupling in place. That is why Step 2 (REST API) and Step 3 (RabbitMQ Pub/Sub) are needed to complete the decoupling

> The Strangler Fig gets you out of the monolith safely. What you build outside it is a separate architectural decision.

---

**Previous:** [03 — Communications Email Flow & Decoupling Guide](03-communications-email-flow.md) · **Next:** [05 — Supporting Patterns Catalogue](05-supporting-patterns-catalogue.md)
