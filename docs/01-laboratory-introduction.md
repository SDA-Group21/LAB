# Laboratory Introduction: Software Architecture Patterns with MZinga

---

## What Is MZinga

[MZinga](https://github.com/mzinga-io) is an open source Content Management System built and maintained by [Newesis Srl](https://github.com/mzinga-io). It is forked from [Payload CMS](https://payloadcms.com/) version 2 and extended into a production-grade platform for SaaS and enterprise environments. The core repository is [mzinga-core](https://github.com/mzinga-io/mzinga-core), and the application layer we work with in this laboratory is [mzinga-apps](https://github.com/mzinga-io/mzinga-apps).

MZinga is not a toy project or a tutorial scaffold. It is a real system, actively developed, deployed in production environments, and carrying the full complexity that entails: multi-tenancy, role-based access control, scheduled tasks, distributed caching with Redis, asynchronous messaging with RabbitMQ, observability via OpenTelemetry, and support for both MongoDB and PostgreSQL as database backends. The `package.json` alone lists over forty production dependencies spanning transport, messaging, tracing, authentication, and content rendering.

This is precisely why it is the right subject for this laboratory.

---

## Why a Real System Matters

Software architecture is a discipline that is easy to understand in the abstract and difficult to apply in practice. Patterns described in isolation — a box labelled "Service A" sending an arrow to a box labelled "Service B" — give the impression that architectural decisions are clean, obvious, and consequence-free. They are not.

Real systems carry history. They carry decisions made under time pressure, dependencies that cannot be changed overnight, data models that dozens of features rely on, and operational constraints that no diagram captures. Learning to read a real codebase, identify the patterns already present, understand why they were chosen, and reason about how to evolve them — without breaking what works — is the actual skill this laboratory develops.

MZinga gives us a system with enough complexity to be instructive and enough clarity in its structure to be approachable. It is written in TypeScript, runs on Node.js 18, and its architecture is visible directly in the source code without requiring reverse engineering.

---

## What This Laboratory Is About

This laboratory uses MZinga as a living case study for **software architecture patterns applied to solution design, transformation, and evolution**.

The focus is not on building features. It is on understanding the architectural decisions embedded in an existing system, identifying the patterns those decisions represent, and reasoning through how the system can be transformed — step by step, safely, without a rewrite — to meet new requirements.

The laboratory is structured around a single, concrete capability: the **Communications system**, which handles how MZinga sends notifications to users. This is a deliberately bounded scope. It is small enough to understand completely, and rich enough to illustrate a full progression of architectural patterns from a synchronous monolith to an event-driven microservice.

Every pattern introduced in this laboratory is grounded in actual lines of code in the MZinga repository. When we discuss the Observer pattern, we point to `Communications.ts` line 36. When we discuss the Strangler Fig, we point to the specific hook body that becomes the migration seam. When we discuss Pub/Sub, we point to `messageBusService.ts` and `WebHooks.ts`, which already implement the infrastructure. The patterns are not hypothetical — they are either already present in the code or they are the next concrete step in a real evolution path.

---

## The Architecture Journey

The laboratory follows a progression of four architectural states, each representing a distinct pattern and a distinct set of trade-offs:

1. **State 0 — Modular Monolith with Synchronous In-Process Observer.**
   The current state of MZinga. All concerns run in a single Node.js process. Email is sent synchronously inside a Payload lifecycle hook, blocking the HTTP request until every SMTP call completes. This is the baseline we analyse, understand, and then evolve away from.

2. **State 1 — Strangler Fig into a DB-Coupled External Worker.**
   The first extraction. A new service is introduced alongside the monolith, reading directly from the shared MongoDB database. The monolith's hook is reduced to a status flag write. The Strangler Fig pattern governs the transition. The Shared Database integration pattern governs the coupling.

3. **State 2 — REST API-Coupled External Worker.**
   The database coupling is removed. The external worker interacts with MZinga exclusively through its auto-generated REST API, authenticated via JWT. The Remote Facade pattern governs the integration. The worker is now schema-agnostic.

4. **State 3 — Event-Driven Microservice via RabbitMQ Pub/Sub.**
   The polling is removed. The monolith publishes an event to RabbitMQ when a `Communications` document is saved — requiring only an environment variable change, no code modification. The worker subscribes and reacts. The Publish/Subscribe pattern governs the integration. The monolith and the worker are fully decoupled.

---

## The Microservices Argument — Grounded in This Context

Microservices are often introduced as an abstract architectural ideal. In this laboratory they are introduced as a practical answer to specific, observable problems in the MZinga codebase.

- **Segregation of concerns.** The current `Communications.ts` hook mixes three distinct responsibilities: resolving user data from the database, rendering HTML from a Slate AST, and delivering messages over SMTP. These are different concerns with different rates of change, different failure modes, and different scaling requirements. Separating them into a dedicated service makes each concern independently testable, deployable, and replaceable.

- **Language freedom.** Once the communications capability is extracted into its own service, it no longer needs to be written in TypeScript. A Python service might be preferred for its mature email templating ecosystem. A Go service might be preferred for its concurrency model when handling thousands of simultaneous deliveries. A service that posts to Slack might be written by a team that works entirely in Kotlin. The monolith does not care — it publishes an event and the contract ends there.

- **Replaceability of components.** The current system sends only email, via SendGrid, configured in `server.ts` lines 120–128. Adding Slack notifications today would require modifying `Communications.ts`, adding a new dependency to the monolith, and redeploying the entire application. In the microservice model, a new delivery adapter — Slack, SMS, push notification, webhook — is a new implementation of the `CommunicationChannel` interface inside the worker, or an entirely separate worker subscribing to the same RabbitMQ exchange. The monolith is never touched. The `Communications` collection gains a `channel` field and the routing logic lives entirely outside the core system.

- **Independent scalability.** Sending bulk communications — the `sendToAll` path in `Communications.ts` lines 148–172 can generate hundreds of recipient entries — currently blocks the Node.js event loop. As a separate service with Competing Consumers on a RabbitMQ queue, the delivery layer scales horizontally by adding worker instances, with no impact on the monolith's ability to serve the admin UI and REST API.

- **Fault isolation.** If SendGrid is down today, the `afterChange` hook throws, the HTTP request fails, and the `Communications` document may or may not be saved depending on where in the hook the failure occurs. In the microservice model, the monolith saves the document and publishes the event regardless. The delivery failure is contained entirely within the worker, handled by a Dead Letter Queue, and retried without any user-facing impact on the core application.

---

## How to Use These Documents

The laboratory is documented across a set of markdown files in the `mzinga-apps` repository root, each addressing a specific layer of the architecture study:

| Document | Contents |
|---|---|
| `02-architecture-evolution.md` | Line-by-line walkthrough of the current email flow and the specific code changes needed to decouple it |
| `03-communications-email-flow.md` | The full four-state evolution with pattern names, trade-offs, and code references at each step |
| `04-strangler-fig-pattern.md` | A deep dive into the Strangler Fig as the primary migration pattern, its origin, mechanics, and limitations |
| `05-supporting-patterns-catalogue.md` | The full catalogue of supporting patterns relevant across all four states, from Anti-Corruption Layer to Event Sourcing |

Read them in order. Each document builds on the previous one.

> The goal is not to memorise pattern names — it is to develop the habit of recognising the forces at play in a real system and selecting the pattern that addresses those forces with the least unnecessary complexity.

---

**Next:** [02 — Architecture Evolution: Four States from Monolith to Event-Driven](02-architecture-evolution.md)
