# Communications — Email Flow & Decoupling Guide

This document explains how the current email sending process works, with references to specific files and line numbers, and describes how to replace the synchronous in-process sending with an external microservice.

---

## Current Flow: How an Email Is Sent

### 1. Entry Point — `afterChange` hook

**File:** `src/collections/Communications.ts` · **Line 36**

```ts
afterChange: [
  async ({ doc }) => {
```

Every time a `Communications` document is **created or updated** in MongoDB, Payload fires this hook **synchronously** — the HTTP request that saved the document does not return until all email sending is complete. This is the root cause of the blocking behaviour.

---

### 2. Body Enrichment — Resolving Upload Attachments

**File:** `src/collections/Communications.ts` · **Lines 37–47**

```ts
const { tos, ccs, bccs, subject, body } = doc;
for (const part of body) {
  if (part.type !== "upload") { continue; }
  const doc = await payload.findByID({
    collection: relationToSlug,
    id: part.value.id,
  });
  part.value = { ...part.value, ...doc };
}
```

For each `upload` block in the rich-text body, the full media document is fetched from MongoDB and merged into `part.value`. This is required so the serializer can produce a proper `<a href>` tag pointing to the file URL.

---

### 3. Rich-Text → HTML Serialization

**File:** `src/collections/Communications.ts` · **Line 48**

```ts
const html = TextUtils.Serialize(body || "");
```

`TextUtils.Serialize` (**`src/utils/TextUtils.ts` lines 17–88**) walks the Slate AST recursively and converts each node into an HTML string — headings, bold, italic, links, uploads, etc. The result is the final HTML body of the email.

> **Note:** `TextUtils.Serialize` has **zero Payload dependencies** — it only uses `slate` and `escape-html`. This makes it trivial to extract into a shared package for use by an external microservice.

---

### 4. Resolving `tos` → Email Addresses

**File:** `src/collections/Communications.ts` · **Lines 50–58**

```ts
const users = await payload.find({
  collection: tos[0].relationTo,
  where: { id: { in: tos.map((to) => to.value.id || to.value).join(",") } },
});
const usersEmails = users.docs.map((u) => u.email);
```

The `tos` field stores relationship references (collection slug + document id). This query resolves them to actual `User` documents and extracts their email addresses. If no valid addresses are found, an error is thrown at line 59.

---

### 5. Resolving `ccs` and `bccs`

**File:** `src/collections/Communications.ts` · **Lines 62–79**

The same pattern is repeated for CC and BCC recipients, producing comma-separated email strings assigned to `cc` and `bcc` variables.

---

### 6. Building and Dispatching One Message per Recipient

**File:** `src/collections/Communications.ts` · **Lines 80–95**

```ts
for (const to of usersEmails) {
  const message = {
    from: payload.emailOptions.fromAddress,
    subject, to, cc, bcc, html,
  };
  promises.push(
    MailUtils.sendMail(payload, message).catch((e) => {
      MZingaLogger.Instance?.error(`[Communications:err] ${e}`);
      return null;
    })
  );
}
await Promise.all(promises.filter((p) => Boolean(p)));
```

One SMTP message is built per recipient and pushed into a `promises` array. `Promise.all` waits for **every single SMTP call** to settle before the hook returns — this is the **synchronous bottleneck** that blocks the request.

---

### 7. The Actual SMTP Call

**File:** `src/utils/MailUtils.ts` · **Lines 12–13**

```ts
const email = await payload.email;
const result = await email.transport.sendMail(message);
```

`payload.email` exposes the Nodemailer transport configured in `mzinga.config.ts`. `sendMail` is a direct, blocking SMTP call. If the SMTP server is slow or unavailable, the entire request hangs or fails.

---

### 8. `sendToAll` Pre-Population

**File:** `src/collections/Communications.ts` · **Lines 148–172** (`tos.beforeValidate` hook)

When the `sendToAll` checkbox is ticked, all users are fetched in pages of 100 and the `tos` array is fully populated **before the document is saved**. This means the complete recipient list is already persisted in MongoDB by the time `afterChange` fires — a key enabler for the decoupling strategy below.

---

## Proposed Change: External Microservice

The decoupling is straightforward because **all data needed to send the email is already stored in the `Communications` document** by the time `afterChange` fires.

### What Changes in `Communications.ts`

Replace the entire body of the `afterChange` hook (lines 37–103) with a single queue publish:

```ts
afterChange: [
  async ({ doc }) => {
    await publishToQueue("communications.send", { id: doc.id });
  },
],
```

The hook no longer resolves users, serializes HTML, or calls SMTP. It publishes the document `id` to RabbitMQ and returns immediately. The HTTP request completes in milliseconds.

---

### What the External Microservice Does

The microservice subscribes to the `communications.send` queue and, for each message received:

| Step | Action | Current code reference |
|------|--------|------------------------|
| 1 | Fetch the full `Communications` document from MongoDB by `id` | — |
| 2 | Run the upload-enrichment loop | `Communications.ts` lines 38–47 |
| 3 | Serialize rich-text body to HTML | `TextUtils.ts` lines 17–88 |
| 4 | Resolve `tos`, `ccs`, `bccs` to email addresses from MongoDB | `Communications.ts` lines 50–79 |
| 5 | Dispatch via the appropriate channel (SMTP, Slack, etc.) based on a `channel` field | `MailUtils.ts` lines 12–13 |
| 6 | Write back a `status` field (`sent` / `failed`) to the document | — |

---

### Why This Works Cleanly

- **Recipients are already materialised.** The `sendToAll` hook (`Communications.ts` lines 148–172) populates the full `tos` array before the document is saved, so the microservice never needs to re-run the pagination logic.
- **`TextUtils.Serialize` is portable.** It has no Payload dependencies (`TextUtils.ts` lines 17–88) and can be published as a shared npm package consumed by both this app and the microservice.
- **RabbitMQ is already in place.** The existing `RABBITMQ_URL` infrastructure and webhook system described in `README.md` can be reused directly for the publish call.
- **Multi-channel support is additive.** Adding a `channel` field (e.g. `email`, `slack`) to the `Communications` collection requires no changes to the hook — the microservice simply branches on that field to call the right delivery adapter.

---

**Previous:** [02 — Architecture Evolution](02-architecture-evolution.md) · **Next:** [04 — The Strangler Fig Pattern](04-strangler-fig-pattern.md)
