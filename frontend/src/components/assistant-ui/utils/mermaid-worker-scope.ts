// ELK's bundled CommonJS module mistakes `self` without `document` for its own
// worker entry point. Its lazy initialization must select the in-process driver;
// deleting `self` cannot hide the inherited WorkerGlobalScope getter in browsers.
// Keep `self` intact and mark ELK's browser feature flag only during initialization
// and synchronous rendering. This does not provide any DOM APIs to the worker.
export const workerScope = self;
const environment = globalThis as { document?: unknown };
const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
const originalDocument = environment.document;
const hadDocument = Object.hasOwn(environment, "document");
export function prepareWorkerScope() {
  environment.document = {};
  // The library also assigns `self` when restoring its environment. Shadow the
  // inherited getter with a writable own property for that synchronous call.
  Object.defineProperty(globalThis, "self", { value: workerScope, configurable: true, writable: true });
}
prepareWorkerScope();

export function restoreWorkerScope() {
  if (hadDocument) environment.document = originalDocument;
  else delete environment.document;
  if (originalSelf) Object.defineProperty(globalThis, "self", originalSelf);
  else delete (globalThis as { self?: unknown }).self;
}
