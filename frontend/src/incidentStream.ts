type IncidentStreamHandlers = {
  onOpen: () => Promise<void>;
  onFrame: (lines: string[]) => Promise<void>;
  onClose: () => void;
};

const maxFrameLength = 16_384;

export async function consumeIncidentStream(
  body: ReadableStream<Uint8Array>, signal: AbortSignal, handlers: IncidentStreamHandlers,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    await handlers.onOpen();
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done || signal.aborted) return;
      pending += decoder.decode(value, { stream: true });
      let boundary = /\r?\n\r?\n/.exec(pending);
      while (boundary && !signal.aborted) {
        if (boundary.index > maxFrameLength) throw new Error("invalid event stream");
        const frame = pending.slice(0, boundary.index);
        pending = pending.slice(boundary.index + boundary[0].length);
        await handlers.onFrame(frame.split(/\r?\n/));
        boundary = /\r?\n\r?\n/.exec(pending);
      }
      if (pending.length > maxFrameLength) throw new Error("invalid event stream");
    }
  } finally {
    if (!signal.aborted) handlers.onClose();
    // Closing the reader also releases the HTTP stream if snapshot or frame handling failed.
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
