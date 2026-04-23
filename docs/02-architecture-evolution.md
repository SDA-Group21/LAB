# Software Architecture Patterns in MZinga — Communications Flow

---

## Current Pattern: Modular Monolith

MZinga is a **modular monolith**. All concerns — HTTP serving, business logic, data access, and email delivery — run in a single Node.js process. The modularity comes from how the codebase is organised: collections, hooks, utils, endpoints, and configs are cleanly separated into their own modules under `src/`, but they all compile and deploy as one unit via `server.ts`.

### Key Structural Evidence

- `server.ts` line 27 bootstraps a single Express app that hosts everything — admin UI, REST API, metrics, health probes, and the Payload CMS engine — all in one process
- `mzinga.config.ts` lines 196–207 wires every collection through `WebHooks.EnrichCollection` and `WebHooks.EnrichFields` at startup, meaning all hook logic is registered in-process
- `messageBusService.ts` line 113 shows RabbitMQ is connected at startup inside the same process — it is used as an outbound publisher only, not as a decoupling boundary

The pattern applied to collections is **Domain Module + Hook Pipeline**: each collection in `src/collections/` is a self-contained domain module that declares its own fields, access rules, and lifecycle hooks. `Communications.ts` is a canonical example of this.

---

## Current Communication Flow Pattern: Synchronous In-Process Observer

The `afterChange` hook in `Communications.ts` line 36 is an implementation of the **Observer pattern** — Payload fires it after a document is persisted, and the hook reacts to that event. However, the observer runs **synchronously inside the same process and the same request lifecycle**, which means:

- The HTTP request that creates the `Communications` document does not return until every SMTP call resolves — `Communications.ts` lines 80–95 with `await Promise.all(...)`
- The SMTP transport at `MailUtils.ts` lines 12–13 is a direct blocking call to Nodemailer's `email.transport.sendMail`
- If SendGrid (configured in `server.ts` lines 120–128 via `nodemailerSendgrid`) is slow or unavailable, the entire request thread is blocked
- There is no retry logic, no dead-letter handling, and no delivery status written back to the document

> The pattern is effectively a **Synchronous In-Process Event Handler** — the observer and the subject share the same thread, memory, and failure domain.

---

## Step 1 — Strangler Fig into a DB-Coupled External Service

The first transformation applies the **Strangler Fig pattern**: introduce a new service alongside the monolith without changing its public API, gradually moving responsibility out.

The new service is a **DB-Coupled Worker** — it connects directly to the same MongoDB instance and polls for new `Communications` documents to process.

### What Changes in the Monolith

The entire body of the `afterChange` hook (`Communications.ts` lines 37–103) is replaced with a single status field write:

```ts
afterChange: [
  async ({ doc }) => {
    await payload.update({
      collection: Slugs.Communications,
      id: doc.id,
      data: { status: "pending" },
    });
  },
],
```

The hook now only marks the document as `pending` and returns immediately. The HTTP request completes in milliseconds.

### What the External Service Does

The worker applies the **Polling Consumer pattern** — it queries MongoDB on an interval for documents where `status = "pending"`, processes them (resolving users, serialising HTML via `TextUtils.Serialize`, dispatching via SMTP or Slack), and writes `status = "sent"` or `status = "failed"` back.

> `TextUtils.Serialize` (`TextUtils.ts` lines 17–88) can be extracted into a shared npm package since it has no Payload dependencies — only `slate` and `escape-html`.

### Pattern Summary

| Concern | Pattern |
|---|---|
| Transition strategy | Strangler Fig |
| Worker consumption model | Polling Consumer |
| Delivery channel abstraction | Strategy (swap SMTP for Slack, etc.) |
| Shared logic extraction | Shared Kernel (npm package) |

> **Trade-off:** The worker is still tightly coupled to the database schema. Any MongoDB schema change in the `Communications` collection breaks the worker directly. The two services share a data store — the **Shared Database integration pattern** — which is pragmatic but limits independent deployability.

---

## Step 2 — REST API-Coupled External Service

The second transformation removes the shared database dependency and replaces it with the **API Gateway / Client-Server pattern**. The external service no longer reads MongoDB directly — it interacts exclusively with MZinga through its REST API.

### What Changes in the Monolith

The `afterChange` hook is reduced to the same `pending` status write as Step 1. The worker now authenticates against the Payload REST API using these auto-generated endpoints:

```
GET   /api/communications?where[status][equals]=pending
GET   /api/communications/:id
PATCH /api/communications/:id
```

These endpoints are already available — Payload auto-generates full REST CRUD for every collection, including `Communications`, protected by the access rules defined in `Communications.ts` lines 15–25 (`GetIsAdmin` for read and create).

### What the External Service Does

The worker applies the **Remote Facade pattern** — it treats MZinga as an opaque service and communicates only through its published HTTP interface:

1. Authenticates via `POST /api/users/login` to obtain a JWT
2. Polls `GET /api/communications?where[status][equals]=pending` for work
3. Fetches full document details including resolved relationships
4. Dispatches via the appropriate channel
5. Writes back status via `PATCH /api/communications/:id`

### Pattern Summary

| Concern | Pattern |
|---|---|
| Integration style | REST / Remote Facade |
| Data ownership | Single owner (MZinga owns the data, worker is a consumer) |
| Auth | JWT Bearer token (already in place via Payload's auth system) |
| Coupling level | Contract coupling (HTTP schema) instead of data coupling (DB schema) |

> **Trade-off:** The worker is now decoupled from the database schema, but it still **polls** — introducing latency between document creation and delivery, and unnecessary load on the API when there is nothing to process. This is solved in the next step.

---

## Step 3 — Event-Driven Microservice via RabbitMQ Pub/Sub

The final transformation is a full **Event-Driven Architecture** using the **Publish/Subscribe pattern**. This is the most loosely coupled form, and the infrastructure for it is already present in the codebase.

### What Already Exists

- `messageBusService.ts` lines 16–27 declares two exchanges: `mzinga_events` (topic, transient) and `mzinga_events_durable` (topic, durable, persistent). The durable exchange is bound to receive all events from the transient one via routing key `#` at lines 62–66
- `messageBusService.ts` lines 83–100 exposes `publishEvent(event)` which sends to the `mzinga_events` exchange with the event type as the routing key
- `WebHooks.ts` lines 74–100 already implements a RabbitMQ publisher hook — when `HOOKSURL_COMMUNICATIONS_AFTERCHANGE=rabbitmq` is set in `.env`, every `afterChange` automatically publishes to RabbitMQ with the full document payload
- `server.ts` lines 93–99 connects `messageBusService` at startup if `RABBITMQ_URL` is present

### What Changes in the Monolith

The `afterChange` hook in `Communications.ts` is **removed entirely**. Instead, set one environment variable:

```bash
HOOKSURL_COMMUNICATIONS_AFTERCHANGE=rabbitmq
```

`WebHooks.EnrichCollection` in `mzinga.config.ts` lines 196–207 picks this up at startup and automatically attaches the RabbitMQ publisher hook to the `Communications` collection — **no code change required**.

The published event routing key becomes `HOOKSURL_COMMUNICATIONS_AFTERCHANGE` and the payload includes `doc`, `data`, `operation`, and `previousDoc` as built in `WebHooks.ts` lines 82–95.

### What the External Microservice Does

The worker is now a pure **event consumer** — it subscribes to the `mzinga_events_durable` exchange with a binding key of `HOOKSURL_COMMUNICATIONS_AFTERCHANGE` (or a wildcard like `HOOKSURL_COMMUNICATIONS_*`). For each message:

1. Reads `event.data.doc.id` from the message body
2. Calls `GET /api/communications/:id` on the MZinga REST API to get the full resolved document (applying the REST pattern from Step 2, keeping the worker schema-agnostic)
3. Dispatches via the appropriate channel — SMTP, Slack, or any future adapter — using the **Strategy pattern** keyed on a `channel` field in the document
4. Writes back delivery status via `PATCH /api/communications/:id`

### Pattern Summary

| Concern | Pattern |
|---|---|
| Integration style | Event-Driven Architecture |
| Messaging model | Publish/Subscribe (topic exchange, durable queue) |
| Event durability | Guaranteed via `mzinga_events_durable` exchange (already configured) |
| Worker consumption | Competing Consumers (multiple instances for horizontal scaling) |
| Delivery channel | Strategy pattern |
| Retry / dead-letter | Handled at the RabbitMQ level, outside the monolith |
| Monolith change required | Zero — only an `.env` variable |

---

## Evolution Summary

```
Modular Monolith       Step 1                 Step 2                 Step 3
─────────────────   ──────────────────    ──────────────────    ──────────────────
Synchronous         DB-Coupled Worker     REST API Worker       Event-Driven Worker
In-Process Hook  →  (Shared Database) →   (Remote Facade)   →  (Pub/Sub, RabbitMQ)
                    Strangler Fig         API Contract          Zero monolith change
                    Polling Consumer      JWT Auth              Competing Consumers
                    Shared Kernel         Single Data Owner     Durable Events
```

Each step increases decoupling, reduces the blast radius of failures, and moves closer to independent deployability — while Step 3 requires no code change to the monolith at all, only configuration.

---

**Previous:** [01 — Laboratory Introduction](01-laboratory-introduction.md) · **Next:** [03 — Communications Email Flow & Decoupling Guide](03-communications-email-flow.md)
